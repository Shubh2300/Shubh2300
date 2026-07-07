"""Approval state machine + service.

State machine (from the spec):

    proposed
       └─ submit ──────────────▶ pending_approval
                                    ├─ approve ──▶ approved ─ start ─▶ executing
                                    └─ reject  ──▶ rejected (terminal)
    executing
       ├─ verify           ─▶ verified            (terminal)
       ├─ fail             ─▶ failed              (terminal)
       └─ needs_review     ─▶ needs_human_review  (terminal)

The state machine is pure logic. Persistence goes through an injected
``ApprovalStore`` protocol so the service can be unit tested with an in-memory
store (test isolation — NOT mock patient data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol


class ApprovalState(str, Enum):
    proposed = "proposed"
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"
    executing = "executing"
    verified = "verified"
    failed = "failed"
    needs_human_review = "needs_human_review"


class ApprovalEvent(str, Enum):
    submit = "submit"
    approve = "approve"
    reject = "reject"
    start = "start"
    verify = "verify"
    fail = "fail"
    needs_review = "needs_review"


TERMINAL_STATES = frozenset(
    {
        ApprovalState.rejected,
        ApprovalState.verified,
        ApprovalState.failed,
        ApprovalState.needs_human_review,
    }
)


# (state, event) -> next_state
_TRANSITIONS: Dict[tuple, ApprovalState] = {
    (ApprovalState.proposed, ApprovalEvent.submit): ApprovalState.pending_approval,
    (ApprovalState.pending_approval, ApprovalEvent.approve): ApprovalState.approved,
    (ApprovalState.pending_approval, ApprovalEvent.reject): ApprovalState.rejected,
    (ApprovalState.approved, ApprovalEvent.start): ApprovalState.executing,
    (ApprovalState.executing, ApprovalEvent.verify): ApprovalState.verified,
    (ApprovalState.executing, ApprovalEvent.fail): ApprovalState.failed,
    (ApprovalState.executing, ApprovalEvent.needs_review): ApprovalState.needs_human_review,
}


class InvalidTransition(Exception):
    """Raised when an event is not legal from the current state."""


class ApprovalStateMachine:
    """Pure transition logic. No I/O."""

    transitions = _TRANSITIONS

    @classmethod
    def can(cls, state: ApprovalState, event: ApprovalEvent) -> bool:
        return (state, event) in cls.transitions

    @classmethod
    def next_state(cls, state: ApprovalState, event: ApprovalEvent) -> ApprovalState:
        try:
            return cls.transitions[(state, event)]
        except KeyError:
            raise InvalidTransition(
                f"cannot apply '{event.value}' from state '{state.value}'"
            )

    @classmethod
    def is_terminal(cls, state: ApprovalState) -> bool:
        return state in TERMINAL_STATES


# --------------------------------------------------------------------------- #
# Records + store protocol
# --------------------------------------------------------------------------- #
@dataclass
class ApprovalRecord:
    id: str
    action: str
    intent: Dict[str, Any]
    state: ApprovalState = ApprovalState.proposed
    task_id: Optional[str] = None
    submitted_by: Optional[str] = None
    approver_user_id: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = None


class ApprovalStore(Protocol):
    def create(self, record: ApprovalRecord) -> ApprovalRecord: ...
    def get(self, approval_id: str) -> Optional[ApprovalRecord]: ...
    def update(self, record: ApprovalRecord) -> ApprovalRecord: ...
    def list_by_state(self, state: ApprovalState) -> List[ApprovalRecord]: ...


class InMemoryApprovalStore:
    """Store used by unit tests and as a reference implementation."""

    def __init__(self) -> None:
        self._rows: Dict[str, ApprovalRecord] = {}
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"appr_{self._seq}"

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        if not record.id:
            record.id = self._next_id()
        self._rows[record.id] = record
        return record

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        return self._rows.get(approval_id)

    def update(self, record: ApprovalRecord) -> ApprovalRecord:
        record.updated_at = datetime.now(timezone.utc)
        self._rows[record.id] = record
        return record

    def list_by_state(self, state: ApprovalState) -> List[ApprovalRecord]:
        return [r for r in self._rows.values() if r.state == state]


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class ApprovalService:
    """Coordinates the state machine with a persistence store.

    v1 policy (locked decision #11): any staff member may approve anything.
    We still record *who* approved and *when* on every decision.
    """

    def __init__(self, store: ApprovalStore, clock=None) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _apply(self, record: ApprovalRecord, event: ApprovalEvent) -> ApprovalRecord:
        record.state = ApprovalStateMachine.next_state(record.state, event)
        return record

    def submit(
        self,
        *,
        approval_id: str,
        action: str,
        intent: Dict[str, Any],
        submitted_by: str,
        task_id: Optional[str] = None,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            id=approval_id,
            action=action,
            intent=intent,
            task_id=task_id,
            submitted_by=submitted_by,
        )
        self._apply(record, ApprovalEvent.submit)
        return self._store.create(record)

    def list_pending(self) -> List[ApprovalRecord]:
        return self._store.list_by_state(ApprovalState.pending_approval)

    def approve(self, approval_id: str, approver_user_id: str) -> ApprovalRecord:
        record = self._require(approval_id)
        self._apply(record, ApprovalEvent.approve)
        record.approver_user_id = approver_user_id
        record.decided_at = self._clock()
        return self._store.update(record)

    def reject(
        self, approval_id: str, approver_user_id: str, reason: Optional[str] = None
    ) -> ApprovalRecord:
        record = self._require(approval_id)
        self._apply(record, ApprovalEvent.reject)
        record.approver_user_id = approver_user_id
        record.decided_at = self._clock()
        return self._store.update(record)

    def mark_executing(self, approval_id: str) -> ApprovalRecord:
        record = self._require(approval_id)
        self._apply(record, ApprovalEvent.start)
        return self._store.update(record)

    def mark_outcome(self, approval_id: str, event: ApprovalEvent) -> ApprovalRecord:
        if event not in (
            ApprovalEvent.verify,
            ApprovalEvent.fail,
            ApprovalEvent.needs_review,
        ):
            raise InvalidTransition(f"'{event.value}' is not an outcome event")
        record = self._require(approval_id)
        self._apply(record, event)
        return self._store.update(record)

    def _require(self, approval_id: str) -> ApprovalRecord:
        record = self._store.get(approval_id)
        if record is None:
            raise KeyError(f"approval '{approval_id}' not found")
        return record

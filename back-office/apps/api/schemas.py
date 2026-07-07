"""Pydantic v2 schemas shared across routers and services.

These are transport/validation models for the API surface. The canonical
JSON Schema for an ActionIntent is owned by the action-registry package
(``action_intent.schema.json``) and is what the AI parser is constrained to;
``ActionIntent`` here is a permissive Python view over that shape so the API
can carry it around and pass it to the registry validator and Temporal.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class ApprovalState(str, Enum):
    proposed = "proposed"
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"
    executing = "executing"
    verified = "verified"
    failed = "failed"
    needs_human_review = "needs_human_review"


TERMINAL_STATES = frozenset(
    {
        ApprovalState.rejected,
        ApprovalState.verified,
        ApprovalState.failed,
        ApprovalState.needs_human_review,
    }
)


# --------------------------------------------------------------------------- #
# Action intent (parser output / execution input)
# --------------------------------------------------------------------------- #
class PatientRef(BaseModel):
    """A soft reference to a patient. Never fabricated by the parser — if the
    prompt does not name a patient this stays empty and resolution happens
    (and can fail-closed) at the bridge layer."""

    patient_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    dob: Optional[str] = None
    system_ids: Dict[str, str] = Field(default_factory=dict)


class ActionIntent(BaseModel):
    """Structured, reviewable representation of a staff request.

    This is *only* a proposal. It is never executed until a human approves it.
    """

    action: str
    system: Optional[str] = None  # "sis" | "svigg"; may be derived from registry
    patient: Optional[PatientRef] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    rationale: Optional[str] = None
    confidence: Optional[float] = None
    requested_by: Optional[str] = None


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
class TaskCreate(BaseModel):
    prompt: str = Field(min_length=1)
    created_by: str


class TaskOut(BaseModel):
    id: str
    prompt: str
    created_by: str
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Intent parsing
# --------------------------------------------------------------------------- #
class ParseRequest(BaseModel):
    prompt: str = Field(min_length=1)
    requested_by: Optional[str] = None
    task_id: Optional[str] = None


class RegistryValidation(BaseModel):
    ok: bool
    status: str  # "accepted" | "rejected"
    unknown_action: bool = False
    missing_inputs: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class ParseResponse(BaseModel):
    intent: ActionIntent
    validation: RegistryValidation


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #
class SubmitForApprovalRequest(BaseModel):
    intent: ActionIntent
    task_id: Optional[str] = None
    submitted_by: str


class ApprovalDecision(BaseModel):
    approver_user_id: str
    reason: Optional[str] = None


class ApprovalOut(BaseModel):
    id: str
    task_id: Optional[str] = None
    action: str
    state: ApprovalState
    intent: Dict[str, Any]
    submitted_by: Optional[str] = None
    approver_user_id: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Action runs
# --------------------------------------------------------------------------- #
class ActionRunOut(BaseModel):
    id: str
    approval_id: Optional[str] = None
    workflow_id: Optional[str] = None
    action: str
    state: ApprovalState
    status: Optional[str] = None  # bridge envelope status
    verified: Optional[bool] = None
    screenshot_id: Optional[str] = None
    trace_id: Optional[str] = None
    failure_reason: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
class AuditEntryOut(BaseModel):
    id: str
    ts: datetime
    actor: str
    action: str
    intent: Optional[str] = None
    result_summary: str
    prev_hash: str
    entry_hash: str


# --------------------------------------------------------------------------- #
# Patients (internal workflow-layer records)
# --------------------------------------------------------------------------- #
class PatientOut(BaseModel):
    id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    dob: Optional[str] = None
    system_ids: Dict[str, str] = Field(default_factory=dict)
    created_at: datetime

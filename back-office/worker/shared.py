"""Shared dataclasses passed between the API, workflow, and activities.

These must be import-safe from inside the Temporal workflow sandbox, so they
depend only on the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExecuteActionInput:
    approval_id: str
    action_run_id: str
    intent: Dict[str, Any]
    # Single-use token minted at human-approval time; required for writes
    # (risk_level >= 2). None for reads.
    approval_token: Optional[str] = None


@dataclass
class BridgeEnvelope:
    """Normalized view of the bridge's standard response envelope.

    Bridge envelope fields:
        status: success | failed | blocked | needs_human_review
        verified, system, action, patient_match, data, source, as_of,
        screenshot_id, trace_id, requires_human_review, warnings, failure_reason
    """

    status: str
    verified: bool = False
    system: Optional[str] = None
    action: Optional[str] = None
    screenshot_id: Optional[str] = None
    trace_id: Optional[str] = None
    requires_human_review: bool = False
    warnings: List[str] = field(default_factory=list)
    failure_reason: Optional[str] = None
    patient_match: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    as_of: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BridgeEnvelope":
        return cls(
            status=d.get("status", "failed"),
            verified=bool(d.get("verified", False)),
            system=d.get("system"),
            action=d.get("action"),
            screenshot_id=d.get("screenshot_id"),
            trace_id=d.get("trace_id"),
            requires_human_review=bool(d.get("requires_human_review", False)),
            warnings=list(d.get("warnings") or []),
            failure_reason=d.get("failure_reason"),
            patient_match=d.get("patient_match"),
            source=d.get("source"),
            as_of=d.get("as_of"),
        )


@dataclass
class ActionOutcome:
    """Final workflow result recorded against the action_run."""

    state: str  # verified | failed | needs_human_review
    status: Optional[str] = None
    verified: bool = False
    screenshot_id: Optional[str] = None
    trace_id: Optional[str] = None
    failure_reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

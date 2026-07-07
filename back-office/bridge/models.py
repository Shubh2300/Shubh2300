"""Pydantic v2 models for the Local EMR Bridge.

The single most important type here is ``BridgeResponse`` — the standard
envelope every endpoint returns. It is deliberately honest: a ``blocked`` or
``needs_human_review`` status is a first-class outcome, never dressed up as
success, and ``verified`` is only ever ``True`` when a post-action re-read
actually confirmed the change.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Status(str, Enum):
    success = "success"
    failed = "failed"
    blocked = "blocked"
    needs_human_review = "needs_human_review"


class System(str, Enum):
    sis = "sis"
    svigg = "svigg"
    both = "both"
    unknown = "unknown"


class MatchLevel(str, Enum):
    strong = "strong"
    weak = "weak"
    none = "none"


# ---------------------------------------------------------------------------
# Standard response envelope
# ---------------------------------------------------------------------------
class PatientMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match_level: MatchLevel = MatchLevel.none
    matched_by: list[str] = Field(default_factory=list)


class BridgeResponse(BaseModel):
    """The one response shape every bridge endpoint returns."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: Status
    verified: bool = False
    system: System
    action: str
    patient_match: PatientMatch = Field(default_factory=PatientMatch)
    data: Optional[Any] = None
    source: Optional[str] = None            # e.g. 'sis:rest', 'svigg:grid'
    as_of: Optional[str] = None             # ISO-8601 timestamp of the read
    screenshot_id: Optional[str] = None
    trace_id: Optional[str] = None
    # -- proof-of-outcome (vendored clients capture a screenshot on EVERY
    #    terminal outcome — success AND rejection/failure, not just success).
    #    proof_captured is True only when a screenshot was actually written;
    #    proof_kind labels what it shows: 'completed' (verified end-state) vs
    #    'rejected' (what the system saw when it did NOT complete). Both are
    #    additive/optional so existing envelope consumers are unaffected. ------
    proof_captured: bool = False
    proof_kind: Optional[str] = None        # 'completed' | 'rejected' | None
    requires_human_review: bool = False
    warnings: list[str] = Field(default_factory=list)
    failure_reason: Optional[str] = None

    # -- convenience constructors ------------------------------------------
    @classmethod
    def blocked(
        cls,
        *,
        system: System,
        action: str,
        reason: str,
        needed_from_user: Optional[list[str]] = None,
    ) -> "BridgeResponse":
        return cls(
            status=Status.blocked,
            system=system,
            action=action,
            failure_reason=reason,
            data={"needed_from_user": needed_from_user or []},
        )

    @classmethod
    def needs_review(
        cls,
        *,
        system: System,
        action: str,
        reason: str,
        patient_match: Optional[PatientMatch] = None,
        data: Optional[Any] = None,
    ) -> "BridgeResponse":
        return cls(
            status=Status.needs_human_review,
            system=system,
            action=action,
            requires_human_review=True,
            patient_match=patient_match or PatientMatch(),
            failure_reason=reason,
            data=data,
        )


# ---------------------------------------------------------------------------
# The canonical "blocked" payload for missing selectors / credentials.
# Endpoints return this shape (inside BridgeResponse.data / or raw) when a real
# implementation or env credential is absent at runtime.
# ---------------------------------------------------------------------------
class BlockedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = "blocked"
    reason: str = "Missing real SIS/Svigg selectors or credentials"
    needed_from_user: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared request models
# ---------------------------------------------------------------------------
class PatientIdentifiers(BaseModel):
    """Identifiers used to resolve + verify the patient. Strong match requires
    emr_id, or dob + exact first_name + last_name, or dob + phone."""

    model_config = ConfigDict(extra="forbid")
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    dob: Optional[str] = None                # ISO 8601 YYYY-MM-DD
    phone: Optional[str] = None
    email: Optional[str] = None
    emr_id: Optional[str] = None             # SIS patient_id or Svigg acct


class ApprovalMixin(BaseModel):
    """Write requests (risk_level >= 2) must carry an approval token. The bridge
    checks it before dispatching to a write adapter; a missing token is a hard
    block, never a silent execute."""

    model_config = ConfigDict(extra="forbid")
    approval_token: Optional[str] = Field(
        default=None,
        description="Single-use approval token issued after human approval.",
    )


# ---- Read requests --------------------------------------------------------
class FindPatientRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient: PatientIdentifiers
    query: Optional[str] = Field(default=None, description="Free-text fallback query.")


class PatientRefRequest(BaseModel):
    """Demographics / notes / appointments / referral / documents reads."""
    model_config = ConfigDict(extra="forbid")
    patient: PatientIdentifiers


# ---- Write requests -------------------------------------------------------
class BookAppointmentRequest(ApprovalMixin):
    patient: PatientIdentifiers
    date: str = Field(description="MM/DD/YYYY per the Svigg grid.")
    time: str
    provider: str
    office: str
    cpt00: str = Field(description="CPT code — REQUIRED; blank => silent Svigg bounce.")
    duration_minutes: int = 15
    note: Optional[str] = None


class CancelAppointmentRequest(ApprovalMixin):
    patient: PatientIdentifiers
    encounter_id: Optional[str] = Field(
        default=None, description="Svigg enc — preferred, keyed cancel path."
    )
    appointment_date: Optional[str] = None
    appointment_time: Optional[str] = None
    cancel_reason: Optional[str] = None


class CreatePatientRequest(ApprovalMixin):
    demographics: dict = Field(description="At minimum last_name + first_name.")
    dry_run: bool = True
    confirm_unverified: bool = False


class UpdateUnsignedNoteRequest(ApprovalMixin):
    patient: PatientIdentifiers
    note_id: str
    content: str


class AppendSignedNoteAddendumRequest(ApprovalMixin):
    patient: PatientIdentifiers
    note_id: str
    addendum_text: str
    provider_approved: bool = False

"""Pydantic v2 schemas for the API surface.

ActionIntent mirrors packages/action-registry/action_intent.schema.json (the
strict shape the AI parser is constrained to). Output models mirror the columns
in db/schema.sql (owned by the schema sibling agent).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Action intent (parser output / execution input) — matches action_intent.schema
# --------------------------------------------------------------------------- #
class PatientIdentifiers(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    dob: Optional[str] = None  # ISO YYYY-MM-DD
    phone: Optional[str] = None
    email: Optional[str] = None
    emr_id: Optional[str] = None  # exact SIS patient_id or Svigg acct


class ActionIntent(BaseModel):
    """Structured, reviewable representation of a staff request mapped onto
    exactly one Action-Registry action. A proposal only — never executed until
    a human approves it."""

    action_name: str
    target_system: str  # sis | svigg | both | unknown
    risk_level: int  # 1 read, 2 schedule, 3 patient, 4 notes
    requires_approval: bool
    patient_identifiers: PatientIdentifiers = Field(default_factory=PatientIdentifiers)
    action_inputs: Dict[str, Any] = Field(default_factory=dict)
    missing_fields: List[str] = Field(default_factory=list)
    reason: str = ""


# --------------------------------------------------------------------------- #
# Tasks (tasks table: title/description/status/created_by/patient_id)
# --------------------------------------------------------------------------- #
class TaskCreate(BaseModel):
    # The free-text staff request. Stored as description; title is derived.
    prompt: str = Field(min_length=1)
    created_by: Optional[str] = None  # user uuid
    patient_id: Optional[str] = None  # patient uuid
    title: Optional[str] = None


class TaskOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    status: str
    created_by: Optional[str] = None
    patient_id: Optional[str] = None
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
    implemented: bool = True


class ParseResponse(BaseModel):
    intent: ActionIntent
    validation: RegistryValidation


# --------------------------------------------------------------------------- #
# Approvals (approvals + action_runs tables)
# --------------------------------------------------------------------------- #
class SubmitForApprovalRequest(BaseModel):
    intent: ActionIntent
    task_id: Optional[str] = None
    requested_by: Optional[str] = None  # user uuid
    staff_prompt: Optional[str] = None
    patient_id: Optional[str] = None


class ApprovalDecision(BaseModel):
    approver_user_id: Optional[str] = None  # user uuid; v1 any staff may approve
    reason: Optional[str] = None


class ApprovalOut(BaseModel):
    id: str
    action_run_id: str
    action_name: str
    status: str  # approval_status enum
    risk_level: int
    proposed_action: Dict[str, Any]
    requested_by: Optional[str] = None
    approver_id: Optional[str] = None
    decision_reason: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Action runs (action_runs table)
# --------------------------------------------------------------------------- #
class ActionRunOut(BaseModel):
    id: str
    action_name: Optional[str] = None
    target_system: str
    risk_level: int
    status: str  # run_status enum
    verified: Optional[bool] = None
    requires_human_review: bool = False
    workflow_run_id: Optional[str] = None
    task_id: Optional[str] = None
    patient_id: Optional[str] = None
    screenshot_id: Optional[str] = None
    trace_id: Optional[str] = None
    failure_reason: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Audit (audit_logs table — hash chain computed by DB trigger)
# --------------------------------------------------------------------------- #
class AuditEntryOut(BaseModel):
    id: int
    actor_label: str
    action: str
    target_system: str
    result: str
    failure_reason: Optional[str] = None
    prev_hash: str
    entry_hash: str
    created_at: datetime


# --------------------------------------------------------------------------- #
# Patients (patients table + patient_external_ids)
# --------------------------------------------------------------------------- #
class PatientOut(BaseModel):
    id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    dob: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    is_test_patient: bool = False
    external_ids: Dict[str, str] = Field(default_factory=dict)
    created_at: datetime

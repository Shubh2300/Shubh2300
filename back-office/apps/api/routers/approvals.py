"""Approvals router.

Endpoints:
  * POST /approvals              — submit a proposed intent for approval
  * GET  /approvals/pending      — list pending approvals (shared queue)
  * POST /approvals/{id}/approve — approve (records who/when) + start workflow
  * POST /approvals/{id}/reject  — reject (records who/when)

v1 policy (decision #11): any staff member may approve anything. We still
record the approver's user id and decision time on every decision.

Approving an intent starts the Temporal ExecuteActionWorkflow. Before starting
we re-validate against the Action Registry (defense in depth) and refuse to
start an unknown/invalid action.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException

from config import get_settings
from deps import get_registry
from repositories import (
    PostgresApprovalStore,
    create_action_run,
    set_action_run_workflow,
)
from schemas import ApprovalDecision, ApprovalOut, SubmitForApprovalRequest
from services.approvals import ApprovalService, InvalidTransition
from services.audit import AuditWriter
from services.temporal_client import start_execute_action_workflow
from db import get_connection

logger = logging.getLogger("backoffice.approvals")

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _service() -> ApprovalService:
    return ApprovalService(PostgresApprovalStore())


def _audit() -> AuditWriter:
    return AuditWriter(get_connection, pepper=get_settings().audit_pepper)


def _to_out(record) -> ApprovalOut:
    return ApprovalOut(
        id=record.id,
        task_id=record.task_id,
        action=record.action,
        state=record.state,
        intent=record.intent,
        submitted_by=record.submitted_by,
        approver_user_id=record.approver_user_id,
        decided_at=record.decided_at,
        created_at=record.created_at,
    )


@router.post("", response_model=ApprovalOut, status_code=201)
def submit_for_approval(payload: SubmitForApprovalRequest) -> ApprovalOut:
    intent_dict = payload.intent.model_dump()

    # Reject unknown/invalid actions at submit time.
    result = get_registry().validate(intent_dict)
    if not result.ok:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "intent_rejected",
                "unknown_action": result.unknown_action,
                "missing_inputs": result.missing_inputs,
                "errors": result.errors,
            },
        )

    approval_id = str(uuid.uuid4())
    record = _service().submit(
        approval_id=approval_id,
        action=payload.intent.action,
        intent=intent_dict,
        submitted_by=payload.submitted_by,
        task_id=payload.task_id,
    )
    _audit().append(
        actor=payload.submitted_by,
        action="APPROVAL_SUBMITTED",
        result_summary=f"pending_approval action={payload.intent.action}",
        intent=payload.intent.action,
        patient_id=_patient_id(intent_dict),
    )
    return _to_out(record)


@router.get("/pending", response_model=list[ApprovalOut])
def list_pending() -> list[ApprovalOut]:
    return [_to_out(r) for r in _service().list_pending()]


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
async def approve(approval_id: str, decision: ApprovalDecision) -> ApprovalOut:
    settings = get_settings()
    service = _service()

    try:
        record = service.approve(approval_id, decision.approver_user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found")
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Re-validate before execution (defense in depth).
    result = get_registry().validate(record.intent)
    if not result.ok:
        raise HTTPException(
            status_code=422,
            detail={"code": "intent_rejected", "errors": result.errors},
        )

    # Create the action_run, then start the workflow, then mark executing.
    action_run_id = str(uuid.uuid4())
    create_action_run(
        action_run_id=action_run_id,
        approval_id=record.id,
        action=record.action,
        state="executing",
    )

    workflow_id = await start_execute_action_workflow(
        address=settings.temporal_address,
        namespace=settings.temporal_namespace,
        task_queue=settings.temporal_task_queue,
        approval_id=record.id,
        action_run_id=action_run_id,
        intent=record.intent,
    )
    set_action_run_workflow(action_run_id, workflow_id)
    record = service.mark_executing(approval_id)

    _audit().append(
        actor=decision.approver_user_id,
        action="APPROVAL_APPROVED",
        result_summary=f"executing workflow={workflow_id}",
        intent=record.action,
        patient_id=_patient_id(record.intent),
    )
    logger.info("approval.approved id=%s run=%s", approval_id, action_run_id)
    return _to_out(record)


@router.post("/{approval_id}/reject", response_model=ApprovalOut)
def reject(approval_id: str, decision: ApprovalDecision) -> ApprovalOut:
    service = _service()
    try:
        record = service.reject(
            approval_id, decision.approver_user_id, decision.reason
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found")
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    _audit().append(
        actor=decision.approver_user_id,
        action="APPROVAL_REJECTED",
        result_summary="rejected",
        intent=record.action,
        patient_id=_patient_id(record.intent),
    )
    return _to_out(record)


def _patient_id(intent: dict) -> str | None:
    patient = intent.get("patient") or {}
    if isinstance(patient, dict):
        return patient.get("patient_id")
    return None

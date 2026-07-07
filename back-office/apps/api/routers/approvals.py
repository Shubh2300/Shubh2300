"""Approvals router.

Endpoints:
  * POST /approvals              — submit a proposed intent for approval
                                    (creates an action_run + a pending approval)
  * GET  /approvals/pending      — list pending approvals (shared queue)
  * POST /approvals/{id}/approve — approve (records who/when) + start workflow
  * POST /approvals/{id}/reject  — reject (records who/when)

v1 policy (decision #11): any staff member may approve anything. We still
record the approver's user id and the decision time on every decision.

Approving re-validates against the Action Registry, creates a workflow_run,
starts the Temporal ExecuteActionWorkflow, and moves the run to 'executing'.
"""

from __future__ import annotations

import hashlib
import logging
import secrets

from fastapi import APIRouter, HTTPException

from config import get_settings
from db import get_connection
from deps import get_registry
from repositories import (
    approve_approval,
    create_run_and_approval,
    get_approval,
    list_approvals,
    list_pending_approvals,
    reject_approval,
    set_workflow_temporal_ids,
    start_run_execution,
)
from schemas import ApprovalDecision, ApprovalOut, SubmitForApprovalRequest
from services.audit import AuditWriter
from services.temporal_client import start_execute_action_workflow

logger = logging.getLogger("backoffice.approvals")

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _audit() -> AuditWriter:
    return AuditWriter(get_connection)


def _org_id() -> str:
    from repositories import resolve_org_id

    with get_connection() as conn:
        with conn.cursor() as cur:
            return resolve_org_id(cur)


def _out(row: dict) -> ApprovalOut:
    return ApprovalOut(**row)


@router.post("", response_model=ApprovalOut, status_code=201)
def submit_for_approval(payload: SubmitForApprovalRequest) -> ApprovalOut:
    intent_dict = payload.intent.model_dump()

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

    try:
        row = create_run_and_approval(
            intent=intent_dict,
            staff_prompt=payload.staff_prompt,
            requested_by=payload.requested_by,
            task_id=payload.task_id,
            patient_id=payload.patient_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail={"code": "unknown_action", "message": str(exc)})

    _audit().append(
        organization_id=_org_id(),
        actor_label=payload.requested_by or "staff",
        actor_user_id=payload.requested_by,
        action=payload.intent.action_name,
        target_system=payload.intent.target_system,
        result="needs_human_review",
        action_run_id=row["action_run_id"],
        staff_prompt=payload.staff_prompt,
        parsed_intent=intent_dict,
        patient_identifiers_used=intent_dict.get("patient_identifiers"),
    )
    return _out(row)


@router.get("", response_model=list[ApprovalOut])
def list_all() -> list[ApprovalOut]:
    """Shared approval queue + history (newest first). The web filters the
    pending ones for the actionable queue and counts them on the dashboard."""
    return [_out(r) for r in list_approvals()]


@router.get("/pending", response_model=list[ApprovalOut])
def list_pending() -> list[ApprovalOut]:
    return [_out(r) for r in list_pending_approvals()]


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
async def approve(approval_id: str, decision: ApprovalDecision) -> ApprovalOut:
    settings = get_settings()

    existing = get_approval(approval_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="approval not found")

    # Re-validate before execution (defense in depth).
    result = get_registry().validate(existing["proposed_action"])
    if not result.ok:
        raise HTTPException(
            status_code=422,
            detail={"code": "intent_rejected", "errors": result.errors},
        )

    try:
        row = approve_approval(approval_id, decision.approver_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail="approval not found")

    intent = row["proposed_action"]
    action_run_id = row["action_run_id"]

    # Create workflow_run, mark run executing, then start Temporal.
    workflow_run_id = start_run_execution(
        action_run_id=action_run_id,
        intent=intent,
        initiated_by=decision.approver_user_id,
        patient_id=None,
    )
    _, temporal_run_id = await start_execute_action_workflow(
        address=settings.temporal_address,
        namespace=settings.temporal_namespace,
        task_queue=settings.temporal_task_queue,
        workflow_id=workflow_run_id,
        approval_id=approval_id,
        action_run_id=action_run_id,
        intent=intent,
    )
    set_workflow_temporal_ids(workflow_run_id, workflow_run_id, temporal_run_id)

    _audit().append(
        organization_id=_org_id(),
        actor_label=decision.approver_user_id or "staff",
        actor_user_id=decision.approver_user_id,
        approver_label=decision.approver_user_id,
        action=row["action_name"],
        target_system=intent.get("target_system", "unknown"),
        result="success",
        action_run_id=action_run_id,
        parsed_intent=intent,
    )
    logger.info("approval.approved id=%s run=%s", approval_id, action_run_id)
    return _out(row)


@router.post("/{approval_id}/reject", response_model=ApprovalOut)
def reject(approval_id: str, decision: ApprovalDecision) -> ApprovalOut:
    try:
        row = reject_approval(approval_id, decision.approver_user_id, decision.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail="approval not found")

    _audit().append(
        organization_id=_org_id(),
        actor_label=decision.approver_user_id or "staff",
        actor_user_id=decision.approver_user_id,
        approver_label=decision.approver_user_id,
        action=row["action_name"],
        target_system=row["proposed_action"].get("target_system", "unknown"),
        result="failed",
        action_run_id=row["action_run_id"],
        failure_reason=decision.reason or "rejected by approver",
    )
    return _out(row)

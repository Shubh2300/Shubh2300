"""Action runs router — status/detail incl. screenshot_id / trace_id.

action_runs columns include: id, action_registry_id, target_system, risk_level,
status (run_status), verified, requires_human_review, workflow_run_id, task_id,
patient_id, screenshot_id, trace_id, failure_reason, result, created_at,
updated_at. screenshot_id/trace_id are UUID FKs to screenshots/traces; we
resolve them to the bridge-generated keys the UI can display.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException

from db import get_connection
from schemas import ActionRunOut

router = APIRouter(prefix="/action_runs", tags=["action_runs"])

_SELECT = """
    SELECT ar.id, reg.action_name, ar.target_system, ar.risk_level, ar.status,
           ar.verified, ar.requires_human_review, ar.workflow_run_id,
           ar.task_id, ar.patient_id, ar.failure_reason, ar.result,
           sc.screenshot_key AS screenshot_id, tr.trace_key AS trace_id,
           ar.created_at, ar.updated_at
    FROM action_runs ar
    JOIN action_registry reg ON reg.id = ar.action_registry_id
    LEFT JOIN screenshots sc ON sc.id = ar.screenshot_id
    LEFT JOIN traces tr ON tr.id = ar.trace_id
"""


def _to_out(row: dict) -> ActionRunOut:
    return ActionRunOut(
        id=row["id"],
        action_name=row.get("action_name"),
        target_system=row["target_system"],
        risk_level=int(row["risk_level"]),
        status=row["status"],
        verified=row.get("verified"),
        requires_human_review=row.get("requires_human_review", False),
        workflow_run_id=row.get("workflow_run_id"),
        task_id=row.get("task_id"),
        patient_id=row.get("patient_id"),
        screenshot_id=row.get("screenshot_id"),
        trace_id=row.get("trace_id"),
        failure_reason=row.get("failure_reason"),
        result=row.get("result"),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


@router.get("", response_model=List[ActionRunOut])
def list_action_runs(status: str | None = None, limit: int = 100) -> List[ActionRunOut]:
    limit = max(1, min(limit, 500))
    with get_connection() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    _SELECT + " WHERE ar.status = %s ORDER BY ar.created_at DESC LIMIT %s",
                    (status, limit),
                )
            else:
                cur.execute(_SELECT + " ORDER BY ar.created_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
    return [_to_out(r) for r in rows]


@router.get("/{action_run_id}", response_model=ActionRunOut)
def get_action_run(action_run_id: str) -> ActionRunOut:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " WHERE ar.id = %s", (action_run_id,))
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="action run not found")
    return _to_out(row)

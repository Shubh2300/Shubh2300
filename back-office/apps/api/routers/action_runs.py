"""Action runs router — status/detail incl. screenshot_id / trace_id.

Assumed ``action_runs`` table (owned by db/schema.sql):
    id, approval_id, workflow_id, action, state, status, verified,
    screenshot_id, trace_id, failure_reason, warnings (jsonb),
    created_at, updated_at
"""

from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, HTTPException

from db import get_connection
from schemas import ActionRunOut

router = APIRouter(prefix="/action-runs", tags=["action_runs"])

_SELECT = """
    SELECT id, approval_id, workflow_id, action, state, status, verified,
           screenshot_id, trace_id, failure_reason, warnings,
           created_at, updated_at
    FROM action_runs
"""


def _to_out(row: dict) -> ActionRunOut:
    warnings = row.get("warnings")
    if isinstance(warnings, str):
        warnings = json.loads(warnings)
    return ActionRunOut(
        id=row["id"],
        approval_id=row.get("approval_id"),
        workflow_id=row.get("workflow_id"),
        action=row["action"],
        state=row["state"],
        status=row.get("status"),
        verified=row.get("verified"),
        screenshot_id=row.get("screenshot_id"),
        trace_id=row.get("trace_id"),
        failure_reason=row.get("failure_reason"),
        warnings=warnings or [],
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


@router.get("", response_model=List[ActionRunOut])
def list_action_runs(state: str | None = None, limit: int = 100) -> List[ActionRunOut]:
    limit = max(1, min(limit, 500))
    with get_connection() as conn:
        with conn.cursor() as cur:
            if state:
                cur.execute(
                    _SELECT + " WHERE state = %s ORDER BY created_at DESC LIMIT %s",
                    (state, limit),
                )
            else:
                cur.execute(
                    _SELECT + " ORDER BY created_at DESC LIMIT %s", (limit,)
                )
            rows = cur.fetchall()
    return [_to_out(r) for r in rows]


@router.get("/{action_run_id}", response_model=ActionRunOut)
def get_action_run(action_run_id: str) -> ActionRunOut:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " WHERE id = %s", (action_run_id,))
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="action run not found")
    return _to_out(row)

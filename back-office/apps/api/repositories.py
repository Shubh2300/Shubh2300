"""Postgres-backed repositories (psycopg3).

These adapt the pure service layer (services/approvals.py, services/audit.py)
to the concrete schema owned by db/schema.sql. Assumed tables/columns are
documented inline. Kept separate from the pure logic so unit tests never touch
a database.
"""

from __future__ import annotations

import json
from typing import List, Optional

from psycopg.types.json import Json

from db import get_connection
from services.approvals import ApprovalRecord, ApprovalState


def _row_to_record(row: dict) -> ApprovalRecord:
    intent = row["intent"]
    if isinstance(intent, str):
        intent = json.loads(intent)
    return ApprovalRecord(
        id=row["id"],
        action=row["action"],
        intent=intent or {},
        state=ApprovalState(row["state"]),
        task_id=row.get("task_id"),
        submitted_by=row.get("submitted_by"),
        approver_user_id=row.get("approver_user_id"),
        decided_at=row.get("decided_at"),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


_SELECT = """
    SELECT id, task_id, action, state, intent, submitted_by,
           approver_user_id, decided_at, created_at, updated_at
    FROM approvals
"""


class PostgresApprovalStore:
    """Implements the ApprovalStore protocol against the ``approvals`` table."""

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO approvals
                        (id, task_id, action, state, intent, submitted_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, task_id, action, state, intent, submitted_by,
                              approver_user_id, decided_at, created_at, updated_at
                    """,
                    (
                        record.id,
                        record.task_id,
                        record.action,
                        record.state.value,
                        Json(record.intent),
                        record.submitted_by,
                    ),
                )
                row = cur.fetchone()
        return _row_to_record(row)

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT + " WHERE id = %s", (approval_id,))
                row = cur.fetchone()
        return _row_to_record(row) if row else None

    def update(self, record: ApprovalRecord) -> ApprovalRecord:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE approvals
                    SET state = %s,
                        approver_user_id = %s,
                        decided_at = %s,
                        updated_at = now()
                    WHERE id = %s
                    RETURNING id, task_id, action, state, intent, submitted_by,
                              approver_user_id, decided_at, created_at, updated_at
                    """,
                    (
                        record.state.value,
                        record.approver_user_id,
                        record.decided_at,
                        record.id,
                    ),
                )
                row = cur.fetchone()
        return _row_to_record(row)

    def list_by_state(self, state: ApprovalState) -> List[ApprovalRecord]:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _SELECT + " WHERE state = %s ORDER BY created_at ASC",
                    (state.value,),
                )
                rows = cur.fetchall()
        return [_row_to_record(r) for r in rows]


def create_action_run(
    *, action_run_id: str, approval_id: str, action: str, state: str
) -> dict:
    """Insert a fresh action_run row. Assumed ``action_runs`` table columns:
    id, approval_id, workflow_id, action, state, status, verified,
    screenshot_id, trace_id, failure_reason, warnings (jsonb), created_at,
    updated_at."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO action_runs (id, approval_id, action, state)
                VALUES (%s, %s, %s, %s)
                RETURNING id, approval_id, workflow_id, action, state, status,
                          verified, screenshot_id, trace_id, failure_reason,
                          warnings, created_at, updated_at
                """,
                (action_run_id, approval_id, action, state),
            )
            row = cur.fetchone()
    return row


def set_action_run_workflow(action_run_id: str, workflow_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE action_runs SET workflow_id = %s, updated_at = now() "
                "WHERE id = %s",
                (workflow_id, action_run_id),
            )

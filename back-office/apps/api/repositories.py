"""Postgres repositories (psycopg3) mapping the API onto db/schema.sql.

Kept separate from the pure service logic (services/*) so unit tests never
touch a database. All rows are scoped to a single organization (single-office
deployment): the org id comes from ORGANIZATION_ID if set, else the single
organizations row.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from psycopg.types.json import Json

from config import get_settings
from db import get_connection


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #
def resolve_org_id(cur) -> str:
    override = get_settings().organization_id
    if override:
        return override
    cur.execute("SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("no organization row exists; seed one first")
    return row["id"]


# --------------------------------------------------------------------------- #
# Action registry lookup
# --------------------------------------------------------------------------- #
def get_action_registry(cur, action_name: str) -> Optional[dict]:
    cur.execute(
        """
        SELECT id, action_name, target_system, risk_level, requires_approval,
               implementation_status
        FROM action_registry WHERE action_name = %s
        """,
        (action_name,),
    )
    return cur.fetchone()


# --------------------------------------------------------------------------- #
# Approvals view helper (approvals joined to its action_run for action_name)
# --------------------------------------------------------------------------- #
_APPROVAL_SELECT = """
    SELECT a.id, a.action_run_id, a.status, a.risk_level, a.proposed_action,
           a.requested_by, a.approver_id, a.decision_reason, a.decided_at,
           a.created_at, ar.action_registry_id, reg.action_name
    FROM approvals a
    JOIN action_runs ar ON ar.id = a.action_run_id
    JOIN action_registry reg ON reg.id = ar.action_registry_id
"""


def _approval_row(row: dict) -> dict:
    proposed = row["proposed_action"]
    return {
        "id": row["id"],
        "action_run_id": row["action_run_id"],
        "action_name": row["action_name"],
        "status": row["status"],
        "risk_level": int(row["risk_level"]),
        "proposed_action": proposed or {},
        "requested_by": row.get("requested_by"),
        "approver_id": row.get("approver_id"),
        "decision_reason": row.get("decision_reason"),
        "decided_at": row.get("decided_at"),
        "created_at": row["created_at"],
    }


def create_run_and_approval(
    *,
    intent: Dict[str, Any],
    staff_prompt: Optional[str],
    requested_by: Optional[str],
    task_id: Optional[str],
    patient_id: Optional[str],
) -> dict:
    """Create the action_run (awaiting_approval) and its approval (pending).

    Raises KeyError if the action is not in the registry.
    """
    action_name = intent.get("action_name")
    with get_connection() as conn:
        with conn.cursor() as cur:
            org_id = resolve_org_id(cur)
            reg = get_action_registry(cur, action_name)
            if reg is None:
                raise KeyError(f"unknown action '{action_name}'")

            cur.execute(
                """
                INSERT INTO action_runs
                    (organization_id, action_registry_id, task_id, patient_id,
                     requested_by, target_system, risk_level, status,
                     staff_prompt, parsed_intent, action_inputs,
                     patient_identifiers_used)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, 'awaiting_approval',
                     %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    org_id,
                    reg["id"],
                    task_id,
                    patient_id,
                    requested_by,
                    reg["target_system"],
                    reg["risk_level"],
                    staff_prompt,
                    Json(intent),
                    Json(intent.get("action_inputs") or {}),
                    Json(intent.get("patient_identifiers") or {}),
                ),
            )
            action_run_id = cur.fetchone()["id"]

            cur.execute(
                """
                INSERT INTO approvals
                    (organization_id, action_run_id, requested_by, status,
                     risk_level, proposed_action)
                VALUES (%s, %s, %s, 'pending', %s, %s)
                RETURNING id
                """,
                (
                    org_id,
                    action_run_id,
                    requested_by,
                    reg["risk_level"],
                    Json(intent),
                ),
            )
            approval_id = cur.fetchone()["id"]

            cur.execute(_APPROVAL_SELECT + " WHERE a.id = %s", (approval_id,))
            row = cur.fetchone()
    return _approval_row(row)


def list_pending_approvals() -> List[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _APPROVAL_SELECT + " WHERE a.status = 'pending' "
                "ORDER BY a.created_at ASC"
            )
            rows = cur.fetchall()
    return [_approval_row(r) for r in rows]


def get_approval(approval_id: str) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_APPROVAL_SELECT + " WHERE a.id = %s", (approval_id,))
            row = cur.fetchone()
    return _approval_row(row) if row else None


def approve_approval(approval_id: str, approver_id: Optional[str]) -> Optional[dict]:
    """Set approval -> approved and its run -> approved. Returns None if the
    approval is missing; raises ValueError if not pending."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, action_run_id FROM approvals WHERE id = %s",
                (approval_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if row["status"] != "pending":
                raise ValueError(f"approval is '{row['status']}', not pending")

            cur.execute(
                """
                UPDATE approvals
                SET status = 'approved', approver_id = %s, decided_at = now()
                WHERE id = %s
                """,
                (approver_id, approval_id),
            )
            cur.execute(
                "UPDATE action_runs SET status = 'approved' WHERE id = %s",
                (row["action_run_id"],),
            )
            cur.execute(_APPROVAL_SELECT + " WHERE a.id = %s", (approval_id,))
            out = cur.fetchone()
    return _approval_row(out)


def reject_approval(
    approval_id: str, approver_id: Optional[str], reason: Optional[str]
) -> Optional[dict]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, action_run_id FROM approvals WHERE id = %s",
                (approval_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if row["status"] != "pending":
                raise ValueError(f"approval is '{row['status']}', not pending")

            cur.execute(
                """
                UPDATE approvals
                SET status = 'rejected', approver_id = %s,
                    decision_reason = %s, decided_at = now()
                WHERE id = %s
                """,
                (approver_id, reason, approval_id),
            )
            cur.execute(
                "UPDATE action_runs SET status = 'cancelled' WHERE id = %s",
                (row["action_run_id"],),
            )
            cur.execute(_APPROVAL_SELECT + " WHERE a.id = %s", (approval_id,))
            out = cur.fetchone()
    return _approval_row(out)


# --------------------------------------------------------------------------- #
# Workflow run wiring (Temporal correlation)
# --------------------------------------------------------------------------- #
def start_run_execution(
    *, action_run_id: str, intent: Dict[str, Any], initiated_by: Optional[str],
    patient_id: Optional[str],
) -> str:
    """Create a workflow_runs row, link the action_run, mark both executing.
    Returns the workflow_run id (used as the Temporal workflow id)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            org_id = resolve_org_id(cur)
            cur.execute(
                """
                INSERT INTO workflow_runs
                    (organization_id, workflow_type, initiated_by, patient_id,
                     status, input, started_at)
                VALUES (%s, 'execute_action', %s, %s, 'executing', %s, now())
                RETURNING id
                """,
                (org_id, initiated_by, patient_id, Json(intent)),
            )
            workflow_run_id = cur.fetchone()["id"]
            cur.execute(
                """
                UPDATE action_runs
                SET status = 'executing', workflow_run_id = %s, started_at = now()
                WHERE id = %s
                """,
                (workflow_run_id, action_run_id),
            )
    return workflow_run_id


def set_workflow_temporal_ids(
    workflow_run_id: str, temporal_workflow_id: str, temporal_run_id: Optional[str]
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_runs
                SET temporal_workflow_id = %s, temporal_run_id = %s
                WHERE id = %s
                """,
                (temporal_workflow_id, temporal_run_id, workflow_run_id),
            )

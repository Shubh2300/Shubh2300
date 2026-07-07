"""Audit router — read-only listing of the append-only, hash-chained log.

Writes go only through services/audit.py::AuditWriter. This router never
mutates the log. No PHI is returned: patient identifiers are stored hashed.

Assumed ``audit_logs`` table (owned by db/schema.sql):
    id, ts, actor, action, intent, result_summary,
    target_patient_id_hash, prev_hash, entry_hash
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter

from db import get_connection
from schemas import AuditEntryOut

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=List[AuditEntryOut])
def list_audit(limit: int = 100, offset: int = 0) -> List[AuditEntryOut]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ts, actor, action, intent, result_summary,
                       prev_hash, entry_hash
                FROM audit_logs
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
    return [AuditEntryOut(**r) for r in rows]

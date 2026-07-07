"""Audit router — read-only listing of the append-only, hash-chained log.

Writes go only through services/audit.py::AuditWriter (and the DB trigger that
computes the hash chain). This router never mutates the log. No PHI is
returned: only identifiers-used are stored, and this listing omits them.

audit_logs columns: id, actor_label, action, target_system, result,
failure_reason, prev_hash, entry_hash, created_at, ...
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
                SELECT id, actor_label, action, target_system, result,
                       failure_reason, prev_hash, entry_hash, created_at
                FROM audit_logs
                ORDER BY id DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
    return [AuditEntryOut(**r) for r in rows]

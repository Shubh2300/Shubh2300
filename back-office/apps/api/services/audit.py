"""Append-only audit writer for the ``audit_logs`` table.

IMPORTANT: the hash chain (``prev_hash`` + ``entry_hash``) is computed by a
BEFORE INSERT trigger in db/schema.sql (``audit_logs_hash_chain``) so the
application cannot forge or forget it. This writer therefore only inserts the
semantic columns; it never sets the hashes.

``compute_entry_hash`` below mirrors the trigger's canonical serialization
(pipe-joined field order, load-bearing) so the read/verify side can validate a
chain in Python and stay consistent with the DB.

No PHI in logs: only identifiers-used (already minimized upstream) land in
``patient_identifiers_used``; nothing here is written to application logs.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

GENESIS_PREV_HASH = "0" * 64

# Field order MUST match audit_logs_hash_chain() in db/schema.sql exactly.
_CANON_FIELDS = (
    "actor_label",
    "staff_prompt",
    "parsed_intent",
    "approver_label",
    "target_system",
    "action",
    "patient_identifiers_used",
    "match_result",
    "matched_by",
    "pre_action_state",
    "post_action_state",
    "screenshot_id",
    "trace_id",
    "result",
    "failure_reason",
    "created_at",
)


def _canon_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v)


def compute_entry_hash(row: Dict[str, Any], prev_hash: str) -> str:
    """Mirror the DB trigger's SHA-256 over the canonical field set + prev_hash.

    ``row`` values should already be rendered the way Postgres casts them
    (``parsed_intent`` as JSON text, ``created_at`` as its text form, etc.).
    """
    parts = [_canon_value(row.get(f)) for f in _CANON_FIELDS]
    parts.append(prev_hash)
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chain(entries: List[Dict[str, Any]]) -> tuple[bool, Optional[Any]]:
    """Verify an ordered (ascending id) list of audit rows. Returns
    (ok, first_broken_id). Each row must expose the raw column values plus
    ``prev_hash``/``entry_hash``/``created_at`` (as text)."""
    prev = GENESIS_PREV_HASH
    for entry in entries:
        if entry.get("prev_hash") != prev:
            return False, entry.get("id")
        if entry.get("entry_hash") != compute_entry_hash(entry, prev):
            return False, entry.get("id")
        prev = entry["entry_hash"]
    return True, None


class AuditWriter:
    """Append-only writer. ``connection_factory`` yields a psycopg connection
    (e.g. ``db.get_connection``)."""

    def __init__(self, connection_factory) -> None:
        self._conn_factory = connection_factory

    def append(
        self,
        *,
        organization_id: str,
        actor_label: str,
        action: str,
        target_system: str,
        result: str,
        action_run_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        approver_label: Optional[str] = None,
        staff_prompt: Optional[str] = None,
        parsed_intent: Optional[Dict[str, Any]] = None,
        patient_identifiers_used: Optional[Dict[str, Any]] = None,
        failure_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        from psycopg.types.json import Json

        with self._conn_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs
                        (organization_id, action_run_id, actor_user_id,
                         actor_label, staff_prompt, parsed_intent,
                         approver_label, target_system, action,
                         patient_identifiers_used, result, failure_reason)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, prev_hash, entry_hash
                    """,
                    (
                        organization_id,
                        action_run_id,
                        actor_user_id,
                        actor_label,
                        staff_prompt,
                        Json(parsed_intent) if parsed_intent is not None else None,
                        approver_label,
                        target_system,
                        action,
                        Json(patient_identifiers_used)
                        if patient_identifiers_used is not None
                        else None,
                        result,
                        failure_reason,
                    ),
                )
                row = cur.fetchone()
        return {
            "id": row["id"],
            "prev_hash": row["prev_hash"],
            "entry_hash": row["entry_hash"],
        }

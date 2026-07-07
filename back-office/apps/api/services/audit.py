"""Append-only, hash-chained audit writer.

Consistent with the toolbox's ``audit_log.py`` (n8n-office): every entry stores
``prev_hash`` (the previous entry's ``entry_hash``) and ``entry_hash`` =
SHA-256 over a canonical serialization of the entry fields plus ``prev_hash``.
Any silent edit/deletion of an earlier row breaks the chain from that point.

NO PHI in the log: patient identifiers are hashed with a per-install pepper
before storage, never written in the clear. Counts/ids only.

The ``audit_logs`` table (owned by db/schema.sql) is assumed to expose at least:
    id, ts, actor, action, intent, result_summary,
    target_patient_id_hash, prev_hash, entry_hash

The pure hashing helpers below have no I/O and are unit-testable. The
``AuditWriter`` takes an injected connection factory so it stays decoupled from
psycopg specifics.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

GENESIS_PREV_HASH = "0" * 64

# Fixed field order for the hash preimage. Order is load-bearing — changing it
# invalidates every existing chain.
_HASHED_FIELDS = (
    "ts",
    "actor",
    "action",
    "intent",
    "target_patient_id_hash",
    "result_summary",
)


def hash_patient_id(raw_id: Optional[str], pepper: Optional[str]) -> Optional[str]:
    """SHA-256(pepper || raw_id). Same patient -> same hash (groupable) but the
    raw identifier is never stored. Returns None for non-patient events."""
    if not raw_id:
        return None
    key = (pepper or "").encode("utf-8")
    return hmac.new(key, raw_id.encode("utf-8"), hashlib.sha256).hexdigest()


def compute_entry_hash(entry: Dict[str, Any], prev_hash: str) -> str:
    """Canonical, deterministic hash over the entry + prev_hash."""
    preimage = {k: entry.get(k) for k in _HASHED_FIELDS}
    preimage["prev_hash"] = prev_hash
    canonical = json.dumps(
        preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chain(entries: list[Dict[str, Any]]) -> tuple[bool, Optional[Any]]:
    """Verify an ordered list of entries. Returns (ok, first_broken_id)."""
    prev = GENESIS_PREV_HASH
    for entry in entries:
        if entry.get("prev_hash") != prev:
            return False, entry.get("id")
        expected = compute_entry_hash(entry, entry.get("prev_hash", prev))
        if entry.get("entry_hash") != expected:
            return False, entry.get("id")
        prev = entry["entry_hash"]
    return True, None


class AuditWriter:
    """Append-only writer over the ``audit_logs`` table.

    ``connection_factory`` is a context manager yielding a psycopg connection
    (e.g. ``db.get_connection``). Kept injectable for testing.
    """

    def __init__(self, connection_factory, pepper: Optional[str] = None) -> None:
        self._conn_factory = connection_factory
        self._pepper = pepper

    def append(
        self,
        *,
        actor: str,
        action: str,
        result_summary: str,
        intent: Optional[str] = None,
        patient_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        target_hash = hash_patient_id(patient_id, self._pepper)
        entry = {
            "ts": ts,
            "actor": actor,
            "action": action,
            "intent": intent,
            "target_patient_id_hash": target_hash,
            "result_summary": result_summary,
        }

        with self._conn_factory() as conn:
            with conn.cursor() as cur:
                # Serialize against concurrent appends so the chain stays linear.
                cur.execute("LOCK TABLE audit_logs IN EXCLUSIVE MODE")
                cur.execute(
                    "SELECT entry_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                prev_hash = (
                    row["entry_hash"] if row else GENESIS_PREV_HASH
                )
                entry_hash = compute_entry_hash(entry, prev_hash)
                cur.execute(
                    """
                    INSERT INTO audit_logs
                        (ts, actor, action, intent, target_patient_id_hash,
                         result_summary, prev_hash, entry_hash)
                    VALUES
                        (%(ts)s, %(actor)s, %(action)s, %(intent)s,
                         %(target_patient_id_hash)s, %(result_summary)s,
                         %(prev_hash)s, %(entry_hash)s)
                    RETURNING id
                    """,
                    {**entry, "prev_hash": prev_hash, "entry_hash": entry_hash},
                )
                new = cur.fetchone()
        return {"id": new["id"], "prev_hash": prev_hash, "entry_hash": entry_hash}

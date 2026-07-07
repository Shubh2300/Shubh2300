# VENDORED verbatim from n8n-office/python/integrations/audit_log.py
# Copied into back-office/bridge/integrations/ as a proven module (do not edit lightly).
# SHA-256 hash-chained, append-only audit log (HIPAA 164.312(b)).
"""
audit_log.py — Append-only, hash-chained, SHA-256-peppered SQLite audit log
for a self-hosted PHI workflow system (HIPAA §164.312(b)).

Design choices
--------------
* **Append-only**: only ``INSERT`` is ever issued by this module. There is no
  ``update_*`` or ``delete_*`` API surface. A trigger also blocks ``UPDATE``
  and ``DELETE`` at the DB level so a future caller cannot quietly rewrite
  history through a raw connection.
* **Hash chain**: every row stores ``prev_hash`` (the previous row's
  ``row_hash``) and ``row_hash`` = SHA-256 over a canonical serialization
  of the row fields plus ``prev_hash``. Any silent edit or deletion of an
  earlier row breaks ``verify_chain`` from that point onward.
* **Patient ID is hashed, not stored in the clear.** The log is itself a
  sensitive artifact, but it should not be a second copy of the patient
  index. ``hash_patient_id`` applies SHA-256 with a per-install secret
  pepper (loaded from env / config). Same patient → same hash, so you can
  still group actions by patient when reviewing.
* **Retention**: HIPAA requires 6 years (§164.530(j)). This module does
  not delete; archival/rotation is a separate operational concern.

Schema
------
    id                       INTEGER PRIMARY KEY AUTOINCREMENT
    ts                       TEXT    NOT NULL  -- ISO-8601 UTC with "Z"
    actor                    TEXT    NOT NULL  -- username/email/system
    intent                   TEXT    NOT NULL  -- why (e.g. "view-chart",
                                                --        "bill-export",
                                                --        "intake-classify")
    target_patient_id_hash   TEXT              -- SHA-256(pepper||raw_id),
                                                -- NULL for non-patient events
    action                   TEXT    NOT NULL  -- what (e.g. "READ",
                                                --        "UPDATE_STATUS",
                                                --        "LOGIN_FAIL")
    result_summary           TEXT    NOT NULL  -- short outcome
                                                -- ("ok", "denied",
                                                --  "exported 3 rows")
    error_msg                TEXT              -- NULL on success
    prev_hash                TEXT    NOT NULL  -- 64-hex, "0"*64 for row 1
    row_hash                 TEXT    NOT NULL UNIQUE

Usage
-----
    from integrations.audit_log import AuditLog

    audit = AuditLog("/path/to/audit.db", pepper=os.environ["AUDIT_PEPPER"])
    audit.log(
        actor="jdoe@clinic.org",
        intent="view-chart",
        action="READ",
        result_summary="ok",
        patient_id="12345",
    )

    ok, broken_at = audit.verify_chain()
    if not ok:
        raise RuntimeError(f"Audit chain broken at row id={broken_at}")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Tuple

# 64-hex-char string used as prev_hash for the first row in the chain.
GENESIS_PREV_HASH = "0" * 64

# Fields, in fixed order, that go into the row_hash preimage. Order is
# load-bearing: changing it invalidates every existing chain, so don't.
_HASHED_FIELDS = (
    "ts",
    "actor",
    "intent",
    "target_patient_id_hash",
    "action",
    "result_summary",
    "error_msg",
    "prev_hash",
)


class AuditLogError(RuntimeError):
    """Raised for any audit-log integrity or configuration failure."""


class AuditLog:
    """Thread-safe append-only audit log with a SHA-256 hash chain.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database. The file will be created
        if it does not yet exist. Place this inside the FileVault volume.
    pepper:
        Secret bytes (or str) mixed into the patient-id hash. MUST be
        stable across the lifetime of the log — rotating the pepper
        breaks the ability to correlate rows for the same patient. Load
        from a secret store, not source. Minimum 32 bytes recommended.
    """

    def __init__(self, db_path: str, pepper: bytes | str) -> None:
        if not pepper:
            raise AuditLogError(
                "AuditLog requires a non-empty pepper; load it from a secret "
                "store and do NOT hard-code it."
            )
        self._db_path = db_path
        self._pepper = pepper.encode("utf-8") if isinstance(pepper, str) else pepper
        # SQLite connections are not safe to share across threads by default;
        # serialize all writes through a single lock. Reads also go through
        # the same connection for simplicity — this log is not high-throughput.
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage txns explicitly
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=FULL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    # ------------------------------------------------------------------ schema

    def _init_schema(self) -> None:
        with self._txn() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                      TEXT    NOT NULL,
                    actor                   TEXT    NOT NULL,
                    intent                  TEXT    NOT NULL,
                    target_patient_id_hash  TEXT,
                    action                  TEXT    NOT NULL,
                    result_summary          TEXT    NOT NULL,
                    error_msg               TEXT,
                    prev_hash               TEXT    NOT NULL,
                    row_hash                TEXT    NOT NULL UNIQUE
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_patient "
                "ON audit_log(target_patient_id_hash);"
            )
            # Belt-and-braces: block UPDATE and DELETE at the DB layer so a
            # future caller using a raw sqlite3 connection cannot quietly
            # mutate history. The application layer also never issues them.
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS audit_log_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT,
                        'audit_log is append-only: UPDATE is not permitted');
                END;
                """
            )
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT,
                        'audit_log is append-only: DELETE is not permitted');
                END;
                """
            )

    # ------------------------------------------------------------------ public

    def hash_patient_id(self, patient_id: str | int | None) -> Optional[str]:
        """Return SHA-256(pepper || patient_id) as hex, or ``None`` if no id.

        Uses ``hmac.new`` for the pepper to avoid length-extension concerns
        and to make the intent ("keyed hash") explicit at the call site.
        """
        if patient_id is None:
            return None
        raw = str(patient_id).strip()
        if not raw:
            return None
        return hmac.new(self._pepper, raw.encode("utf-8"), hashlib.sha256).hexdigest()

    def log(
        self,
        *,
        actor: str,
        intent: str,
        action: str,
        result_summary: str,
        patient_id: str | int | None = None,
        error_msg: str | None = None,
        ts: datetime | None = None,
    ) -> int:
        """Append one row to the audit log.

        Returns
        -------
        int
            The ``id`` of the inserted row.

        Raises
        ------
        AuditLogError
            On any validation failure (empty required field, etc.).
        """
        if not actor:
            raise AuditLogError("actor is required")
        if not intent:
            raise AuditLogError("intent is required")
        if not action:
            raise AuditLogError("action is required")
        if result_summary is None:
            raise AuditLogError("result_summary is required (use '' for empty)")

        ts_iso = _iso_utc(ts or datetime.now(timezone.utc))
        patient_hash = self.hash_patient_id(patient_id)

        with self._lock, self._txn() as cur:
            prev_hash = self._latest_hash(cur)
            row = {
                "ts": ts_iso,
                "actor": actor,
                "intent": intent,
                "target_patient_id_hash": patient_hash,
                "action": action,
                "result_summary": result_summary,
                "error_msg": error_msg,
                "prev_hash": prev_hash,
            }
            row_hash = _compute_row_hash(row)
            cur.execute(
                """
                INSERT INTO audit_log (
                    ts, actor, intent, target_patient_id_hash,
                    action, result_summary, error_msg, prev_hash, row_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    row["ts"],
                    row["actor"],
                    row["intent"],
                    row["target_patient_id_hash"],
                    row["action"],
                    row["result_summary"],
                    row["error_msg"],
                    row["prev_hash"],
                    row_hash,
                ),
            )
            return int(cur.lastrowid)

    def verify_chain(self, start_id: int = 1) -> Tuple[bool, Optional[int]]:
        """Recompute every row's hash and confirm the chain is intact.

        Parameters
        ----------
        start_id:
            Lowest ``id`` to verify (default 1 = whole log). Useful when a
            scheduled job has already verified everything up to some
            checkpoint and only needs to validate the tail.

        Returns
        -------
        (ok, broken_at):
            ``(True, None)`` if intact, else ``(False, id_of_first_bad_row)``.
        """
        expected_prev = GENESIS_PREV_HASH
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT id, ts, actor, intent, target_patient_id_hash,
                       action, result_summary, error_msg, prev_hash, row_hash
                FROM audit_log
                WHERE id >= ?
                ORDER BY id ASC;
                """,
                (start_id,),
            )
            # If we're resuming mid-chain, fetch the prior row's row_hash
            # so we can validate the linkage at the join.
            if start_id > 1:
                prior = self._conn.execute(
                    "SELECT row_hash FROM audit_log WHERE id = ?;",
                    (start_id - 1,),
                ).fetchone()
                if prior is None:
                    return False, start_id  # gap implies a delete
                expected_prev = prior[0]

            for row in cur:
                (
                    row_id, ts, actor, intent, patient_hash,
                    action, result_summary, error_msg, prev_hash, row_hash,
                ) = row
                if prev_hash != expected_prev:
                    return False, int(row_id)
                recomputed = _compute_row_hash({
                    "ts": ts,
                    "actor": actor,
                    "intent": intent,
                    "target_patient_id_hash": patient_hash,
                    "action": action,
                    "result_summary": result_summary,
                    "error_msg": error_msg,
                    "prev_hash": prev_hash,
                })
                if recomputed != row_hash:
                    return False, int(row_id)
                expected_prev = row_hash
        return True, None

    def tail(self, limit: int = 50) -> list[dict]:
        """Return the most recent rows as plain dicts (for review UIs)."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT id, ts, actor, intent, target_patient_id_hash,
                       action, result_summary, error_msg, prev_hash, row_hash
                FROM audit_log
                ORDER BY id DESC
                LIMIT ?;
                """,
                (limit,),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------------- helpers

    def _latest_hash(self, cur: sqlite3.Cursor) -> str:
        row = cur.execute(
            "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1;"
        ).fetchone()
        return row[0] if row else GENESIS_PREV_HASH

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            yield cur
            cur.execute("COMMIT;")
        except Exception:
            cur.execute("ROLLBACK;")
            raise
        finally:
            cur.close()


# ---------------------------------------------------------------------- module-level helpers


def _iso_utc(ts: datetime) -> str:
    """Return an ISO-8601 UTC timestamp with a trailing ``Z``.

    Naive datetimes are assumed to already be UTC (we don't want to silently
    apply the local zone — that would produce a wrong timestamp in the log).
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    # millisecond precision is plenty for a human-readable audit trail
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _compute_row_hash(row: dict) -> str:
    """SHA-256 over a canonical JSON of the hashed fields.

    Using ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` yields
    a stable byte sequence regardless of Python's dict insertion order.
    """
    payload = {k: row.get(k) for k in _HASHED_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------- self-test

if __name__ == "__main__":
    # Quick smoke test — does NOT run against a real PHI log.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "audit.db")
        log = AuditLog(db, pepper="dev-pepper-do-not-use-in-prod")

        log.log(actor="alice@clinic.org", intent="login",
                action="LOGIN_OK", result_summary="ok")
        log.log(actor="alice@clinic.org", intent="view-chart",
                action="READ", result_summary="ok", patient_id="12345")
        log.log(actor="bob@clinic.org", intent="bill-export",
                action="EXPORT", result_summary="exported 3 rows",
                patient_id="12345")
        log.log(actor="system", intent="intake-classify",
                action="CLASSIFY", result_summary="ignored",
                error_msg=None)

        ok, broken = log.verify_chain()
        assert ok, f"chain broken at id={broken}"
        print(f"OK: {len(log.tail())} rows, chain intact")

        # Demonstrate that tampering is detected.
        log._conn.execute(
            "PRAGMA writable_schema = 1;"
        )  # not enough on its own; we have triggers
        try:
            log._conn.execute(
                "UPDATE audit_log SET actor='mallory' WHERE id=2;"
            )
            print("FAIL: trigger did not block UPDATE")
        except sqlite3.IntegrityError as e:
            print(f"OK: UPDATE blocked by trigger ({e})")

        log.close()

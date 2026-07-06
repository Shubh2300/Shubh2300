"""
app/audit.py — Append-only audit trail.

Every meaningful action in the app (login, approval decision, EMR write
attempt, referral update, manager run…) should leave a row here so there is
an honest, reviewable record of what the system did and whether it worked.

PUBLIC API
  log(actor, kind, action, detail=None, outcome='ok') -> None
  recent(limit=200, kind=None) -> list[dict]

HONESTY NOTE
  ``outcome`` records what ACTUALLY happened — callers must pass 'ok' only
  when the underlying operation truly succeeded, and an error/failed outcome
  otherwise. This log is the ledger that lets the manager report tell the
  truth about failures; do not write a success row for work that did not
  happen.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app import db

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """UTC timestamp, ISO-8601 with offset (sortable, unambiguous)."""
    return datetime.now(timezone.utc).isoformat()


def log(actor, kind, action, detail=None, outcome="ok") -> None:
    """Append one row to the audit table.

    Args:
        actor:   who/what triggered it (username, 'system', 'manager'…).
        kind:    coarse category ('auth', 'approval', 'referral', 'chat'…).
        action:  short verb-ish label for the specific event.
        detail:  optional context. A dict/list is JSON-encoded; anything
                 else is stringified. ``None`` stores NULL.
        outcome: 'ok' by default; pass 'error' / 'failed' / etc. on failure.

    Never raises on a logging failure — an audit write must not take down the
    caller's real work; failures are logged to the Python logger instead.
    """
    if isinstance(detail, (dict, list)):
        detail_str = json.dumps(detail, default=str, ensure_ascii=False)
    elif detail is None:
        detail_str = None
    else:
        detail_str = str(detail)

    try:
        conn = db.get_conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO audit "
                    "(ts, actor, kind, action, detail, outcome) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (_now_iso(), str(actor), str(kind), str(action),
                     detail_str, str(outcome)),
                )
        finally:
            conn.close()
    except Exception as exc:  # never let audit failure break the caller
        logger.error(
            "audit.log failed (actor=%s kind=%s action=%s): %s",
            actor, kind, action, exc,
        )


def recent(limit: int = 200, kind: str | None = None,
           action: str | None = None) -> list[dict]:
    """Return the most recent audit rows (newest first) as plain dicts.

    Args:
        limit:  maximum number of rows to return.
        kind:   if given, filter to rows with that ``kind``.
        action: if given, filter to rows with that ``action``. Filtering in SQL
                (not after a shared recency window) lets a low-volume daily job
                find its own last run even when high-volume rows of the same
                ``kind`` (e.g. comms_poll) would otherwise crowd it out.

    ``detail`` is returned as the stored string (JSON text if the caller
    passed a dict); callers that want the object can ``json.loads`` it.
    Returns [] on any read error rather than raising.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    if limit < 0:
        limit = 0

    sql = ("SELECT id, ts, actor, kind, action, detail, outcome "
           "FROM audit")
    args: list = []
    clauses: list = []
    if kind is not None:
        clauses.append("kind = ?")
        args.append(kind)
    if action is not None:
        clauses.append("action = ?")
        args.append(action)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)

    try:
        conn = db.get_conn()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("audit.recent failed: %s", exc)
        return []

    return [dict(r) for r in rows]

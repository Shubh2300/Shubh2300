"""
app/db.py — SQLite persistence layer for the back-office assistant.

Single source of truth for the on-disk schema. Everything the app records
that must survive a restart lives here: the audit trail, the human-approval
queue, ingested referrals, manager reports, login sessions, and the chat
transcript.

DESIGN NOTES
  - ``get_conn()`` opens the DB in WAL mode with ``check_same_thread=False``
    so the async FastAPI worker (which may touch the connection from
    different threads via ``run_in_executor`` / threadpool offload) does not
    trip SQLite's default same-thread guard. Rows come back as
    ``sqlite3.Row`` (dict-like access by column name).
  - ``init_db()`` is idempotent: every table uses ``CREATE TABLE IF NOT
    EXISTS`` so it is safe to call on every startup.

HONESTY NOTE
  This module only stores what it is given; it never invents patient data.
  Missing values are the caller's concern — the schema simply keeps NULL /
  '' as-is so the UI can render "—" rather than a fabricated value.
"""

from __future__ import annotations

import logging
import sqlite3

from app.config import SETTINGS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DDL — one statement per table, all IF NOT EXISTS (idempotent).
# ---------------------------------------------------------------------------

_SCHEMA = (
    # Append-only audit trail. Every meaningful action lands here.
    """
    CREATE TABLE IF NOT EXISTS audit (
        id       INTEGER PRIMARY KEY,
        ts       TEXT,
        actor    TEXT,
        kind     TEXT,
        action   TEXT,
        detail   TEXT,
        outcome  TEXT
    )
    """,
    # Human-approval queue for every EMR write / manual task.
    """
    CREATE TABLE IF NOT EXISTS approvals (
        id            INTEGER PRIMARY KEY,
        ts_created    TEXT,
        ts_decided    TEXT,
        status        TEXT,
        action_type   TEXT,
        params        TEXT,
        reason        TEXT,
        requested_by  TEXT,
        decided_by    TEXT,
        decision_note TEXT,
        result        TEXT
    )
    """,
    # Referrals ingested from the drop folder / Google Sheet.
    """
    CREATE TABLE IF NOT EXISTS referrals (
        id            INTEGER PRIMARY KEY,
        ts            TEXT,
        source        TEXT,
        patient_name  TEXT,
        dob           TEXT,
        phone         TEXT,
        referrer      TEXT,
        reason        TEXT,
        status        TEXT DEFAULT 'new',
        detail        TEXT,
        candidates    TEXT
    )
    """,
    # Manager (self-review) reports.
    """
    CREATE TABLE IF NOT EXISTS manager_reports (
        id            INTEGER PRIMARY KEY,
        ts            TEXT,
        window_hours  INTEGER,
        report        TEXT
    )
    """,
    # Login sessions — token_hash is the sha256 hex of the raw cookie value.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash  TEXT PRIMARY KEY,
        username    TEXT,
        expires_ts  REAL
    )
    """,
    # Chat transcript (user + assistant turns), newest by id.
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id        INTEGER PRIMARY KEY,
        ts        TEXT,
        username  TEXT,
        role      TEXT,
        content   TEXT
    )
    """,
    # Comms lane — inbound/outbound email + SMS. ``external_id`` (IMAP
    # Message-ID / RingCentral message id) is UNIQUE so INSERT OR IGNORE
    # dedupes a re-poll. Message bodies are PHI and UNTRUSTED external
    # content — stored verbatim; the UI renders them textContent-only.
    #   channel:   'email' | 'sms'
    #   direction: 'in' | 'out'
    #   status:    'new' | 'triaged' | 'replied' | 'archived' | 'sent'
    """
    CREATE TABLE IF NOT EXISTS messages (
        id           INTEGER PRIMARY KEY,
        ts           TEXT,
        channel      TEXT,
        direction    TEXT,
        sender       TEXT,
        recipient    TEXT,
        subject      TEXT,
        body         TEXT,
        external_id  TEXT UNIQUE,
        thread_ref   TEXT,
        status       TEXT DEFAULT 'new',
        detail       TEXT
    )
    """,
)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    """Return a new SQLite connection to ``SETTINGS.DB_PATH``.

    Configured with ``row_factory=sqlite3.Row`` (name-based column access),
    WAL journal mode (better read/write concurrency for a single-process
    async server), and ``check_same_thread=False`` (FastAPI may hand the
    connection to a worker thread).

    The caller owns the connection's lifecycle (``with get_conn() as c:``
    commits/rolls back; close when done).
    """
    conn = sqlite3.connect(
        SETTINGS.DB_PATH,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # WAL: readers don't block the writer and vice-versa; survives restarts.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:  # pragma: no cover - PRAGMA rarely fails
        logger.warning("could not set connection PRAGMAs: %s", exc)
    return conn


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create every table if it does not already exist. Idempotent.

    Safe to call on every process startup; existing data and rows are left
    untouched (all DDL is ``CREATE TABLE IF NOT EXISTS``).
    """
    conn = get_conn()
    try:
        with conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)
        logger.info("db.init_db: schema ensured at %s", SETTINGS.DB_PATH)
    finally:
        conn.close()

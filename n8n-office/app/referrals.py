"""app/referrals.py — Referral intake, listing, and EMR candidate matching.

A referral is an inbound "please see this patient" lead that arrives from
two sources:

  1. A **drop folder** — ``DATA_DIR/referrals_in/*.json`` — where any other
     process (an email-intake bot, an n8n flow, a manual paste) can leave a
     JSON file. ``ingest_all`` picks these up, inserts one ``referrals`` row
     each, and moves the file into ``referrals_in/processed/`` so it is never
     ingested twice.
  2. A **Google Sheet** (``REFERRALS_SHEET_ID``) — polled read-only via a
     service account. This arm is entirely optional: the ``googleapiclient``
     dependency and the service-account file may both be absent, in which case
     ``poll_sheet`` returns ``'not configured'`` and never raises.

Honesty notes (house rules):

  * Missing fields are stored as ``"—"`` — we never invent a name, DOB,
    phone, referrer, or reason. An empty/whitespace value from the source
    becomes ``"—"`` on the way in.
  * ``match_candidates`` calls the LIVE EMR via the shared session manager.
    It is guarded by ``SETTINGS.EMR_ENABLED``; when EMR access is off, or the
    search errors, the failure is recorded honestly as
    ``{"error": "..."}`` in the ``candidates`` column — we do not fabricate a
    patient match.
  * PHI note: candidate matches contain patient data. They are persisted only
    in the local SQLite ``referrals`` table (never echoed to browser console
    or sent to a non-PHI-safe LLM).
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import SETTINGS
from app import db, audit

logger = logging.getLogger(__name__)

# Placeholder for any missing / blank referral field (house rule: never
# fabricate; render a dash instead).
_MISSING = "—"

# Canonical top-level columns we lift straight out of a dropped JSON file /
# sheet row. Everything else the source provides is folded into ``detail``.
_CORE_KEYS = ("patient_name", "dob", "phone", "referrer", "reason")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """UTC timestamp, ISO-8601 — matches the rest of the app's ``ts`` cols."""
    return datetime.now(timezone.utc).isoformat()


def _clean(value) -> str:
    """Normalise a source field to a stored string.

    Blank / whitespace-only / ``None`` → the ``"—"`` placeholder so the UI
    never shows an empty cell that looks like real (missing) data.
    """
    if value is None:
        return _MISSING
    text = str(value).strip()
    return text if text else _MISSING


def _referrals_dir() -> Path:
    """Return (creating if needed) the drop folder + its ``processed`` child."""
    drop = SETTINGS.DATA_DIR / "referrals_in"
    (drop / "processed").mkdir(parents=True, exist_ok=True)
    return drop


def _row_to_dict(row) -> dict:
    """Convert a ``referrals`` ``sqlite3.Row`` to a plain dict.

    The ``candidates`` column is JSON text (or NULL) → decoded to a Python
    object (list/dict) or ``None`` so callers get structured data, not a raw
    string.
    """
    d = dict(row)
    raw = d.get("candidates")
    if raw:
        try:
            d["candidates"] = json.loads(raw)
        except (TypeError, ValueError):
            # Corrupt JSON should not blow up a listing; surface it honestly.
            d["candidates"] = {"error": "candidates JSON unreadable"}
    else:
        d["candidates"] = None
    return d


def _insert_referral(
    *,
    source: str,
    patient_name: str,
    dob: str,
    phone: str,
    referrer: str,
    reason: str,
    detail: Optional[dict],
) -> int:
    """Insert one referral row (status defaults to ``'new'``). Returns its id."""
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    with db.get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO referrals
                (ts, source, patient_name, dob, phone, referrer, reason,
                 status, detail, candidates)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, NULL)
            """,
            (
                _now_iso(),
                source,
                patient_name,
                dob,
                phone,
                referrer,
                reason,
                detail_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def _fields_from_record(record: dict) -> dict:
    """Split a raw source dict into core columns + a ``detail`` extras dict.

    Core keys land in dedicated columns (cleaned via ``_clean``); every other
    key is preserved verbatim under ``detail`` so nothing the source sent is
    silently dropped.
    """
    core = {k: _clean(record.get(k)) for k in _CORE_KEYS}
    extras = {
        k: v for k, v in record.items() if k not in _CORE_KEYS
    }
    return {**core, "detail": extras or None}


# ---------------------------------------------------------------------------
# Drop-folder ingestion
# ---------------------------------------------------------------------------

def _ingest_drop() -> int:
    """Ingest every ``*.json`` in the drop folder; return the count inserted.

    Each file becomes exactly one referral row, then is moved into
    ``processed/`` (guaranteeing no double-ingest). A malformed / unreadable
    file is logged and moved aside so it never wedges the queue; it is NOT
    counted as ingested.
    """
    drop = _referrals_dir()
    processed = drop / "processed"
    count = 0

    for path in sorted(drop.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error("referrals: unreadable drop file %s: %s", path.name, e)
            # Move the bad file out of the way so it stops being retried.
            try:
                shutil.move(str(path), str(processed / path.name))
            except OSError:
                logger.error("referrals: could not quarantine %s", path.name)
            continue

        if not isinstance(record, dict):
            logger.error(
                "referrals: drop file %s is not a JSON object; skipping",
                path.name,
            )
            try:
                shutil.move(str(path), str(processed / path.name))
            except OSError:
                pass
            continue

        fields = _fields_from_record(record)
        rid = _insert_referral(source="drop", **fields)
        audit.log(
            actor="system",
            kind="referral",
            action="ingest_drop",
            detail={"referral_id": rid, "file": path.name},
        )
        count += 1

        # Move AFTER a successful insert so a crash mid-loop re-ingests at
        # most the current file, never a lost one.
        try:
            shutil.move(str(path), str(processed / path.name))
        except OSError as e:
            logger.error(
                "referrals: inserted %s but could not move %s: %s",
                rid, path.name, e,
            )

    return count


# ---------------------------------------------------------------------------
# Google Sheet polling (optional — dependency may be absent)
# ---------------------------------------------------------------------------

def poll_sheet():
    """Poll the referrals Google Sheet, inserting any new rows.

    Returns the number of rows ingested, or the string ``'not configured'``
    when this arm is unavailable for ANY of these reasons (all non-fatal):

      * ``REFERRALS_SHEET_ID`` is empty,
      * the service-account JSON file does not exist,
      * ``googleapiclient`` is not installed.

    The Google client libraries are imported *inside* this function so the
    whole app runs fine on a box that never installed them.

    Honesty note: rows are cleaned exactly like drop files (blank → ``"—"``);
    header/value mismatches are skipped, never guessed at.
    """
    sheet_id = getattr(SETTINGS, "REFERRALS_SHEET_ID", "") or ""
    if not sheet_id:
        return "not configured"

    sa_path = Path(SETTINGS.SERVICE_ACCOUNT_JSON)
    if not sa_path.exists():
        logger.info(
            "referrals: sheet configured but service account %s missing",
            sa_path,
        )
        return "not configured"

    try:
        from google.oauth2.service_account import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except Exception as e:  # ImportError or any transitive failure.
        logger.info("referrals: googleapiclient unavailable (%s)", e)
        return "not configured"

    try:
        creds = Credentials.from_service_account_file(
            str(sa_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        service = build("sheets", "v4", credentials=creds,
                        cache_discovery=False)
        resp = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range="A1:Z10000")
            .execute()
        )
    except Exception as e:
        # Network / auth / API errors are non-fatal — the drop folder still
        # works. Report honestly rather than crashing the ingest endpoint.
        logger.error("referrals: sheet poll failed: %s", e)
        return "not configured"

    values = resp.get("values", [])
    if len(values) < 2:
        return 0  # header only (or empty) → nothing to ingest.

    header = [str(h).strip().lower().replace(" ", "_") for h in values[0]]
    # Track which sheet rows we've already ingested (by row-number key) so a
    # re-poll doesn't duplicate. We stash the marker in ``detail``.
    seen = _seen_sheet_rows()

    count = 0
    for idx, raw_row in enumerate(values[1:], start=2):  # sheet row numbers
        if idx in seen:
            continue
        record = {}
        for col, cell in zip(header, raw_row):
            if col:
                record[col] = cell
        if not any(str(v).strip() for v in record.values()):
            continue  # wholly blank row.

        fields = _fields_from_record(record)
        # Fold the source row-number into detail for dedup on next poll.
        detail = dict(fields.get("detail") or {})
        detail["_sheet_row"] = idx
        fields["detail"] = detail

        rid = _insert_referral(source="sheet", **fields)
        audit.log(
            actor="system",
            kind="referral",
            action="ingest_sheet",
            detail={"referral_id": rid, "sheet_row": idx},
        )
        count += 1

    return count


def _seen_sheet_rows() -> set:
    """Return the set of sheet row-numbers already ingested (dedup guard)."""
    seen = set()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT detail FROM referrals WHERE source = 'sheet'"
        ).fetchall()
    for r in rows:
        raw = r["detail"]
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except (TypeError, ValueError):
            continue
        marker = d.get("_sheet_row") if isinstance(d, dict) else None
        if marker is not None:
            seen.add(marker)
    return seen


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_all() -> dict:
    """Ingest from every source. Returns per-source counts.

    Shape (matches the contract):

        {"drop": <int>, "sheet": <int> | 'not configured'}

    The drop folder always runs; the sheet arm degrades to
    ``'not configured'`` when unavailable (see ``poll_sheet``).
    """
    drop_n = _ingest_drop()
    sheet_n = poll_sheet()
    return {"drop": drop_n, "sheet": sheet_n}


def list_referrals(status: Optional[str] = None, limit: int = 200) -> list:
    """Return referral rows (newest first) as dicts, optionally filtered.

    ``candidates`` and ``detail`` remain as stored; ``candidates`` is
    JSON-decoded by ``_row_to_dict``. ``status=None`` returns every status.
    """
    with db.get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM referrals WHERE status = ? "
                "ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM referrals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_referral(referral_id: int, status: str, note: str = "") -> Optional[dict]:
    """Update a referral's status (and append an optional note to ``detail``).

    Returns the refreshed referral dict, or ``None`` if the id is unknown.
    The note is stored under ``detail.notes`` (a list) so status-change
    history is preserved rather than overwritten.
    """
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM referrals WHERE id = ?", (referral_id,)
        ).fetchone()
        if row is None:
            return None

        detail = {}
        if row["detail"]:
            try:
                loaded = json.loads(row["detail"])
                if isinstance(loaded, dict):
                    detail = loaded
            except (TypeError, ValueError):
                detail = {}
        if note:
            notes = detail.get("notes")
            if not isinstance(notes, list):
                notes = []
            notes.append({"ts": _now_iso(), "note": note, "status": status})
            detail["notes"] = notes

        conn.execute(
            "UPDATE referrals SET status = ?, detail = ? WHERE id = ?",
            (status, json.dumps(detail, ensure_ascii=False) if detail else None,
             referral_id),
        )
        conn.commit()

    audit.log(
        actor="system",
        kind="referral",
        action="update",
        detail={"referral_id": referral_id, "status": status,
                "note": note or None},
    )

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM referrals WHERE id = ?", (referral_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def match_candidates(referral_id: int) -> Optional[dict]:
    """Search BOTH EMRs for the referral's patient; store the result.

    Runs ``mgr.combined_search(patient_name)`` via the shared session manager
    and persists the raw result (SIS + Svigg arms) in the ``candidates``
    column. Returns the refreshed referral dict, or ``None`` if the id is
    unknown.

    Honesty / guards:
      * If ``SETTINGS.EMR_ENABLED`` is false, or the search raises, the
        ``candidates`` column stores ``{"error": "..."}`` — we never invent
        a match.
      * The search is synchronous from the caller's view: we spin a private
        event loop (this module has no ``async`` public surface, matching the
        rest of ``referrals.py``).
    """
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT patient_name FROM referrals WHERE id = ?", (referral_id,)
        ).fetchone()
    if row is None:
        return None

    patient_name = row["patient_name"]
    candidates = _run_combined_search(patient_name, referral_id=referral_id)

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE referrals SET candidates = ? WHERE id = ?",
            (json.dumps(candidates, ensure_ascii=False), referral_id),
        )
        conn.commit()

    outcome = "error" if isinstance(candidates, dict) and "error" in candidates \
        else "ok"
    audit.log(
        actor="system",
        kind="referral",
        action="match_candidates",
        detail={"referral_id": referral_id, "query": patient_name},
        outcome=outcome,
    )

    with db.get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM referrals WHERE id = ?", (referral_id,)
        ).fetchone()
    return _row_to_dict(r) if r else None


def _run_combined_search(patient_name: str,
                         referral_id: Optional[int] = None) -> dict:
    """Run the live EMR ``combined_search`` for ``patient_name``.

    Returns the scraper's result dict on success, or ``{"error": "..."}`` on
    any failure (EMR disabled, no name, connect/search exception). This is the
    single place that touches the EMR session manager, so the guard lives
    here. Import is deferred so importing ``referrals`` never drags in the
    heavy scraper stack.

    PHI: on failure we log the referral ID only — NEVER the patient name (the
    server log / launchd .err.log is outside the access-controlled DB).
    """
    if not SETTINGS.EMR_ENABLED:
        return {"error": "EMR disabled (EMR_ENABLED=0)"}
    if not patient_name or patient_name == _MISSING:
        return {"error": "no patient name on referral to search"}

    import asyncio

    async def _do() -> dict:
        # Imported inside the coroutine: config.py has already put
        # python/integrations on sys.path[0], so this bare import resolves.
        from emr_session_manager import EMRSessionManager

        mgr = EMRSessionManager.get_instance()
        await mgr.connect_all()
        return await mgr.combined_search(patient_name)

    def _run_in_fresh_loop() -> dict:
        """Run ``_do`` on a brand-new event loop in the current thread."""
        return asyncio.run(_do())

    try:
        # If we're already inside a running event loop (e.g. a future FastAPI
        # route calls this synchronously), ``asyncio.run`` would raise
        # "cannot be called from a running event loop". Run the coroutine on a
        # dedicated worker thread with its own loop instead — that also keeps
        # the shared manager's asyncio.Lock objects from being reused across
        # two different loops. When there is no running loop, take the simple
        # in-thread path.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run_in_fresh_loop()

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run_in_fresh_loop).result()
    except Exception as e:  # connect/search failure → honest error, no fake.
        logger.error("referrals: combined_search failed for referral id=%s: %s",
                     referral_id if referral_id is not None else "?", e)
        return {"error": f"EMR search failed: {e}"}

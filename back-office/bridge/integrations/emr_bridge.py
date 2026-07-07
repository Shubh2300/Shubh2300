# VENDORED from mainlinesurgery-a11y/n8n-office @ backoffice-autopilot-live-20260705, commit eec3888
# Source path: python/integrations/emr_bridge.py
# Re-vendored from the REAL, HAR-verified production repo (do not edit lightly).
# Cache-fronted EMR lookup bridge across SIS + Svigg / WEBeDoctor / Dr.Com.
#!/usr/bin/env python3
"""
emr_bridge.py - EMR Integration Bridge for SysComplete (SIS) and Svigg / WEBeDoctor / Dr.Com

NONE of these EMRs publish a public REST API. The only realistic integration is
browser-based RPA. This bridge does NOT re-implement scrapers. It WRAPS the
existing, hardened Antigravity Playwright scripts via subprocess + JSON IPC and
adds a short-lived SQLite cache so callers don't slam the portals.

INTEGRATION DEPTH (v1 policy):
    READ-ONLY ONLY. Write operations (creating/moving/cancelling appointments,
    editing chart fields, posting payments) are gated behind a staff sign-off
    that does not exist yet. Functions for those operations are stubs marked
    `# TODO: REQUIRES STAFF SIGN-OFF` and raise NotImplementedError on call.

SESSION EXPIRY:
    The Antigravity RPA scripts persist Auth0 / cookie session state and
    typically auto-reuse it for ~30 days. When a session expires the subprocess
    redirects to the login page and exits with a marker string we surface as
    EMRSessionExpired. The bridge writes a structured row to the audit log
    (cache/audit.log) so staff can see in the dashboard that a manual 2FA
    refresh is needed - we deliberately do NOT auto-trigger a 2FA prompt from
    background jobs.

CACHE:
    SQLite at /Users/shubh/n8n-office/cache/emr_cache.db, keyed by
    (source, op, patient_id_or_query), with a 5-minute TTL. Lookups inside TTL
    return cached JSON without launching Chromium.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Ensure this directory is on sys.path so svigg_scraper can be imported
# directly (bypassing the subprocess wrapper used for older RPA scripts).
_INTEGRATIONS_DIR = Path(__file__).parent
if str(_INTEGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATIONS_DIR))

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

ANTIGRAVITY_DIR = Path(
    os.environ.get("ANTIGRAVITY_DIR", "/Users/shubh/Documents/Antigravity")
)
ANTIGRAVITY_SCRATCH = Path(
    os.environ.get(
        "ANTIGRAVITY_SCRATCH_DIR",
        os.path.expanduser("~/.gemini/antigravity/scratch"),
    )
)

CACHE_DIR = Path("/Users/shubh/n8n-office/cache")
CACHE_DB = CACHE_DIR / "emr_cache.db"
AUDIT_LOG = CACHE_DIR / "audit.log"

CACHE_TTL_SECONDS = 5 * 60  # 5 min

# Map logical EMR source names to the Antigravity script that talks to them.
# Atlantic Pain & Wellness has exactly two EMRs:
#   - SIS Complete: clinical/surgical (has a Playwright RPA agent)
#   - Svigg / Dr.Com / WEBeDoctor: billing + appointments (same system, three
#     names; has a Playwright RPA agent). Ledger data also flows in via the
#     AR-report pipeline (ar_export_agent.py -> import_ar_reports.py).
# The local patient_database.json cache is a merged view of both.
EMR_SOURCES = {
    "sis": {
        "script": ANTIGRAVITY_DIR / "sis_agent.py",
        "label": "SIS Complete (clinical/surgical)",
        "session_file": ANTIGRAVITY_SCRATCH / "sis_browser_state.json",
    },
    "webedoctor": {
        "script": ANTIGRAVITY_DIR / "webedoctor_agent.py",
        "label": "Svigg / Dr.Com / WEBeDoctor (billing + appointments)",
        "session_file": None,  # no persisted state today
    },
}

PATIENT_DB = ANTIGRAVITY_SCRATCH / "patient_database.json"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EMRBridgeError(Exception):
    """Base exception for EMR bridge failures."""


class EMRSessionExpired(EMRBridgeError):
    """The portal session expired and a human must re-auth (SMS 2FA, etc.)."""


class EMRSourceUnsupported(EMRBridgeError):
    """The requested source has no RPA script wired up yet."""


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------


def _ensure_cache() -> None:
    """Create cache dir + SQLite schema on first call. Idempotent."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS emr_cache (
                source       TEXT NOT NULL,
                op           TEXT NOT NULL,
                key          TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at   REAL NOT NULL,
                PRIMARY KEY (source, op, key)
            );
            CREATE INDEX IF NOT EXISTS idx_emr_cache_fetched_at
                ON emr_cache(fetched_at);

            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                source      TEXT,
                op          TEXT,
                key         TEXT,
                outcome     TEXT NOT NULL,   -- ok | cache_hit | session_expired | error
                detail      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
            CREATE INDEX IF NOT EXISTS idx_audit_log_outcome ON audit_log(outcome);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _cache_get(source: str, op: str, key: str) -> Optional[Any]:
    _ensure_cache()
    conn = sqlite3.connect(CACHE_DB)
    try:
        row = conn.execute(
            "SELECT payload_json, fetched_at FROM emr_cache "
            "WHERE source=? AND op=? AND key=?",
            (source, op, key),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    payload_json, fetched_at = row
    if (time.time() - fetched_at) > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(payload_json)
    except json.JSONDecodeError:
        return None


def _cache_put(source: str, op: str, key: str, payload: Any) -> None:
    _ensure_cache()
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO emr_cache(source, op, key, payload_json, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, op, key, json.dumps(payload, default=str), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Audit PHI scrubbing (defense in depth)
# ---------------------------------------------------------------------------
# emr_bridge runs in a SEPARATE process from the MCP server and cannot import
# it, so the audit sanitizer is duplicated here (kept behaviorally identical:
# same salt env var + same 12-hex SHA-256 token so a value hashes to the SAME
# token in both components). The `key` a caller passes to `_audit` IS a raw
# patient identifier — lookup_patient passes a lowercased name, svigg passes a
# raw name — which would otherwise persist verbatim in the shared audit_log and
# replay into chat. We hash the whole key here so the raw name never touches
# the DB or the human-readable log line. `detail` (exception / portal error
# text) is scrubbed of name/phone/email shapes for the same reason.
#
# All name/phone/email regexes are CASE-INSENSITIVE so lowercased ('dorca
# jones'), ALL-CAPS ('SMITH, JOHN'), and mixed-case names are all caught.
_AUDIT_SALT = os.environ.get("ATLANTIC_EMR_AUDIT_SALT", "atlantic-emr-audit-v1")

_DIGIT_RUN_RE = re.compile(r"\d[\d\-\.\s]{4,}\d")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# A word pair — "Jane Doe", "Doe, Jane", "SMITH, JOHN", "dorca jones" — a name
# shape, matched case-insensitively so lowercased / all-caps names are caught.
_NAME_PAIR_RE = re.compile(r"\b[A-Za-z][A-Za-z'\-]+\s*,?\s+[A-Za-z][A-Za-z'\-]+\b")


def _audit_hash(value: str) -> str:
    """One-way, salted, short token for a raw identifier. Never reversible."""
    digest = hashlib.sha256((_AUDIT_SALT + "|" + value).encode("utf-8", "replace"))
    return digest.hexdigest()[:12]


def _scrub_audit_text(text: str) -> str:
    """Mask PHI-shaped substrings (emails, digit runs, name pairs) in free text."""
    if not text:
        return text
    text = _EMAIL_RE.sub("<redacted-email>", text)
    text = _DIGIT_RUN_RE.sub("<redacted-num>", text)
    text = _NAME_PAIR_RE.sub("<redacted-name>", text)
    return text


def _sanitize_audit_key(key: str) -> str:
    """Hash a raw identifier key so no name/phone/MRN persists in the audit log.

    Callers pass a raw patient identifier as ``key`` (a lowercased name, a
    phone, an MRN). It is a single opaque value (not ``k=v`` args), so we hash
    the whole thing to a stable ``<hash:...>`` token — never reversible, but
    stable enough to correlate repeat lookups of the same patient. An empty key
    stays a readable ``<none>`` marker (no PHI).
    """
    if key is None:
        return ""
    v = str(key).strip()
    if not v:
        return "<none>"
    return f"<hash:{_audit_hash(v)}>"


def _audit(
    source: Optional[str],
    op: str,
    key: str,
    outcome: str,
    detail: str = "",
) -> None:
    """
    Append a structured audit row AND a human-readable line to audit.log.
    The dashboard's /api/logs surface can tail audit.log to show staff things
    like 'SIS session expired — please refresh via Antigravity'.

    The ``key`` (a raw patient identifier) is HASHED and ``detail`` is scrubbed
    of PHI shapes BEFORE anything is written, so the shared audit_log never
    persists a raw name / phone / MRN. Op + hashed key keep the audit useful.
    """
    _ensure_cache()
    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    safe_key = _sanitize_audit_key(key)
    safe_detail = _scrub_audit_text(detail)
    conn = sqlite3.connect(CACHE_DB)
    try:
        conn.execute(
            "INSERT INTO audit_log(ts, source, op, key, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, source, op, safe_key, outcome, safe_detail),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                f"[{ts}] source={source or '-'} op={op} key={safe_key} "
                f"outcome={outcome} detail={safe_detail}\n"
            )
    except OSError:
        # Audit log is best-effort. Never break the caller on a disk hiccup.
        pass


# ---------------------------------------------------------------------------
# Subprocess wrapper around Antigravity RPA scripts
# ---------------------------------------------------------------------------


_SESSION_EXPIRED_MARKERS = (
    "session expired",
    "redirected to login",
    "saved session expired",
    "Authentication sequence aborted",
    "awaiting_2fa",
    "SMS Multi-Factor Authentication requested",
)


def _run_antigravity(source: str, args: list[str], timeout: int = 180) -> dict:
    """
    Invoke an Antigravity RPA script. Returns {stdout, stderr, returncode}.
    Raises EMRSourceUnsupported if no script is wired for that source.
    Raises EMRSessionExpired if portal kicked us back to login / asked for 2FA.
    """
    meta = EMR_SOURCES.get(source)
    if meta is None:
        raise EMRSourceUnsupported(f"Unknown EMR source: {source!r}")
    script = meta["script"]
    if script is None:
        raise EMRSourceUnsupported(
            f"{meta['label']} has no standalone RPA script; "
            "data is served from patient_database.json via the AR pipeline."
        )
    if not Path(script).exists():
        raise EMRBridgeError(f"RPA script missing on disk: {script}")

    env = os.environ.copy()
    env.setdefault("ANTIGRAVITY_WORKSPACE_DIR", str(ANTIGRAVITY_DIR))
    env.setdefault("ANTIGRAVITY_SCRATCH_DIR", str(ANTIGRAVITY_SCRATCH))

    try:
        proc = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=str(ANTIGRAVITY_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise EMRBridgeError(
            f"{meta['label']} RPA timed out after {timeout}s"
        ) from exc

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lower = combined.lower()
    if any(marker.lower() in lower for marker in _SESSION_EXPIRED_MARKERS):
        raise EMRSessionExpired(
            f"{meta['label']} session expired or required 2FA. "
            "Refresh via Antigravity (run the agent interactively) and retry."
        )

    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }


# ---------------------------------------------------------------------------
# Local fallback: read from patient_database.json
# ---------------------------------------------------------------------------


def _load_patient_db() -> list[dict]:
    if not PATIENT_DB.exists():
        return []
    try:
        with PATIENT_DB.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _match_patient(patients: list[dict], patient_id: str) -> Optional[dict]:
    """Patient records in Antigravity have no stable id — match by name or phone."""
    needle = (patient_id or "").strip().lower()
    if not needle:
        return None
    for p in patients:
        if str(p.get("name", "")).lower() == needle:
            return p
        if needle and needle == str(p.get("phone", "")).lower():
            return p
        if needle and needle == str(p.get("email", "")).lower():
            return p
    # Fuzzy: substring on name.
    for p in patients:
        if needle in str(p.get("name", "")).lower():
            return p
    return None


# ---------------------------------------------------------------------------
# Public read-only API
# ---------------------------------------------------------------------------


def lookup_patient(patient_id: str, source: str = "all") -> dict:
    """
    Read-only patient lookup across one or all EMRs.

    Args:
        patient_id: Patient identifier — name, phone, or email (Antigravity's
            patient records have no stable numeric id today).
        source: 'all' | 'sis' | 'webedoctor'.

    Returns:
        {
          "patient_id": str,
          "source": str,
          "results": [ { source, label, record|null, cached: bool } ],
          "session_expired": [ source, ... ],   # any sources that need 2FA refresh
        }

    Never raises on per-source failure; aggregates errors into the response so
    the caller can show partial data. Only raises on programmer error
    (unknown source name passed in directly).
    """
    if not patient_id:
        raise EMRBridgeError("patient_id is required")

    sources = (
        list(EMR_SOURCES.keys()) if source == "all" else [source]
    )
    for s in sources:
        if s not in EMR_SOURCES:
            raise EMRSourceUnsupported(f"Unknown EMR source: {s!r}")

    results: list[dict] = []
    expired: list[str] = []

    for src in sources:
        cache_key = patient_id.strip().lower()
        cached = _cache_get(src, "lookup_patient", cache_key)
        if cached is not None:
            _audit(src, "lookup_patient", cache_key, "cache_hit")
            results.append(
                {
                    "source": src,
                    "label": EMR_SOURCES[src]["label"],
                    "record": cached,
                    "cached": True,
                }
            )
            continue

        # No standalone RPA agent supports a one-off patient lookup today
        # (sis_agent.py / webedoctor_agent.py only do full --action sync).
        # So for v1 we serve patient lookups from the local merged DB that
        # those agents populate on their scheduled runs. When a per-patient
        # RPA action gets added upstream, swap the body below for
        # _run_antigravity(src, ["--action", "lookup", "--id", patient_id]).
        try:
            patients = _load_patient_db()
            record = _match_patient(patients, patient_id)
            payload = record  # may be None — that's a valid cached "miss"
            _cache_put(src, "lookup_patient", cache_key, payload)
            _audit(
                src,
                "lookup_patient",
                cache_key,
                "ok" if record else "ok_no_match",
            )
            results.append(
                {
                    "source": src,
                    "label": EMR_SOURCES[src]["label"],
                    "record": record,
                    "cached": False,
                }
            )
        except EMRSessionExpired as exc:
            expired.append(src)
            _audit(src, "lookup_patient", cache_key, "session_expired", str(exc))
            results.append(
                {
                    "source": src,
                    "label": EMR_SOURCES[src]["label"],
                    "record": None,
                    "cached": False,
                    "error": "session_expired",
                }
            )
        except EMRBridgeError as exc:
            _audit(src, "lookup_patient", cache_key, "error", str(exc))
            results.append(
                {
                    "source": src,
                    "label": EMR_SOURCES[src]["label"],
                    "record": None,
                    "cached": False,
                    "error": str(exc),
                }
            )

    return {
        "patient_id": patient_id,
        "source": source,
        "results": results,
        "session_expired": expired,
    }


def check_appointment_book(
    date_range: tuple[str, str],
    source: str = "sis",
) -> dict:
    """
    Read-only appointment book scan for a date range.

    Args:
        date_range: (start_iso_date, end_iso_date), e.g. ('2026-06-28','2026-07-05').
        source: which EMR to query. Defaults to SIS (the surgical-center scheduler).

    Returns:
        { source, label, date_range, appointments: [...], cached: bool }

    Today the Antigravity SIS agent only runs a full `--action sync` — it does
    not expose a one-shot appointment-range scrape. Until that lands upstream,
    this function reads scheduling data from patient_database.json
    (intakeDate / surgeryStatus fields) and filters by the requested window.
    When a dedicated `--action schedule --from X --to Y` mode is added to
    sis_agent.py, swap the body for the _run_antigravity(...) call below.
    """
    start, end = date_range
    if not (start and end):
        raise EMRBridgeError("date_range must be (start, end) ISO dates")
    if source not in EMR_SOURCES:
        raise EMRSourceUnsupported(f"Unknown EMR source: {source!r}")

    cache_key = f"{start}__{end}"
    cached = _cache_get(source, "appointment_book", cache_key)
    if cached is not None:
        _audit(source, "appointment_book", cache_key, "cache_hit")
        return {
            "source": source,
            "label": EMR_SOURCES[source]["label"],
            "date_range": [start, end],
            "appointments": cached,
            "cached": True,
        }

    try:
        # Upstream-ready hook — keep commented until sis_agent.py grows the action:
        #
        # _run_antigravity(
        #     source,
        #     ["--action", "schedule", "--from", start, "--to", end],
        #     timeout=240,
        # )
        # appts = json.loads(
        #     (ANTIGRAVITY_SCRATCH / "sis_schedule_latest.json").read_text()
        # )

        patients = _load_patient_db()

        def _in_window(p: dict) -> bool:
            raw = p.get("intakeDate") or ""
            # patient_database.json uses MM/DD/YYYY; normalize to ISO.
            try:
                if "/" in raw:
                    m, d, y = raw.split("/")
                    iso = f"{y}-{int(m):02d}-{int(d):02d}"
                else:
                    iso = raw
                return start <= iso <= end
            except (ValueError, AttributeError):
                return False

        appts = [
            {
                "name": p.get("name"),
                "intake_date": p.get("intakeDate"),
                "type": p.get("type"),
                "status": p.get("surgeryStatus") or "",
                "insurance": p.get("insurance"),
            }
            for p in patients
            if _in_window(p)
        ]
        _cache_put(source, "appointment_book", cache_key, appts)
        _audit(source, "appointment_book", cache_key, "ok", f"count={len(appts)}")
        return {
            "source": source,
            "label": EMR_SOURCES[source]["label"],
            "date_range": [start, end],
            "appointments": appts,
            "cached": False,
        }
    except EMRSessionExpired as exc:
        _audit(source, "appointment_book", cache_key, "session_expired", str(exc))
        raise
    except EMRBridgeError as exc:
        _audit(source, "appointment_book", cache_key, "error", str(exc))
        raise


def check_billing(patient_id: str, source: str = "all") -> dict:
    """
    Read-only billing/ledger lookup. Aggregates across sources by default.

    Returns:
        {
          patient_id, source,
          ledgers: [ { source, label, balance, paid, status, cached } ],
          session_expired: [...]
        }
    """
    if not patient_id:
        raise EMRBridgeError("patient_id is required")

    sources = list(EMR_SOURCES.keys()) if source == "all" else [source]
    for s in sources:
        if s not in EMR_SOURCES:
            raise EMRSourceUnsupported(f"Unknown EMR source: {s!r}")

    ledgers: list[dict] = []
    expired: list[str] = []

    patients = _load_patient_db()
    record = _match_patient(patients, patient_id)

    for src in sources:
        cache_key = patient_id.strip().lower()
        cached = _cache_get(src, "check_billing", cache_key)
        if cached is not None:
            _audit(src, "check_billing", cache_key, "cache_hit")
            ledgers.append({**cached, "source": src, "cached": True})
            continue

        try:
            # Upstream-ready hook:
            # _run_antigravity(src, ["--action", "billing", "--id", patient_id])
            # Then parse the resulting JSON drop. Today: read from merged DB.
            if record is None:
                payload = {
                    "label": EMR_SOURCES[src]["label"],
                    "balance": None,
                    "paid": None,
                    "status": None,
                    "found": False,
                }
            else:
                payload = {
                    "label": EMR_SOURCES[src]["label"],
                    "balance": record.get("balance"),
                    "paid": record.get("paid"),
                    "status": record.get("surgeryStatus") or None,
                    "insurance": record.get("insurance"),
                    "found": True,
                }
            _cache_put(src, "check_billing", cache_key, payload)
            _audit(
                src,
                "check_billing",
                cache_key,
                "ok" if record else "ok_no_match",
            )
            ledgers.append({**payload, "source": src, "cached": False})
        except EMRSessionExpired as exc:
            expired.append(src)
            _audit(src, "check_billing", cache_key, "session_expired", str(exc))
            ledgers.append(
                {
                    "source": src,
                    "label": EMR_SOURCES[src]["label"],
                    "error": "session_expired",
                    "cached": False,
                }
            )
        except EMRBridgeError as exc:
            _audit(src, "check_billing", cache_key, "error", str(exc))
            ledgers.append(
                {
                    "source": src,
                    "label": EMR_SOURCES[src]["label"],
                    "error": str(exc),
                    "cached": False,
                }
            )

    return {
        "patient_id": patient_id,
        "source": source,
        "ledgers": ledgers,
        "session_expired": expired,
    }


# ---------------------------------------------------------------------------
# WRITE OPERATIONS — gated, not yet permitted
# ---------------------------------------------------------------------------
#
# Everything below mutates EMR state. The clinic has not signed off on
# automated writes, and a misclick at this layer can double-book an OR or
# overwrite a chart. Until staff approve a specific operation with explicit
# scope + audit + dry-run plan, these functions intentionally raise.


def book_appointment(  # noqa: D401
    patient_id: str,
    slot_iso: str,
    provider: str,
    source: str = "sis",
) -> dict:
    """Book an appointment in the EMR scheduler.

    # TODO: REQUIRES STAFF SIGN-OFF
    """
    raise NotImplementedError(
        "book_appointment is a write op and is disabled in v1. "
        "Staff must sign off on a written procedure (which slots are eligible, "
        "what conflicts cancel the booking, who reviews the audit log) before "
        "this is wired to _run_antigravity(...)."
    )


def cancel_appointment(  # noqa: D401
    appointment_id: str,
    reason: str,
    source: str = "sis",
) -> dict:
    """Cancel an existing appointment.

    # TODO: REQUIRES STAFF SIGN-OFF
    """
    raise NotImplementedError(
        "cancel_appointment is a write op and is disabled in v1."
    )


def post_payment(  # noqa: D401
    patient_id: str,
    amount_cents: int,
    source: str,
    memo: str = "",
) -> dict:
    """Post a payment / adjustment to the patient ledger.

    # TODO: REQUIRES STAFF SIGN-OFF
    """
    raise NotImplementedError(
        "post_payment is a write op and is disabled in v1."
    )


def update_chart_field(  # noqa: D401
    patient_id: str,
    field: str,
    value: Any,
    source: str,
) -> dict:
    """Edit a free-text or structured chart field in the EMR.

    # TODO: REQUIRES STAFF SIGN-OFF
    """
    raise NotImplementedError(
        "update_chart_field is a write op and is disabled in v1."
    )


# ---------------------------------------------------------------------------
# Smoke-test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Tiny smoke test: ensure the schema initializes and a lookup runs.
    _ensure_cache()
    demo = lookup_patient("Dorca Jones", source="all")
    print(json.dumps(demo, indent=2, default=str))
    appts = check_appointment_book(("2025-11-01", "2025-12-31"), source="sis")
    print(json.dumps(appts, indent=2, default=str))
    bill = check_billing("Dorca Jones", source="all")
    print(json.dumps(bill, indent=2, default=str))

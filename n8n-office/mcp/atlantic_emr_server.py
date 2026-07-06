#!/usr/bin/env python3
"""
atlantic_emr_server.py — MCP server exposing Atlantic Pain & Wellness EMR
read-only operations as tools callable from any Claude Code session.

Name   : atlantic-emr
Version: 0.3.0

READ-ONLY ONLY. No write/mutation tools are exposed. emr_bridge already
returns NotImplementedError for booking/cancellation/payment stubs; we simply
do not surface them here.

PHI WARNING: lookup_patient, check_appointment_book, and check_billing all
return Protected Health Information (PHI). Do not echo raw records to chat or
store them outside the audit log.

Every tool call is written to the audit log (emr_bridge's SQLite cache db)
with actor="mcp", so the operations are traceable.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Self-cleanup at startup (OPT-IN)
#
# Older versions of this script killed any sibling atlantic_emr_server.py
# instance on startup. That caused cascade failures when Claude Desktop /
# Claude Code spawn multiple concurrent CLI sessions: each new CLI starts an
# atlantic-emr child, which SIGTERMs the atlantic-emr children belonging to
# every other CLI session, which surfaces as MCP `Connection closed` errors
# and "Claude Code process exited with code 143" (= 128 + SIGTERM) in the
# host's logs.
#
# The kill loop is now opt-in via the ATLANTIC_EMR_KILL_PRIOR=1 env var. The
# safe default is to leave sibling instances alone — MCP hosts manage their
# own children's lifecycle, and Playwright persistent-context contention is
# handled by emr_bridge's own locking.
# ---------------------------------------------------------------------------

def _kill_prior_instances():
    """Kill any other atlantic_emr_server.py Python processes. No-op if none."""
    my_pid = os.getpid()
    script_marker = "atlantic_emr_server.py"
    try:
        # `ps -ax -o pid=,command=` is portable across macOS/Linux without root
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        # Only match the actual Python process running our script, NOT the
        # MCP host whose cmdline contains the script path as a config arg.
        if pid == my_pid:
            continue
        if script_marker not in cmd:
            continue
        if "/Python.app/" not in cmd and "python3" not in cmd.lower().split("/")[-1:][0:1]:
            # Skip non-Python hosts that happen to mention the script path
            if not any(p in cmd for p in ("Python.app", "python3", "python ")):
                continue
        try:
            os.kill(pid, signal.SIGTERM)
            sys.stderr.write(f"atlantic-emr: killed stale instance pid={pid}\n")
        except (ProcessLookupError, PermissionError):
            pass


if os.environ.get("ATLANTIC_EMR_KILL_PRIOR", "0") == "1":
    _kill_prior_instances()

# ---------------------------------------------------------------------------
# Path resolution — emr_bridge and audit_log live in the sibling package
# ---------------------------------------------------------------------------

_INTEGRATIONS = Path(__file__).parent.parent / "python" / "integrations"
if str(_INTEGRATIONS) not in sys.path:
    sys.path.insert(0, str(_INTEGRATIONS))

import emr_bridge  # noqa: E402
from emr_bridge import (  # noqa: E402
    CACHE_DB,
    ANTIGRAVITY_SCRATCH,
    EMRBridgeError,
    EMRSessionExpired,
    lookup_patient as _emr_lookup,
    check_appointment_book as _emr_appt,
    check_billing as _emr_billing,
)
from emr_session_manager import EMRSessionManager  # noqa: E402
from sis_client import SISClient  # noqa: E402

# ---------------------------------------------------------------------------
# MCP server bootstrap
# ---------------------------------------------------------------------------

from mcp.server.fastmcp import FastMCP  # noqa: E402

server = FastMCP(
    "atlantic-emr",
    instructions="Atlantic Pain & Wellness EMR read-only tools (v0.3.0). PHI — do not echo patient data to chat.",
)

# ---------------------------------------------------------------------------
# Audit helper — writes directly to emr_bridge's own SQLite cache/audit table
# so all MCP calls appear in the same log the dashboard reads.
# ---------------------------------------------------------------------------

def _mcp_audit(tool: str, args_summary: str, outcome: str, detail: str = "") -> None:
    """Append one row to the emr_cache.db audit_log table."""
    try:
        emr_bridge._ensure_cache()
        conn = sqlite3.connect(str(CACHE_DB))
        try:
            conn.execute(
                """
                INSERT INTO audit_log (ts, source, op, key, outcome, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "mcp",
                    tool,
                    args_summary[:255],
                    outcome,
                    detail[:512] if detail else "",
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # audit failure must never crash the tool response


# ---------------------------------------------------------------------------
# Lazy EMR session manager — started on first live-tool call
# ---------------------------------------------------------------------------

_mgr: EMRSessionManager | None = None

async def _get_mgr() -> EMRSessionManager:
    """Return the singleton EMRSessionManager, connecting on first call."""
    global _mgr
    if _mgr is None:
        _mgr = EMRSessionManager.get_instance()
        await _mgr.connect_all()
    return _mgr


# ---------------------------------------------------------------------------
# Tool: lookup_patient
# ---------------------------------------------------------------------------

@server.tool(
    name="lookup_patient",
    description=(
        "Atlantic Pain & Wellness: look up a patient across one or all EMRs "
        "using LIVE data from SIS Complete and/or Svigg / Dr.Com / WEBeDoctor. "
        "READ-ONLY — does not modify any record. "
        "Returns PHI (demographics + record links); do not echo raw output to "
        "chat or store it outside the audit log. "
        "patient_id may be a name (Last, First or First Last), phone, or MRN. "
        "source: 'all' queries both EMRs; 'sis' queries SIS Complete only; "
        "'svigg' or 'webedoctor' queries Svigg/Dr.Com only. "
        "No longer reads the local static snapshot — all data is fetched live."
    ),
)
async def lookup_patient(
    patient_id: str,
    source: str = "all",
) -> dict:
    """
    Args:
        patient_id: Name (Last, First or First Last), phone, or MRN.
        source: 'all' | 'sis' | 'svigg' | 'webedoctor'
    """
    args_str = f"patient_id={patient_id!r} source={source!r}"
    src = (source or "all").strip().lower()
    try:
        mgr = await _get_mgr()

        if src == "all":
            res = await mgr.combined_search(patient_id)
            sis_count = len(res.get("sis_results", []))
            svigg_count = len(res.get("svigg_results", []))
            _mcp_audit("lookup_patient", args_str, "ok",
                       f"sis={sis_count} svigg={svigg_count}")
            return {
                "patient_id": patient_id,
                "source": "all",
                "live": True,
                "sis_results": res.get("sis_results", []),
                "svigg_results": res.get("svigg_results", []),
            }

        elif src == "sis":
            results = await mgr.sis_search(patient_id)
            count = len(results) if isinstance(results, list) else 0
            _mcp_audit("lookup_patient", args_str, "ok", f"count={count}")
            return {
                "patient_id": patient_id,
                "source": "sis",
                "live": True,
                "results": results,
                "count": count,
            }

        elif src in ("svigg", "webedoctor"):
            # Parse name into last/first exactly like svigg_live_lookup
            parts = [p.strip() for p in patient_id.split(",", 1)]
            last_name = parts[0]
            first_name = parts[1] if len(parts) > 1 else ""
            if not first_name and " " in last_name:
                space_parts = last_name.rsplit(" ", 1)
                last_name = space_parts[-1]
                first_name = space_parts[0]
            results = await mgr.svigg_search(last_name, first_name)
            count = len(results) if isinstance(results, list) else 0
            _mcp_audit("lookup_patient", args_str, "ok", f"count={count}")
            return {
                "patient_id": patient_id,
                "source": "svigg",
                "live": True,
                "results": results,
                "count": count,
            }

        else:
            _mcp_audit("lookup_patient", args_str, "error", f"unknown_source={src}")
            return {
                "patient_id": patient_id,
                "source": source,
                "error": f"unknown source: {source}",
                "results": [],
            }

    except EMRSessionExpired as exc:
        _mcp_audit("lookup_patient", args_str, "error", "session_expired")
        return {"error": "session_expired", "patient_id": patient_id, "source": source, "results": []}
    except Exception as exc:
        _mcp_audit("lookup_patient", args_str, "error", f"unexpected: {exc}")
        return {"error": f"unexpected: {exc}", "patient_id": patient_id, "source": source, "results": []}


# ---------------------------------------------------------------------------
# Tool: check_appointment_book
# ---------------------------------------------------------------------------

@server.tool(
    name="check_appointment_book",
    description=(
        "Atlantic Pain & Wellness: scan the appointment book for a date range "
        "using LIVE data from SIS Complete and/or Svigg / Dr.Com / WEBeDoctor. "
        "READ-ONLY — does not create or modify appointments. "
        "Returns PHI (patient names, insurance, intake/surgery dates); do not "
        "echo raw output to chat or store it outside the audit log. "
        "Defaults to SIS (the surgical-center scheduler). "
        "date_range_start and date_range_end are ISO dates (YYYY-MM-DD). "
        "WINDOW NOTE: SIS returns the full Mon-Sun week containing date_range_start; "
        "Svigg returns only the date_range_start day's calendar. "
        "Full arbitrary-range honoring is a future enhancement."
    ),
)
async def check_appointment_book(
    date_range_start: str,
    date_range_end: str,
    source: str = "sis",
) -> dict:
    """
    Args:
        date_range_start: ISO date string, e.g. '2026-06-28'
        date_range_end:   ISO date string, e.g. '2026-07-05'
        source: which EMR(s) to query — 'sis' | 'svigg' | 'webedoctor' | 'all'
    """
    args_str = f"date_range=({date_range_start!r},{date_range_end!r}) source={source!r}"
    src = (source or "sis").strip().lower()
    try:
        mgr = await _get_mgr()
        sched = await mgr.combined_schedule(date_range_start, date_range_end)

        if src == "sis":
            appts = sched.get("sis_schedule", [])
            count = sched.get("sis_count", 0)
            _mcp_audit("check_appointment_book", args_str, "ok", f"count={count}")
            return {
                "source": "sis",
                "live": True,
                "date_range": [date_range_start, date_range_end],
                "appointments": appts,
                "count": count,
                "window_note": "SIS = week containing start_date",
            }

        elif src in ("svigg", "webedoctor"):
            appts = sched.get("svigg_calendar", [])
            count = sched.get("svigg_count", 0)
            _mcp_audit("check_appointment_book", args_str, "ok", f"count={count}")
            return {
                "source": "svigg",
                "live": True,
                "date_range": [date_range_start, date_range_end],
                "appointments": appts,
                "count": count,
                "window_note": "Svigg = start_date day only",
            }

        elif src == "all":
            sched["live"] = True
            total = sched.get("sis_count", 0) + sched.get("svigg_count", 0)
            _mcp_audit("check_appointment_book", args_str, "ok", f"total={total}")
            return sched

        else:
            _mcp_audit("check_appointment_book", args_str, "error", f"unknown_source={src}")
            return {
                "error": f"unknown source: {source}",
                "date_range": [date_range_start, date_range_end],
                "appointments": [],
            }

    except Exception as exc:
        _mcp_audit("check_appointment_book", args_str, "error", str(exc))
        return {
            "error": str(exc),
            "date_range": [date_range_start, date_range_end],
            "appointments": [],
        }


# ---------------------------------------------------------------------------
# Tool: check_billing
# ---------------------------------------------------------------------------

@server.tool(
    name="check_billing",
    description=(
        "Atlantic Pain & Wellness: retrieve billing/ledger summary for a patient. "
        "READ-ONLY — does not post or modify any payment. "
        "Returns PHI (balance, payment status, insurance); do not echo raw "
        "output to chat or store it outside the audit log. "
        "Routes through the LIVE Svigg / Dr.Com / WEBeDoctor portal (visit ledger) "
        "AND the LIVE SIS Complete billing API via the shared EMRSessionManager. "
        "patient_id may be a name ('Last, First' or 'First Last') OR a Svigg "
        "account number (6+ digits). "
        "source='svigg'|'webedoctor' reads the live Svigg ledger; source='sis' "
        "resolves the patient in SIS and returns the live balance from the SIS "
        "billing API (verified 2026-06-30); source='all' returns both lanes."
    ),
)
async def check_billing(
    patient_id: str,
    source: str = "all",
) -> dict:
    """
    Args:
        patient_id: Patient name ('Last, First' / 'First Last') OR a Svigg
            account number (digits only, 6+ chars → treated as acct directly).
        source: 'all' | 'svigg' | 'webedoctor' | 'sis'

    Returns (shape preserved from the legacy bridge so callers don't break):
        {
          patient_id, source,
          ledgers: [ { source, label, found, balance, paid, status,
                       insurance, patient_id_used, cached, ... } ],
          session_expired: [...]
        }
    """
    args_str = f"patient_id={patient_id!r} source={source!r}"

    # Normalize requested lanes. 'svigg' and 'webedoctor' are the same live
    # Svigg / Dr.Com / WEBeDoctor portal. 'all' = svigg + sis.
    src = (source or "all").strip().lower()
    if src == "all":
        lanes = ["svigg", "sis"]
    elif src in ("svigg", "webedoctor"):
        lanes = ["svigg"]
    elif src == "sis":
        lanes = ["sis"]
    else:
        _mcp_audit("check_billing", args_str, "error", "unknown_source")
        return {
            "error": f"unknown source: {source!r} (expected all|svigg|webedoctor|sis)",
            "patient_id": patient_id,
            "source": source,
            "ledgers": [],
            "session_expired": [],
        }

    ledgers: list[dict] = []
    expired: list[str] = []

    SVIGG_LABEL = "Svigg / Dr.Com / WEBeDoctor (billing + appointments)"
    SIS_LABEL = "SIS Complete (clinical/surgical)"

    if not patient_id or not str(patient_id).strip():
        _mcp_audit("check_billing", args_str, "error", "missing_patient_id")
        return {
            "error": "patient_id is required",
            "patient_id": patient_id,
            "source": source,
            "ledgers": [],
            "session_expired": [],
        }

    pid = str(patient_id).strip()

    # ---- SIS lane: LIVE billing balance via SIS Complete billing API. -----
    # Verified 2026-06-30: resolve the patient -> SIS patientId, then read the
    # live balance (statements tracker accountBalance, or derived from ledger
    # aging totals). Returns real data, never a fabricated balance — if the
    # patient can't be resolved in SIS, found=False with a clear note.
    if "sis" in lanes:
        try:
            mgr = await _get_mgr()
            sis_pid = None
            if pid.isdigit():
                # A bare numeric id is ambiguous: it could be a Svigg account
                # number, not a SIS patientId. Only treat it as a SIS patientId
                # when it round-trips to a real SIS record via search would be
                # unreliable, so resolve by the numeric value directly only if
                # it is short (SIS patientIds are small ints like 135/53).
                # Otherwise resolve by name search.
                if len(pid) <= 6:
                    sis_pid = int(pid)
                else:
                    sis_pid = await mgr.sis_resolve_patient_id(pid)
            else:
                sis_pid = await mgr.sis_resolve_patient_id(pid)

            if sis_pid is None:
                ledgers.append({
                    "source": "sis",
                    "label": SIS_LABEL,
                    "found": False,
                    "balance": None,
                    "paid": None,
                    "status": None,
                    "insurance": None,
                    "patient_id_used": pid,
                    "cached": False,
                    "note": "Patient could not be resolved to a SIS patientId.",
                })
            else:
                bal = await mgr.sis_patient_balance(sis_pid)
                bal = bal or {}
                bal_err = bal.get("error")
                balance_val = bal.get("balance")
                ledgers.append({
                    "source": "sis",
                    "label": SIS_LABEL,
                    "found": bal_err is None,
                    "balance": balance_val,
                    "paid": None,
                    "status": None,
                    "insurance": None,
                    "patient_id_used": sis_pid,
                    "balance_source": bal.get("balance_source"),
                    "account_balance": bal.get("account_balance"),
                    "patient_balance": bal.get("patient_balance"),
                    "cached": False,
                    "note": bal.get("note") or "Live SIS Complete billing balance.",
                    **({"error": bal_err} if bal_err else {}),
                })
        except EMRSessionExpired as exc:
            expired.append("sis")
            _mcp_audit("check_billing", args_str, "session_expired", str(exc))
            ledgers.append({
                "source": "sis",
                "label": SIS_LABEL,
                "found": False,
                "error": "session_expired",
                "patient_id_used": pid,
                "cached": False,
            })
        except Exception as exc:
            _mcp_audit("check_billing", args_str, "error", str(exc))
            ledgers.append({
                "source": "sis",
                "label": SIS_LABEL,
                "found": False,
                "error": f"unexpected: {exc}",
                "patient_id_used": pid,
                "cached": False,
            })

    # ---- Svigg lane: resolve to an account number, then pull live ledger.
    if "svigg" in lanes:
        try:
            mgr = await _get_mgr()

            # patient_id may already be a Svigg account number: digits only
            # and 6+ chars → use directly as acct. Otherwise treat as a name.
            acct = ""
            rowid = ""  # REQUIRED to reach the ledger — acct-only hits the
                        # search form and returns zero charges (Bug 1a).
            insurance = None
            resolved_name = pid
            if pid.isdigit() and len(pid) >= 6:
                acct = pid
            else:
                # Name resolution: mirror svigg_live_lookup's parsing.
                parts = [p.strip() for p in pid.split(",", 1)]
                last_name = parts[0]
                first_name = parts[1] if len(parts) > 1 else ""
                if not first_name and " " in last_name:
                    space_parts = last_name.rsplit(" ", 1)
                    last_name = space_parts[-1]
                    first_name = space_parts[0]

                results = await mgr.svigg_search(last_name, first_name)
                if results:
                    first = results[0]
                    acct = (first.get("acct") or "").strip()
                    rowid = (first.get("rowid") or "").strip()
                    insurance = (
                        first.get("insurance_carrier")
                        or first.get("insurance_class")
                        or None
                    )
                    resolved_name = first.get("name") or pid

            if not acct:
                # Name didn't resolve to any Svigg account.
                ledgers.append({
                    "source": "svigg",
                    "label": SVIGG_LABEL,
                    "found": False,
                    "balance": None,
                    "paid": None,
                    "status": None,
                    "insurance": insurance,
                    "patient_id_used": pid,
                    "cached": False,
                    "note": "No matching Svigg patient/account found.",
                })
            else:
                ledger = await mgr.svigg_patient_ledger(acct, rowid)
                ledger = ledger or {}
                ledger_err = ledger.get("error")
                charges = ledger.get("charges") or []
                # An account that resolves is 'found' even when the charge
                # list is empty; only a hard ledger error means not-found.
                found = ledger_err is None
                entry = {
                    "source": "svigg",
                    "label": SVIGG_LABEL,
                    "found": found,
                    # balance/paid are not computable yet — the Svigg payments
                    # endpoint (/apps/pay/*) is unresolved (Bug 1b). Surface
                    # the live charge data we DO have rather than a stub.
                    "balance": ledger.get("balance"),
                    "paid": ledger.get("payments") or None,
                    "status": None,
                    "insurance": insurance,
                    "patient_id_used": acct,
                    "resolved_name": resolved_name,
                    "charges": charges,
                    "charge_count": ledger.get("count", len(charges)),
                    "cached": False,
                    "note": ledger.get("note"),
                }
                if not rowid:
                    # acct-only lookups can't reach the ledger (no rowid), so the
                    # charge list may be empty even when the account has charges.
                    entry["note"] = (
                        "Looked up by account number only — the Svigg ledger "
                        "requires a rowid (carried by a name search), so the charge "
                        "list may be empty. Look up by patient NAME for live charges. "
                    ) + (entry["note"] or "")
                if ledger_err:
                    entry["error"] = ledger_err
                ledgers.append(entry)

        except EMRSessionExpired as exc:
            expired.append("svigg")
            _mcp_audit("check_billing", args_str, "session_expired", str(exc))
            ledgers.append({
                "source": "svigg",
                "label": SVIGG_LABEL,
                "found": False,
                "error": "session_expired",
                "patient_id_used": pid,
                "cached": False,
            })
        except EMRBridgeError as exc:
            _mcp_audit("check_billing", args_str, "error", str(exc))
            ledgers.append({
                "source": "svigg",
                "label": SVIGG_LABEL,
                "found": False,
                "error": str(exc),
                "patient_id_used": pid,
                "cached": False,
            })
        except Exception as exc:
            _mcp_audit("check_billing", args_str, "error", str(exc))
            ledgers.append({
                "source": "svigg",
                "label": SVIGG_LABEL,
                "found": False,
                "error": f"unexpected: {exc}",
                "patient_id_used": pid,
                "cached": False,
            })

    _mcp_audit(
        "check_billing",
        args_str,
        "ok",
        f"lanes={','.join(lanes)} ledgers={len(ledgers)}",
    )
    return {
        "patient_id": patient_id,
        "source": source,
        "ledgers": ledgers,
        "session_expired": expired,
    }


# ---------------------------------------------------------------------------
# Tool: sis_session_status
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_session_status",
    description=(
        "Atlantic Pain & Wellness: check whether the SIS (SysComplete) browser "
        "session is still valid. READ-ONLY. Returns the session file mtime, "
        "estimated days until expiry, and a 'valid' boolean. "
        "No PHI is returned — this is a purely operational health check."
    ),
)
def sis_session_status() -> dict:
    """Returns {valid, expires_in_days, last_used} for the SIS browser state."""
    sis_path = ANTIGRAVITY_SCRATCH / "sis_browser_state.json"
    _mcp_audit("sis_session_status", "", "ok")

    if not sis_path.exists():
        return {
            "valid": False,
            "expires_in_days": None,
            "last_used": None,
            "note": "sis_browser_state.json not found — SIS has never been authenticated in this environment",
        }

    stat = sis_path.stat()
    mtime_ts = stat.st_mtime
    mtime_iso = datetime.fromtimestamp(mtime_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    age_days = (time.time() - mtime_ts) / 86400

    # Session files in Antigravity typically last ~30 days.
    SESSION_LIFETIME_DAYS = 30
    expires_in = max(0.0, SESSION_LIFETIME_DAYS - age_days)

    # Try reading the file to see if it looks structurally valid.
    valid = False
    try:
        raw = sis_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        # Playwright browser state JSON has a "cookies" key.
        valid = bool(data) and ("cookies" in data or "origins" in data)
    except Exception:
        valid = False

    return {
        "valid": valid and expires_in > 0,
        "expires_in_days": round(expires_in, 1),
        "last_used": mtime_iso,
        "age_days": round(age_days, 1),
        "note": (
            "Session age is estimated from the file's last-modified time. "
            "Real expiry depends on the SIS portal cookie TTL."
        ),
    }


# ---------------------------------------------------------------------------
# Tool: read_audit_log
# ---------------------------------------------------------------------------

@server.tool(
    name="read_audit_log",
    description=(
        "Atlantic Pain & Wellness: read recent rows from the EMR bridge audit log. "
        "READ-ONLY. Shows who called which tool, when, and whether it succeeded. "
        "Patient IDs in this table are hashed (not in the clear) per HIPAA §164.312(b). "
        "Use 'since_iso' (ISO-8601 UTC) to filter rows after a timestamp."
    ),
)
def read_audit_log(
    limit: int = 20,
    since_iso: str = "",
) -> dict:
    """
    Args:
        limit:     Maximum number of rows to return (default 20, max 200).
        since_iso: Optional ISO-8601 UTC timestamp; only rows after this are returned.
    """
    limit = min(max(1, int(limit)), 200)
    args_str = f"limit={limit} since_iso={since_iso!r}"

    try:
        emr_bridge._ensure_cache()
        conn = sqlite3.connect(str(CACHE_DB))
        try:
            if since_iso:
                cur = conn.execute(
                    """
                    SELECT id, ts, source, op, key, outcome, detail
                    FROM audit_log
                    WHERE ts > ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (since_iso, limit),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT id, ts, source, op, key, outcome, detail
                    FROM audit_log
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()

        _mcp_audit("read_audit_log", args_str, "ok", f"returned={len(rows)}")
        return {"rows": rows, "count": len(rows)}
    except Exception as exc:
        _mcp_audit("read_audit_log", args_str, "error", str(exc))
        return {"error": str(exc), "rows": [], "count": 0}


# ---------------------------------------------------------------------------
# Tool: clear_cache
# ---------------------------------------------------------------------------

@server.tool(
    name="clear_cache",
    description=(
        "Atlantic Pain & Wellness: delete cached EMR lookup results from the "
        "local SQLite cache so the next lookup fetches fresh data from the "
        "portal RPA scripts. READ-ONLY data mutation (cache only — no PHI "
        "records are deleted). "
        "source='all' clears every cached source."
    ),
)
def clear_cache(source: str = "all") -> dict:
    """
    Args:
        source: 'sis' | 'webedoctor' | 'all'
    """
    valid_sources = set(emr_bridge.EMR_SOURCES.keys()) | {"all"}
    if source not in valid_sources:
        return {"error": f"Unknown source {source!r}. Valid: {sorted(valid_sources)}", "cleared": 0}

    args_str = f"source={source!r}"
    try:
        emr_bridge._ensure_cache()
        conn = sqlite3.connect(str(CACHE_DB))
        try:
            if source == "all":
                cur = conn.execute("DELETE FROM emr_cache")
            else:
                cur = conn.execute("DELETE FROM emr_cache WHERE source=?", (source,))
            cleared = cur.rowcount
            conn.commit()
        finally:
            conn.close()

        _mcp_audit("clear_cache", args_str, "ok", f"cleared={cleared}")
        return {"cleared": cleared, "source": source}
    except Exception as exc:
        _mcp_audit("clear_cache", args_str, "error", str(exc))
        return {"error": str(exc), "cleared": 0}


# ---------------------------------------------------------------------------
# Tool: health
# ---------------------------------------------------------------------------

def _health() -> dict:
    """Internal health check — also called by the smoke test."""
    checks: dict[str, object] = {}

    # 1. emr_bridge importable
    try:
        _ = emr_bridge.EMR_SOURCES
        checks["emr_bridge"] = "ok"
    except Exception as exc:
        checks["emr_bridge"] = f"error: {exc}"

    # 2. Antigravity dir exists
    ag_dir = Path(os.environ.get("ANTIGRAVITY_DIR", "/Users/shubh/Documents/Antigravity"))
    checks["antigravity_dir"] = "ok" if ag_dir.is_dir() else f"missing: {ag_dir}"

    # 3. Cache DB writable
    try:
        emr_bridge._ensure_cache()
        # quick write probe
        conn = sqlite3.connect(str(CACHE_DB))
        conn.execute("SELECT COUNT(*) FROM emr_cache").fetchone()
        conn.close()
        checks["cache_db"] = "ok"
    except Exception as exc:
        checks["cache_db"] = f"error: {exc}"

    # 4. SIS state file exists
    sis_path = ANTIGRAVITY_SCRATCH / "sis_browser_state.json"
    checks["sis_state_file"] = "ok" if sis_path.exists() else f"missing: {sis_path}"

    # 5. Scratch dir readable
    checks["scratch_dir"] = (
        "ok" if ANTIGRAVITY_SCRATCH.is_dir() else f"missing: {ANTIGRAVITY_SCRATCH}"
    )

    overall = "ok" if all(str(v) == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}


@server.tool(
    name="health",
    description=(
        "Atlantic Pain & Wellness: check operational status of the EMR bridge — "
        "whether emr_bridge is importable, the Antigravity directory exists, "
        "the cache database is writable, and the SIS session state file is present. "
        "No PHI is accessed or returned."
    ),
)
def health() -> dict:
    result = _health()
    _mcp_audit("health", "", result["status"])
    return result


# ---------------------------------------------------------------------------
# Tool: svigg_live_lookup
# ---------------------------------------------------------------------------

@server.tool(
    name="svigg_live_lookup",
    description=(
        "Search Svigg/Dr.Com EMR directly for live patient data "
        "(demographics, insurance, cases, contacts). "
        "Requires Svigg credentials in .env (WEBEDOCTOR_USER / WEBEDOCTOR_PASS) "
        "and Playwright. Bypasses the subprocess wrapper — calls the new "
        "svigg_scraper.py directly for reliable live data. "
        "Returns PHI — do not echo to chat."
    ),
)
async def svigg_live_lookup(name: str) -> dict:
    """
    Args:
        name: Patient name to search. Supports 'Last, First' or 'First Last' format.
    """
    args_str = f"name={name!r}"
    try:
        # Split name into last, first if comma-separated ("Last, First")
        parts = [p.strip() for p in name.split(",", 1)]
        last_name = parts[0]
        first_name = parts[1] if len(parts) > 1 else ""

        # If space-separated ("First Last"), swap so last_name is the surname
        if not first_name and " " in last_name:
            space_parts = last_name.rsplit(" ", 1)
            last_name = space_parts[-1]
            first_name = space_parts[0]

        # Route through the singleton EMRSessionManager so concurrent callers
        # share ONE persistent Playwright context instead of each spawning a
        # fresh Chromium (which collides at ~20% ERR_ABORTED rate at 10 parallel
        # calls). Mirrors the sis_search wiring above.
        mgr = await _get_mgr()
        results = await mgr.svigg_search(last_name, first_name)
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("svigg_live_lookup", args_str, "ok", f"results={count}")
        return {
            "source": "svigg_live",
            "name": name,
            "count": count,
            "results": results,
        }
    except EMRSessionExpired as exc:
        _mcp_audit("svigg_live_lookup", args_str, "session_expired", str(exc))
        return {"source": "svigg_live", "error": "session_expired", "detail": str(exc)}
    except EMRBridgeError as exc:
        _mcp_audit("svigg_live_lookup", args_str, "error", str(exc))
        return {"source": "svigg_live", "error": str(exc)}
    except Exception as exc:
        _mcp_audit("svigg_live_lookup", args_str, "error", str(exc))
        return {"source": "svigg_live", "error": f"unexpected: {exc}"}


# ---------------------------------------------------------------------------
# Tool: book_appointment
# ---------------------------------------------------------------------------

@server.tool(
    name="book_appointment",
    description=(
        "Prepare a Svigg/Dr.Com appointment booking. "
        "PROPOSE-ONLY BY DEFAULT: with execute=False (the default) this walks "
        "the live add-appointment flow to the final pre-submit form and returns "
        "the fields it WOULD submit WITHOUT submitting — it creates NO "
        "appointment. The COMMIT path (actually booking) is UNVERIFIED pending a "
        "HAR capture of the bk_p wire contract and is DISABLED by default; it "
        "requires both a source-level flag AND confirm_unverified=True, and even "
        "then returns an explicit 'unverified' warning. "
        "Provide acct + rowid (preferred) or last_name to resolve them. "
        "Returns/handles PHI — do not echo patient data to chat."
    ),
)
async def book_appointment(
    acct: str = "",
    rowid: str = "",
    last_name: str = "",
    first_name: str = "",
    date: str = "",
    start_time: str = "",
    duration_min: int = 15,
    appt_type: str = "EST",
    provider: str = "SGUPTA",
    note: str = "",
    incident: str = "",
    x: int | None = None,
    y: int | None = None,
    execute: bool = False,
    confirm_unverified: bool = False,
) -> dict:
    """
    Args:
        acct: numeric patient account number (e.g. "2574961").
        rowid: Svigg internal patient rowid (e.g. "AAA...@main01").
        last_name/first_name: used to resolve+verify the patient if rowid/acct
            are not both supplied.
        date: appointment date, MM/DD/YYYY.
        start_time: appointment start time (e.g. "8:00a" / "08:00").
        duration_min: length in minutes.
        appt_type: cpt00 value — "EST" (established) or "NP" (new patient).
        provider: prov value (default "SGUPTA").
        note: free-text appointment note.
        incident: optional Incident radio value (case to bill — UNVERIFIED map).
        x, y: explicit free-slot grid coords; omit to use the first free slot.
        execute: DEFAULT False = propose only (never submits). True is still
            blocked unless the unverified commit path is explicitly enabled.
        confirm_unverified: explicit acknowledgement required (with the source
            flag) to ever attempt the unverified commit.
    """
    # Audit WITHOUT PHI: log only non-identifying call shape.
    args_str = (
        f"acct_set={bool(acct)} rowid_set={bool(rowid)} "
        f"name_set={bool(last_name)} date_set={bool(date)} "
        f"appt_type={appt_type!r} provider={provider!r} "
        f"execute={execute} confirm_unverified={confirm_unverified}"
    )
    try:
        mgr = await _get_mgr()
        result = await mgr.svigg_book_appointment(
            acct=acct,
            rowid=rowid,
            last_name=last_name,
            first_name=first_name,
            date=date,
            start_time=start_time,
            duration_min=duration_min,
            appt_type=appt_type,
            provider=provider,
            note=note,
            incident=incident,
            x=x,
            y=y,
            execute=execute,
            confirm_unverified=confirm_unverified,
        )
        status = result.get("status", "?") if isinstance(result, dict) else "?"
        _mcp_audit("book_appointment", args_str, "ok", f"status={status}")
        return result
    except EMRSessionExpired as exc:
        _mcp_audit("book_appointment", args_str, "session_expired", str(exc))
        return {"status": "error", "stage": "session",
                "error": "session_expired", "detail": str(exc)}
    except EMRBridgeError as exc:
        _mcp_audit("book_appointment", args_str, "error", str(exc))
        return {"status": "error", "stage": "bridge", "error": str(exc)}
    except Exception as exc:
        _mcp_audit("book_appointment", args_str, "error", str(exc))
        return {"status": "error", "stage": "unexpected",
                "error": f"unexpected: {exc}"}


# ---------------------------------------------------------------------------
# Tool: create_patient
# ---------------------------------------------------------------------------

@server.tool(
    name="create_patient",
    description=(
        "Create a NEW patient chart in the Svigg/Dr.Com/WEBeDoctor EMR. "
        "PROPOSE-ONLY BY DEFAULT: with dry_run=True (the default) this walks "
        "the live entry flow (new-patient form -> name-search DE-DUPE -> add "
        "form), DISCOVERS the add form's real fields from the DOM, and returns "
        "the fields it WOULD submit WITHOUT saving — it creates NO chart. It is "
        "IDEMPOTENT: if the de-dupe finds an existing chart for the same "
        "name/DOB it stops and returns 'duplicate_suspected' instead of adding "
        "a duplicate. The COMMIT path (dry_run=False) is UNVERIFIED (the Save "
        "POST is not in any HAR) and DISABLED by default; it requires both "
        "SVIGG_CREATE_EXECUTE=1 in the environment AND confirm_unverified=True, "
        "and even then fails closed at a 'save_unverified' terminus until the "
        "Save step is captured. Requires at least last_name + first_name; "
        "missing optional demographics render as honest blanks, never invented. "
        "Handles PHI — do not echo patient data to chat."
    ),
)
async def create_patient(
    last_name: str,
    first_name: str,
    mi: str = "",
    dob: str = "",
    ssn: str = "",
    sex: str = "",
    address: str = "",
    address2: str = "",
    city: str = "",
    state: str = "",
    zip: str = "",
    home_phone: str = "",
    cell_phone: str = "",
    work_phone: str = "",
    email: str = "",
    dry_run: bool = True,
    confirm_unverified: bool = False,
) -> dict:
    """
    Args:
        last_name/first_name: REQUIRED — a blank name is never fabricated.
        mi: middle initial.
        dob: date of birth, MM/DD/YYYY (also used in the de-dupe search).
        ssn: social security number (also used in the de-dupe search).
        sex, address, address2, city, state, zip, home_phone, cell_phone,
            work_phone, email: optional demographics; omitted -> honest blank.
        dry_run: DEFAULT True = propose only (discovers fields, submits
            nothing). False attempts the gated, unverified Save.
        confirm_unverified: explicit acknowledgement required (with the
            SVIGG_CREATE_EXECUTE env flag) to ever attempt a Save.
    """
    # Audit WITHOUT PHI: log only non-identifying call shape.
    args_str = (
        f"name_set={bool(last_name and first_name)} dob_set={bool(dob)} "
        f"ssn_set={bool(ssn)} dry_run={dry_run} "
        f"confirm_unverified={confirm_unverified}"
    )
    demographics = {
        "last_name": last_name, "first_name": first_name, "mi": mi,
        "dob": dob, "ssn": ssn, "sex": sex,
        "address": address, "address2": address2, "city": city,
        "state": state, "zip": zip,
        "home_phone": home_phone, "cell_phone": cell_phone,
        "work_phone": work_phone, "email": email,
    }
    try:
        mgr = await _get_mgr()
        result = await mgr.svigg_create_patient(
            demographics,
            dry_run=dry_run,
            confirm_unverified=confirm_unverified,
        )
        status = result.get("status", "?") if isinstance(result, dict) else "?"
        _mcp_audit("create_patient", args_str, "ok", f"status={status}")
        return result
    except EMRSessionExpired as exc:
        _mcp_audit("create_patient", args_str, "session_expired", str(exc))
        return {"status": "error", "stage": "session",
                "error": "session_expired", "detail": str(exc)}
    except EMRBridgeError as exc:
        _mcp_audit("create_patient", args_str, "error", str(exc))
        return {"status": "error", "stage": "bridge", "error": str(exc)}
    except Exception as exc:
        _mcp_audit("create_patient", args_str, "error", str(exc))
        return {"status": "error", "stage": "unexpected",
                "error": f"unexpected: {exc}"}


# ---------------------------------------------------------------------------
# Tool: svigg_status
# ---------------------------------------------------------------------------

@server.tool(
    name="svigg_status",
    description=(
        "Check if Svigg/Dr.Com EMR connection is healthy — credentials configured "
        "in .env and login works. No PHI is accessed or returned."
    ),
)
async def svigg_status() -> dict:
    """Returns {credential_configured, login_ok, detail}."""
    import os

    user = os.environ.get("WEBEDOCTOR_USER", "")
    pw = os.environ.get("WEBEDOCTOR_PASS", "")
    credential_configured = bool(user and pw)

    if not credential_configured:
        _mcp_audit("svigg_status", "", "ok", "credentials_missing")
        return {
            "credential_configured": False,
            "login_ok": False,
            "detail": "WEBEDOCTOR_USER and/or WEBEDOCTOR_PASS not set in .env",
        }

    # Attempt a real login to verify credentials are still valid.
    try:
        # Import here so missing Playwright doesn't break the whole server.
        from svigg_scraper import SviggScraper  # noqa: PLC0415

        scraper = SviggScraper(headless=True)
        try:
            await scraper.start()
            login_ok = await scraper.login()
        finally:
            await scraper.stop()

        outcome = "ok" if login_ok else "login_failed"
        _mcp_audit("svigg_status", "", outcome)
        return {
            "credential_configured": True,
            "login_ok": login_ok,
            "detail": "Login succeeded" if login_ok else "Login failed — credentials may be wrong or portal is down",
        }
    except Exception as exc:
        _mcp_audit("svigg_status", "", "error", str(exc))
        return {
            "credential_configured": True,
            "login_ok": False,
            "detail": f"Error during login probe: {exc}",
        }


# ---------------------------------------------------------------------------
# Tool: sis_search
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_search",
    description=(
        "Atlantic Pain & Wellness: search SIS Complete EMR directly via its REST API. "
        "Searches by patient name, MRN, or DOB. READ-ONLY. "
        "Returns PHI — do not echo to chat or store outside audit log."
    ),
)
async def sis_search(query: str) -> dict:
    """
    Args:
        query: Patient name, MRN, or DOB to search.
    """
    args_str = f"query={query!r}"
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_search(query)
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_search", args_str, "ok", f"results={count}")
        return {"source": "sis_live", "query": query, "count": count, "results": results}
    except Exception as exc:
        _mcp_audit("sis_search", args_str, "error", str(exc))
        return {"source": "sis_live", "query": query, "error": str(exc), "results": []}


# ---------------------------------------------------------------------------
# Tool: sis_todays_patients
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_todays_patients",
    description=(
        "Atlantic Pain & Wellness: get today's patient tracker from SIS Complete — "
        "lists surgical cases scheduled for today with status. READ-ONLY. "
        "Returns PHI — do not echo to chat."
    ),
)
async def sis_todays_patients() -> dict:
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_todays_patients()
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_todays_patients", "", "ok", f"cases={count}")
        return {"source": "sis_live", "date": "today", "count": count, "patients": results}
    except Exception as exc:
        _mcp_audit("sis_todays_patients", "", "error", str(exc))
        return {"source": "sis_live", "error": str(exc), "patients": []}


# ---------------------------------------------------------------------------
# Tool: sis_unsigned_cases
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_unsigned_cases",
    description=(
        "Atlantic Pain & Wellness: get unsigned/uncancelled cases from SIS Complete. "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def sis_unsigned_cases() -> dict:
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_unsigned_cases()
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_unsigned_cases", "", "ok", f"cases={count}")
        return {"source": "sis_live", "count": count, "cases": results}
    except Exception as exc:
        _mcp_audit("sis_unsigned_cases", "", "error", str(exc))
        return {"source": "sis_live", "error": str(exc), "cases": []}


# ---------------------------------------------------------------------------
# Tool: sis_rooms
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_rooms",
    description=(
        "Atlantic Pain & Wellness: list OR/procedure rooms from SIS Complete. "
        "READ-ONLY. No PHI — room configuration data only."
    ),
)
async def sis_rooms() -> dict:
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_rooms()
        _mcp_audit("sis_rooms", "", "ok", f"rooms={len(results)}")
        return {"source": "sis_live", "rooms": results}
    except Exception as exc:
        _mcp_audit("sis_rooms", "", "error", str(exc))
        return {"source": "sis_live", "error": str(exc), "rooms": []}


# ---------------------------------------------------------------------------
# Tool: emr_combined_search
# ---------------------------------------------------------------------------

@server.tool(
    name="emr_combined_search",
    description=(
        "Atlantic Pain & Wellness: search BOTH EMRs simultaneously — SIS Complete "
        "(clinical/surgical) and Svigg/Dr.Com (billing/appointments). "
        "Returns combined results from both systems. READ-ONLY. "
        "Returns PHI — do not echo to chat."
    ),
)
async def emr_combined_search(query: str) -> dict:
    """
    Args:
        query: Patient name to search across both EMRs.
    """
    args_str = f"query={query!r}"
    try:
        mgr = await _get_mgr()
        results = await mgr.combined_search(query)
        _mcp_audit("emr_combined_search", args_str, "ok")
        return results
    except Exception as exc:
        _mcp_audit("emr_combined_search", args_str, "error", str(exc))
        return {"query": query, "error": str(exc), "sis_results": [], "svigg_results": []}


# ---------------------------------------------------------------------------
# Tool: emr_connection_health
# ---------------------------------------------------------------------------

@server.tool(
    name="emr_connection_health",
    description=(
        "Atlantic Pain & Wellness: check live connection status of BOTH EMRs — "
        "SIS Complete and Svigg/Dr.Com. Shows whether each portal is authenticated "
        "and reachable. No PHI is accessed or returned."
    ),
)
async def emr_connection_health() -> dict:
    try:
        mgr = await _get_mgr()
        result = await mgr.health()
        _mcp_audit("emr_connection_health", "", "ok")
        return result
    except Exception as exc:
        _mcp_audit("emr_connection_health", "", "error", str(exc))
        return {"error": str(exc), "sis": {"status": "error"}, "svigg": {"status": "error"}}


# ---------------------------------------------------------------------------
# Tool: sis_schedule_week
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_schedule_week",
    description=(
        "Atlantic Pain & Wellness: fetch the surgical schedule from SIS Complete "
        "for a full Mon-Sun week. Snaps to the most recent Monday on or before "
        "start_date (defaults to current week). "
        "READ-ONLY. Returns PHI (patient names, case types, room assignments) — "
        "do not echo raw output to chat."
    ),
)
async def sis_schedule_week(start_date: str = None) -> dict:
    """
    Args:
        start_date: Any ISO date YYYY-MM-DD in the desired week (defaults to today).
    """
    args_str = f"start_date={start_date!r}"
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_schedule_week(start_date)
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_schedule_week", args_str, "ok", f"events={count}")
        return {"source": "sis_live", "start_date": start_date, "count": count, "schedule": results}
    except Exception as exc:
        _mcp_audit("sis_schedule_week", args_str, "error", str(exc))
        return {"source": "sis_live", "start_date": start_date, "error": str(exc), "schedule": []}


# ---------------------------------------------------------------------------
# Tool: sis_schedule_day
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_schedule_day",
    description=(
        "Atlantic Pain & Wellness: fetch the surgical schedule from SIS Complete "
        "for a single day. "
        "READ-ONLY. Returns PHI (patient names, case types, room assignments) — "
        "do not echo raw output to chat."
    ),
)
async def sis_schedule_day(date: str = None) -> dict:
    """
    Args:
        date: ISO date YYYY-MM-DD (defaults to today).
    """
    args_str = f"date={date!r}"
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_schedule_day(date)
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_schedule_day", args_str, "ok", f"events={count}")
        return {"source": "sis_live", "date": date, "count": count, "schedule": results}
    except Exception as exc:
        _mcp_audit("sis_schedule_day", args_str, "error", str(exc))
        return {"source": "sis_live", "date": date, "error": str(exc), "schedule": []}


# ---------------------------------------------------------------------------
# Tool: sis_case_details
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_case_details",
    description=(
        "Atlantic Pain & Wellness: fetch detailed case record from SIS Complete "
        "by caseSummaryId. Endpoint verified live (2026-06-28): "
        "GET /api/CaseSummary/{id} returns ~54-key dict "
        "(procedureDt, primaryPhysicianId, referringPhysicianName, "
        "caseAccountNumber, primaryProcedure, roomName, etc.). "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def sis_case_details(case_id: int) -> dict:
    """
    Args:
        case_id: SIS caseSummaryId (integer).
    """
    args_str = f"case_id={case_id}"
    try:
        mgr = await _get_mgr()
        result = await mgr.sis_case_details(case_id)
        has_error = "error" in result
        _mcp_audit("sis_case_details", args_str, "error" if has_error else "ok")
        return {"source": "sis_live", "case_id": case_id, "detail": result}
    except Exception as exc:
        _mcp_audit("sis_case_details", args_str, "error", str(exc))
        return {"source": "sis_live", "case_id": case_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: sis_patient_details
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_details",
    description=(
        "Atlantic Pain & Wellness: fetch detailed patient demographics from SIS "
        "Complete by patientId. Endpoint verified live (2026-06-28): "
        "GET /api/PatientData/Gemini/GetPatient/{id} returns ~62-key dict "
        "(firstName, lastName, dateOfBirth, gender, email, primaryAccountNumber, "
        "insurance/emergency-contact fields, etc.). "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def sis_patient_details(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        result = await mgr.sis_patient_details(patient_id)
        has_error = "error" in result
        _mcp_audit("sis_patient_details", args_str, "error" if has_error else "ok")
        return {"source": "sis_live", "patient_id": patient_id, "detail": result}
    except Exception as exc:
        _mcp_audit("sis_patient_details", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: sis_patient_ledger
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_ledger",
    description=(
        "Atlantic Pain & Wellness: fetch the LIVE billing ledger, aging "
        "breakdown, and a single resolved balance for a patient from SIS "
        "Complete by patientId. Endpoints verified live (2026-06-30): "
        "GET /api/CaseToChargeComplex/GetFaceSheetLedgerModelByPatient/{id} "
        "(charges + aging) plus a resolved balance from the statements tracker "
        "(accountBalance) or, as a fallback, derived from ledger aging totals. "
        "READ-ONLY — does not post or modify any charge or payment. "
        "Returns PHI (charges, balances, insurance) — do not echo to chat."
    ),
)
async def sis_patient_ledger(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        ledger = await mgr.sis_patient_billing_ledger(int(patient_id))
        balance = await mgr.sis_patient_balance(int(patient_id))
        ledger = ledger or {}
        has_error = "error" in ledger
        charges = ledger.get("faceSheetLedgerCharges") or []
        charge_count = len(charges) if isinstance(charges, list) else 0
        _mcp_audit(
            "sis_patient_ledger", args_str,
            "error" if has_error else "ok",
            f"charges={charge_count} balance_src={balance.get('balance_source')}",
        )
        return {
            "source": "sis_live",
            "patient_id": patient_id,
            "found": not has_error,
            "balance": balance.get("balance"),
            "balance_source": balance.get("balance_source"),
            "account_balance": balance.get("account_balance"),
            "patient_balance": balance.get("patient_balance"),
            "aging": ledger.get("aging"),
            "charges": charges,
            "charge_count": charge_count,
            "error": ledger.get("error"),
        }
    except Exception as exc:
        _mcp_audit("sis_patient_ledger", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: sis_ar_tracker
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_ar_tracker",
    description=(
        "Atlantic Pain & Wellness: fetch practice-wide accounts-receivable (AR) "
        "rows from SIS Complete's revenue-cycle tracker. Endpoint verified live "
        "(2026-06-30): POST /api/RCMTracker/RCMTrackerDetails for "
        "organizationId=3 (Main Line Surgical Center) returned ~690 rows. "
        "Each row: caseSummaryId, patientId, patientName, dateOfSurgery, "
        "balance, postDate, responsibleParty, primaryAccountNumber, "
        "followUpDate, status, classification, aging, claimStatus. "
        "READ-ONLY. Returns PHI (patient names, balances) — do not echo to chat."
    ),
)
async def sis_ar_tracker(page_number: int = 1, page_size: int = 25) -> dict:
    """
    Args:
        page_number: 1-based page of the AR tracker (defaults to 1).
        page_size: rows per page (defaults to 25).
    """
    args_str = f"page_number={page_number} page_size={page_size}"
    try:
        mgr = await _get_mgr()
        rows = await mgr.sis_ar_tracker(
            organization_id=3, page_number=int(page_number), page_size=int(page_size)
        )
        count = len(rows) if isinstance(rows, list) else 0
        _mcp_audit("sis_ar_tracker", args_str, "ok", f"rows={count}")
        return {
            "source": "sis_live",
            "organization_id": 3,
            "page_number": page_number,
            "page_size": page_size,
            "count": count,
            "rows": rows,
        }
    except Exception as exc:
        _mcp_audit("sis_ar_tracker", args_str, "error", str(exc))
        return {"source": "sis_live", "page_number": page_number, "error": str(exc), "rows": []}


# ---------------------------------------------------------------------------
# Tool: sis_patient_insurance
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_insurance",
    description=(
        "Atlantic Pain & Wellness: fetch insurance records for a patient from "
        "SIS Complete by patientId. Endpoint verified live (2026-06-30): "
        "GET /api/Insurance/PatientInsurances/{id}. Each record: carrierId, "
        "carrier, displayName, planName, groupNumber, authorizationNumber, "
        "insuredId, effectiveFrom/To, isActive. "
        "READ-ONLY. Returns PHI (insurance/coverage) — do not echo to chat."
    ),
)
async def sis_patient_insurance(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        rows = await mgr.sis_patient_insurance(int(patient_id))
        count = len(rows) if isinstance(rows, list) else 0
        _mcp_audit("sis_patient_insurance", args_str, "ok", f"records={count}")
        return {
            "source": "sis_live",
            "patient_id": patient_id,
            "count": count,
            "insurance": rows,
        }
    except Exception as exc:
        _mcp_audit("sis_patient_insurance", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id, "error": str(exc), "insurance": []}


# ---------------------------------------------------------------------------
# Tool: sis_patient_record  (PRIMARY — consolidated patient record)
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_record",
    description=(
        "Atlantic Pain & Wellness: pull a patient's WHOLE record from SIS "
        "Complete in one call, by patientId. Consolidates demographics, "
        "insurance (face sheet), cases, dates-of-service, and the resolved "
        "billing balance. Endpoints verified live (2026-06-30). "
        "GRACEFUL DEGRADATION: any section that fails is listed in '_partial' "
        "and the rest of the record is still returned — the call never fails "
        "wholesale because one piece failed. "
        "READ-ONLY. Returns PHI (demographics, insurance, balances) — do not "
        "echo to chat."
    ),
)
async def sis_patient_record(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        record = await mgr.sis_patient_record(int(patient_id))
        record = record or {}
        partial = record.get("_partial") or []
        has_error = "error" in record
        outcome = "error" if has_error else ("partial" if partial else "ok")
        # Audit PHI-free: only section availability + count of failed sections.
        sections = [k for k in ("demographics", "insurance", "cases",
                                "dates_of_service", "billing_balance")
                    if record.get(k) is not None]
        _mcp_audit(
            "sis_patient_record", args_str, outcome,
            f"sections={len(sections)} partial={len(partial)}",
        )
        return {"source": "sis_live", "patient_id": patient_id, "record": record}
    except Exception as exc:
        _mcp_audit("sis_patient_record", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: sis_patient_demographics
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_demographics",
    description=(
        "Atlantic Pain & Wellness: fetch full patient demographics from SIS "
        "Complete by patientId (~62-65 key dict: firstName, lastName, "
        "dateOfBirth, gender, address, patientPhones, accountNumbers, ...). "
        "Endpoint verified live (2026-06-30): "
        "GET /api/PatientData/Gemini/GetPatient/{id}. "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def sis_patient_demographics(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        result = await mgr.sis_patient_demographics(int(patient_id))
        has_error = isinstance(result, dict) and "error" in result
        _mcp_audit("sis_patient_demographics", args_str, "error" if has_error else "ok")
        return {"source": "sis_live", "patient_id": patient_id, "detail": result}
    except Exception as exc:
        _mcp_audit("sis_patient_demographics", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: sis_patient_cases
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_cases",
    description=(
        "Atlantic Pain & Wellness: list a patient's surgical cases from SIS "
        "Complete by patientId (lean case list: caseSummaryId, physician, "
        "procedureNameList, procedureStartDt/StopDt, caseStatus, caseType, "
        "caseAccountNumber, caseCode). Endpoint verified live (2026-06-30): "
        "GET /api/CaseSummary/GetFaceSheetCaseListByPatient/{id}. "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def sis_patient_cases(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_patient_cases(int(patient_id))
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_patient_cases", args_str, "ok", f"cases={count}")
        return {"source": "sis_live", "patient_id": patient_id,
                "count": count, "cases": results}
    except Exception as exc:
        _mcp_audit("sis_patient_cases", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id,
                "error": str(exc), "cases": []}


# ---------------------------------------------------------------------------
# Tool: sis_patient_notes
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_notes",
    description=(
        "Atlantic Pain & Wellness: fetch a patient's clinical/administrative "
        "notes from SIS Complete by patientId. Endpoint verified live "
        "(2026-07-01): GET /api/PatientNotes/GetPatientNotes?patientId={id}. "
        "Each note row: patientNoteId, note, title, categoryName, categoryCode, "
        "noteCategoryId, associatedCase, createdDate, createdByAlias, "
        "importantFlagTf. An empty list means the patient has no notes. "
        "READ-ONLY. Returns PHI (note bodies) — do not echo to chat."
    ),
)
async def sis_patient_notes(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_patient_notes(int(patient_id))
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_patient_notes", args_str, "ok", f"notes={count}")
        return {"source": "sis_live", "patient_id": patient_id,
                "count": count, "notes": results}
    except Exception as exc:
        _mcp_audit("sis_patient_notes", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id,
                "error": str(exc), "notes": []}


# ---------------------------------------------------------------------------
# Tool: sis_patient_allergies
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_allergies",
    description=(
        "Atlantic Pain & Wellness: fetch a patient's allergy history from SIS "
        "Complete by patientId. Endpoint verified live (2026-07-01): "
        "GET /api/PatientData/{id}/allergyHistory. Each row: allergen, "
        "allergenId, allergyCategory, reactions, reactionsText, severity, "
        "onsetDt, noKnown, activeTf, patientAllergyHistoryId. An empty list "
        "means no recorded allergy rows (demographics may still carry the "
        "noKnownDrugAllergyTf / noKnownLatexAllergyTf flags). "
        "READ-ONLY. Returns PHI (allergies) — do not echo to chat."
    ),
)
async def sis_patient_allergies(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_patient_allergies(int(patient_id))
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_patient_allergies", args_str, "ok", f"allergies={count}")
        return {"source": "sis_live", "patient_id": patient_id,
                "count": count, "allergies": results}
    except Exception as exc:
        _mcp_audit("sis_patient_allergies", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id,
                "error": str(exc), "allergies": []}


# ---------------------------------------------------------------------------
# Tool: sis_patient_medications
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_patient_medications",
    description=(
        "Atlantic Pain & Wellness: fetch a patient's medication list from SIS "
        "Complete by patientId, resolved case-by-case. SIS meds are case-scoped: "
        "this resolves the patient's cases then queries per case via "
        "GET /api/Medication/{caseSummaryId}/0 and "
        "GET /api/Depletion/GetMedicationsByCaseSummaryId/{caseSummaryId} "
        "(both verified live 2026-07-01 to return HTTP 200). "
        "NOTE (2026-07-02): the meds module is UNUSED by this practice — a "
        "caseSummaryId 1-500 sweep of both endpoints (1000 calls) returned empty "
        "every time, matching the 120-case scan and the practice's own HAR "
        "traffic; Svigg's Rx page is likewise empty. Expect [] for every patient; "
        "meds live in chart notes/case-pack documents, not structured EMR fields. "
        "Rows (if ever present) are returned as-is, tagged with _caseSummaryId. "
        "READ-ONLY. Returns PHI (medications) — do not echo to chat."
    ),
)
async def sis_patient_medications(patient_id: int) -> dict:
    """
    Args:
        patient_id: SIS patientId (integer).
    """
    args_str = f"patient_id={patient_id}"
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_patient_medications(int(patient_id))
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_patient_medications", args_str, "ok", f"medications={count}")
        return {"source": "sis_live", "patient_id": patient_id,
                "count": count, "medications": results}
    except Exception as exc:
        _mcp_audit("sis_patient_medications", args_str, "error", str(exc))
        return {"source": "sis_live", "patient_id": patient_id,
                "error": str(exc), "medications": []}


# ---------------------------------------------------------------------------
# Tool: sis_staff_roster
# ---------------------------------------------------------------------------

@server.tool(
    name="sis_staff_roster",
    description=(
        "Atlantic Pain & Wellness: fetch the SIS Complete organization staff "
        "roster (staffId, roleName, firstName, lastName, fullName, title, "
        "primaryPhone, emailAddress). org_id=3 = Main Line Surgical Center. "
        "Endpoint verified live (2026-06-30, 12 rows): "
        "GET /api/StaffList/GetStaffForOrganization/{org}/false. "
        "READ-ONLY. Staff directory — no patient PHI."
    ),
)
async def sis_staff_roster(org_id: int = 3) -> dict:
    """
    Args:
        org_id: SIS organizationId (default 3 = Main Line Surgical Center).
    """
    args_str = f"org_id={org_id}"
    try:
        mgr = await _get_mgr()
        results = await mgr.sis_staff_roster(int(org_id))
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("sis_staff_roster", args_str, "ok", f"staff={count}")
        return {"source": "sis_live", "org_id": org_id,
                "count": count, "staff": results}
    except Exception as exc:
        _mcp_audit("sis_staff_roster", args_str, "error", str(exc))
        return {"source": "sis_live", "org_id": org_id,
                "error": str(exc), "staff": []}


# ---------------------------------------------------------------------------
# Tool: svigg_patient_ledger
# ---------------------------------------------------------------------------

@server.tool(
    name="svigg_patient_ledger",
    description=(
        "Atlantic Pain & Wellness: fetch the visit/charge ledger for a patient "
        "from Svigg/Dr.Com EMR. Endpoint verified live (2026-06-29): "
        "/proxy.cgi/apps/ven/elist.htm?acct={acct}&rowid={rowid}. "
        "PASS THE rowid: acct-only lands on the Visit-Entry search form and "
        "returns ZERO charges. The rowid is carried alongside acct by "
        "svigg_live_lookup / search results — pass it here. If you only have "
        "the acct, also pass last_name to resolve the rowid via a search. "
        "Returns charges list (batch#, visit date, provider, $ expected/entered). "
        "Returns payments and a computed balance via the ledgerRptcases.htm "
        "report (payments now resolved, 2026-06-30). "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def svigg_patient_ledger(account_number: str, rowid: str = "", last_name: str = "") -> dict:
    """
    Args:
        account_number: Svigg account number (acct) for the patient.
        rowid: Svigg rowid for the patient (carried alongside acct by
            svigg_live_lookup / search results). REQUIRED to reach the real
            charges ledger — without it Svigg serves the Visit-Entry search
            form and zero charges come back. Always pass it when you have it.
        last_name: Optional. When rowid is not supplied, a search round-trip on
            this last name resolves the rowid by matching the account number.
    """
    args_str = f"account_number={account_number!r} rowid={rowid!r}"
    try:
        mgr = await _get_mgr()
        # Resolve rowid via a search round-trip when only acct (+ name) is known.
        # acct-only cannot reach the ledger, so recover the rowid first.
        if not rowid and last_name:
            try:
                matches = await mgr.svigg_search(last_name)
                for m in matches or []:
                    if str(m.get("acct")) == str(account_number) and m.get("rowid"):
                        rowid = m.get("rowid")
                        break
            except Exception:
                pass  # fall through to acct-only (returns zero charges + warns)
            args_str = f"account_number={account_number!r} rowid={rowid!r}"
        result = await mgr.svigg_patient_ledger(account_number, rowid)
        count = result.get("count", len(result.get("charges", [])))
        outcome = "error" if "error" in result else "ok"
        _mcp_audit("svigg_patient_ledger", args_str, outcome, f"charges={count}")
        return result
    except Exception as exc:
        _mcp_audit("svigg_patient_ledger", args_str, "error", str(exc))
        return {
            "source": "svigg_live",
            "account_number": account_number,
            "error": str(exc),
            "charges": [],
            "payments": [],
            "balance": None,
        }


# ---------------------------------------------------------------------------
# Tool: svigg_cancel_appointment  (DESTRUCTIVE — fail-closed, gated to allowlist)
# ---------------------------------------------------------------------------

@server.tool(
    name="svigg_cancel_appointment",
    description=(
        "Atlantic Pain & Wellness: CANCEL (delete) a Svigg/Dr.Com appointment for "
        "a patient on a given date. DESTRUCTIVE and FAIL-CLOSED — does nothing "
        "unless confirm=True AND acct is in the booking allowlist (currently the "
        "designated test record only). Flow: locate the appt cell on the date's "
        "grid by last_name -> mre edit form -> Delete -> Confirm Cancellation "
        "(CancelReason 'or'=Office / 'pr'=Patient) -> Yes; then re-reads the "
        "calendar to verify removal. Optional time / appointment_ref disambiguate "
        "when the patient has multiple appointments the same day; if the name still "
        "matches more than one row this returns status='ambiguous' with candidates "
        "(a safe refusal) rather than guessing. The cancel FLOW was verified live "
        "2026-07-01, but this MCP wrapper has NOT yet been re-exercised end-to-end "
        "— treat the first live use as a supervised test. Reschedule is NOT "
        "implemented (downstream contract unverified). WRITES to the live EMR."
    ),
)
async def svigg_cancel_appointment(
    acct: str, date: str, last_name: str = "", first_name: str = "",
    time: str = "", appointment_ref: str = "",
    reason: str = "or", confirm: bool = False,
) -> dict:
    """
    Args:
        acct: Svigg account number (gate key — must be allowlisted).
        date: appointment date, ISO YYYY-MM-DD.
        last_name/first_name: locate the appt cell + identity-guard the edit form.
        time: optional appointment start time ("10:00AM"/"14:30") to disambiguate
            multiple same-day appointments.
        appointment_ref: exact Svigg cell ref (r= from svigg_appointment_calendar) —
            takes precedence over time.
        reason: CancelReason — 'or' (Office Requested, default) or 'pr' (Patient Requested).
        confirm: MUST be True to perform the cancel (fail-closed).
    """
    args_str = (f"acct={acct!r} date={date!r} time={time!r} "
                f"ref={appointment_ref!r} confirm={confirm}")
    try:
        mgr = await _get_mgr()
        result = await mgr.svigg_cancel_appointment(
            acct=acct, date=date, last_name=last_name,
            first_name=first_name, time=time, appointment_ref=appointment_ref,
            reason=reason, confirm=confirm,
        )
        status = result.get("status")
        outcome = "ok" if status in ("cancelled", "not_found", "ambiguous", "execute_blocked") else "error"
        _mcp_audit("svigg_cancel_appointment", args_str, outcome, f"status={status}")
        return result
    except Exception as exc:
        _mcp_audit("svigg_cancel_appointment", args_str, "error", str(exc))
        return {"status": "error", "source": "svigg_live", "acct": acct, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: svigg_patient_appointments
# ---------------------------------------------------------------------------

@server.tool(
    name="svigg_patient_appointments",
    description=(
        "Atlantic Pain & Wellness: get appointment history for a patient from "
        "Svigg/Dr.Com by account number. "
        "NOTE: Svigg has no dedicated per-patient appointment endpoint (verified "
        "2026-06-28 — 15 candidate URLs all returned 'Sorry'). "
        "Appointment history is inferred from the visit ledger's visit_date column. "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def svigg_patient_appointments(account_number: str) -> dict:
    """
    Args:
        account_number: Svigg account number (acct) for the patient.
    """
    args_str = f"account_number={account_number!r}"
    try:
        mgr = await _get_mgr()
        results = await mgr.svigg_patient_appointments(account_number)
        count = len(results) if isinstance(results, list) else 0
        _mcp_audit("svigg_patient_appointments", args_str, "ok", f"visits={count}")
        return {
            "source": "svigg_live",
            "account_number": account_number,
            "count": count,
            "appointments": results,
            "note": "Inferred from visit ledger — Svigg has no standalone appointments endpoint",
        }
    except Exception as exc:
        _mcp_audit("svigg_patient_appointments", args_str, "error", str(exc))
        return {
            "source": "svigg_live",
            "account_number": account_number,
            "error": str(exc),
            "appointments": [],
        }


# ---------------------------------------------------------------------------
# Tool: svigg_appointment_calendar
# ---------------------------------------------------------------------------

@server.tool(
    name="svigg_appointment_calendar",
    description=(
        "Atlantic Pain & Wellness: scrape the Svigg/Dr.Com global appointment "
        "calendar (book.htm frame) for a given date. "
        "Verified live (2026-06-28): cal.htm is a frameset; appointment data is "
        "in the book.htm child frame with 23 tables. Cell bgcolor encodes status: "
        "#FFCCFF=booked, #CCFFCC=arrived, #FFFF99=confirmed. "
        "Returns list of {patient_name, appointment_type, duration_minutes, "
        "status, appointment_ref} dicts (appointment_ref = the stable Svigg "
        "rowid per record). Requires Playwright and an active Svigg session. "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def svigg_appointment_calendar(date: str = None) -> dict:
    """
    Args:
        date: ISO date YYYY-MM-DD (defaults to today).
    """
    args_str = f"date={date!r}"
    try:
        mgr = await _get_mgr()
        results = await mgr.svigg_appointment_calendar(date)
        count = len(results) if isinstance(results, list) else 0
        has_error = any("error" in r for r in results) if isinstance(results, list) else False
        outcome = "partial_error" if has_error else "ok"
        _mcp_audit("svigg_appointment_calendar", args_str, outcome, f"appointments={count}")
        return {"source": "svigg_live", "date": date, "count": count, "appointments": results}
    except Exception as exc:
        _mcp_audit("svigg_appointment_calendar", args_str, "error", str(exc))
        return {"source": "svigg_live", "date": date, "error": str(exc), "appointments": []}


@server.tool(
    name="svigg_schedule_day",
    description=(
        "Atlantic Pain & Wellness: read the Svigg / Dr.Com / WEBeDoctor per-day "
        "'Appointments' SCHEDULE report for a single day, selected by integer day "
        "offset from today (0=today, 1=tomorrow, ...). This is the front-desk "
        "schedule VIEW (one row per appointment, with a reliable per-day date and "
        "an 'N Patients on Schedule' footer) — NOT the booking grid. READ-ONLY: "
        "does not create, move, or cancel any appointment. Returns PHI (patient "
        "names, insurance, notes) — do not echo raw output to chat. Returns "
        "{date, day_offset, count, appointments:[...]}."
    ),
)
async def svigg_schedule_day(day_offset: int = 0) -> dict:
    """
    Args:
        day_offset: integer days from today (0=today). The schedule report is
            per-day, so a week = call this for offsets 0..7.
    """
    args_str = f"day_offset={day_offset!r}"
    try:
        mgr = await _get_mgr()
        result = await mgr.svigg_schedule_day(day_offset)
        count = result.get("count", 0)
        outcome = "error" if result.get("error") else "ok"
        _mcp_audit("svigg_schedule_day", args_str, outcome,
                   f"date={result.get('date')} count={count}")
        return result
    except Exception as exc:
        _mcp_audit("svigg_schedule_day", args_str, "error", str(exc))
        return {"date": "", "day_offset": day_offset, "count": 0,
                "appointments": [], "error": str(exc), "source": "svigg_live"}


# ---------------------------------------------------------------------------
# Tool: emr_combined_schedule
# ---------------------------------------------------------------------------

@server.tool(
    name="emr_combined_schedule",
    description=(
        "Atlantic Pain & Wellness: fetch schedules from BOTH EMRs in parallel — "
        "SIS Complete (surgical cases, Mon-Sun week) and Svigg/Dr.Com "
        "(office appointment calendar, single day). "
        "Returns merged dict with SIS surgical cases + Svigg office appointments. "
        "READ-ONLY. Returns PHI — do not echo to chat."
    ),
)
async def emr_combined_schedule(start_date: str = None, end_date: str = None) -> dict:
    """
    Args:
        start_date: ISO date YYYY-MM-DD (defaults to today). SIS uses full Mon-Sun week.
        end_date:   Reserved for future multi-day Svigg iteration; currently ignored.
    """
    args_str = f"start_date={start_date!r} end_date={end_date!r}"
    try:
        mgr = await _get_mgr()
        result = await mgr.combined_schedule(start_date, end_date)
        sis_n = result.get("sis_count", 0)
        svigg_n = result.get("svigg_count", 0)
        _mcp_audit("emr_combined_schedule", args_str, "ok", f"sis={sis_n} svigg={svigg_n}")
        return result
    except Exception as exc:
        _mcp_audit("emr_combined_schedule", args_str, "error", str(exc))
        return {
            "start_date": start_date,
            "end_date": end_date,
            "error": str(exc),
            "sis_schedule": [],
            "svigg_calendar": [],
        }


# ---------------------------------------------------------------------------
# Tool: emr_patient_360
# ---------------------------------------------------------------------------

@server.tool(
    name="emr_patient_360",
    description=(
        "Atlantic Pain & Wellness: unified patient view across BOTH EMRs. "
        "Searches SIS Complete + Svigg/Dr.Com in parallel, then fetches "
        "SIS demographics (62 fields) and Svigg visit ledger for the top match. "
        "Returns: sis_demographics, svigg_ledger, match counts, and patient IDs. "
        "READ-ONLY. Returns PHI — do not echo to chat or store outside audit log."
    ),
)
async def emr_patient_360(query: str) -> dict:
    """
    Args:
        query: Patient name (any format) or MRN to search across both EMRs.
    """
    args_str = f"query={query!r}"
    try:
        mgr = await _get_mgr()
        result = await mgr.patient_360(query)
        sis_n = result.get("sis_match_count", 0)
        svigg_n = result.get("svigg_match_count", 0)
        _mcp_audit("emr_patient_360", args_str, "ok", f"sis_matches={sis_n} svigg_matches={svigg_n}")
        return result
    except Exception as exc:
        _mcp_audit("emr_patient_360", args_str, "error", str(exc))
        return {
            "query": query,
            "error": str(exc),
            "sis_match_count": 0,
            "svigg_match_count": 0,
            "sis_demographics": None,
            "svigg_ledger": None,
        }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server.run(transport="stdio")

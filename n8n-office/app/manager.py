"""app/manager.py — The "manager" report: an LLM (or heuristic) review of
how the back-office assistant is running.

``run_manager`` gathers operational statistics straight from the local
SQLite database over a trailing window and produces a structured report:

    {
        "summary": str,
        "working_well": [str, ...],
        "problems": [str, ...],
        "proposals": [str, ...],
    }

Two paths, one shape:

  * **LLM path** — when a chat provider is configured (``get_provider()`` is
    not ``None``), the stats are handed to the model and it writes the four
    fields. Proposals are RECOMMENDATIONS only: the manager never changes
    EMR-write behaviour on its own — every operational change still routes to
    a human on-screen.
  * **Heuristic path** — when no provider is configured, we synthesise the
    same four fields from the raw numbers and tag the report
    ``"LLM unavailable — heuristic report"`` so nobody mistakes it for a
    model's judgement.

Honesty / PHI notes (house rules):

  * The stats are COUNTS and aggregates — never patient names — so nothing
    identifying is assembled. As a belt-and-suspenders guard, when the active
    provider is not PHI-safe (``SETTINGS.PHI_SAFE_LLM`` is false) we still only
    send counts; there is simply no patient text in the prompt to leak.
  * If the model returns something we cannot parse as the required JSON shape,
    we fall back to the heuristic report rather than inventing fields — and we
    say so in the summary.
  * The report is persisted to ``manager_reports`` and returned to the caller.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.config import SETTINGS
from app import db, audit, providers

logger = logging.getLogger(__name__)

# Empty-but-valid report skeleton — every path fills these exact keys.
_REPORT_KEYS = ("summary", "working_well", "problems", "proposals")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ---------------------------------------------------------------------------
# Stats gathering (pure DB reads — no PHI, counts only)
# ---------------------------------------------------------------------------

def _gather_stats(window_hours: int) -> dict:
    """Collect operational metrics over the trailing ``window_hours``.

    Everything here is a count / aggregate. No patient-identifying text is
    read, so the resulting dict is always safe to send to any provider.

    Returns keys:
      window_hours, generated_at,
      approvals_by_status  {status: n}   (all-time, small table),
      approvals_recent     int           (created in window),
      approvals_failed_recent int,
      audit_errors         [ {ts, actor, kind, action, outcome} ]  (window),
      audit_error_count    int,
      referrals_by_status  {status: n},
      referrals_new        int,
      referrals_oldest_new_age_h  float | None,
      chat_turns           int           (conversations in window),
      chat_users           int           (distinct usernames in window).
    """
    cutoff = (_now() - timedelta(hours=window_hours)).isoformat()

    stats: dict = {
        "window_hours": window_hours,
        "generated_at": _now_iso(),
    }

    with db.get_conn() as conn:
        # --- Approvals -----------------------------------------------------
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM approvals GROUP BY status"
        ).fetchall()
        stats["approvals_by_status"] = {r["status"]: r["n"] for r in rows}

        stats["approvals_recent"] = conn.execute(
            "SELECT COUNT(*) AS n FROM approvals WHERE ts_created >= ?",
            (cutoff,),
        ).fetchone()["n"]

        stats["approvals_failed_recent"] = conn.execute(
            "SELECT COUNT(*) AS n FROM approvals "
            "WHERE status = 'failed' AND ts_decided >= ?",
            (cutoff,),
        ).fetchone()["n"]

        # --- Audit error rows (window) ------------------------------------
        err_rows = conn.execute(
            "SELECT ts, actor, kind, action, outcome FROM audit "
            "WHERE outcome != 'ok' AND ts >= ? ORDER BY id DESC LIMIT 50",
            (cutoff,),
        ).fetchall()
        stats["audit_errors"] = [dict(r) for r in err_rows]
        stats["audit_error_count"] = len(err_rows)

        # --- Referrals -----------------------------------------------------
        ref_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM referrals GROUP BY status"
        ).fetchall()
        stats["referrals_by_status"] = {r["status"]: r["n"] for r in ref_rows}
        stats["referrals_new"] = stats["referrals_by_status"].get("new", 0)

        oldest = conn.execute(
            "SELECT MIN(ts) AS oldest FROM referrals WHERE status = 'new'"
        ).fetchone()["oldest"]
        stats["referrals_oldest_new_age_h"] = _age_hours(oldest)

        # --- Chat volume ---------------------------------------------------
        chat = conn.execute(
            "SELECT COUNT(*) AS turns, COUNT(DISTINCT username) AS users "
            "FROM conversations WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
        stats["chat_turns"] = chat["turns"]
        stats["chat_users"] = chat["users"]

    return stats


def _age_hours(ts_iso) -> float | None:
    """Hours between ``ts_iso`` and now, or ``None`` if unparseable/absent."""
    if not ts_iso:
        return None
    try:
        then = datetime.fromisoformat(ts_iso)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return round((_now() - then).total_seconds() / 3600.0, 1)


# ---------------------------------------------------------------------------
# Heuristic report (no LLM)
# ---------------------------------------------------------------------------

def _heuristic_report(stats: dict, note: str) -> dict:
    """Build the four-field report from raw numbers — no model involved.

    ``note`` explains WHY we are on the heuristic path (no provider, or an
    unparseable model reply) and is surfaced in the summary so the report is
    never mistaken for LLM judgement.
    """
    approvals = stats["approvals_by_status"]
    pending = approvals.get("pending", 0)
    failed_recent = stats["approvals_failed_recent"]
    err_count = stats["audit_error_count"]
    new_refs = stats["referrals_new"]
    oldest_h = stats["referrals_oldest_new_age_h"]
    turns = stats["chat_turns"]

    working_well: list[str] = []
    problems: list[str] = []
    proposals: list[str] = []

    if turns:
        working_well.append(
            f"Assistant handled {turns} chat turn(s) from "
            f"{stats['chat_users']} user(s) in the last "
            f"{stats['window_hours']}h."
        )
    if approvals.get("executed"):
        working_well.append(
            f"{approvals['executed']} approval(s) executed successfully "
            "(all-time)."
        )
    if not working_well:
        working_well.append(
            "No activity to highlight in this window yet."
        )

    if pending:
        problems.append(
            f"{pending} approval(s) still pending human decision."
        )
        proposals.append(
            "Review the pending approvals queue on-screen; each write still "
            "needs a human click."
        )
    if failed_recent:
        problems.append(
            f"{failed_recent} approval(s) FAILED to execute in the window "
            "(EMR write did not confirm)."
        )
        proposals.append(
            "Inspect failed approvals' stored EMR responses before re-queuing "
            "— do not assume the write landed."
        )
    if err_count:
        problems.append(
            f"{err_count} audit row(s) recorded a non-ok outcome in the "
            "window."
        )
    if new_refs:
        age_txt = (
            f"; oldest is {oldest_h}h old" if oldest_h is not None else ""
        )
        problems.append(
            f"{new_refs} referral(s) still status 'new' (unactioned)"
            f"{age_txt}."
        )
        proposals.append(
            "Work the new-referral queue: match candidates, then book or "
            "route each — every booking routes through on-screen approval."
        )

    if not problems:
        problems.append("No problems detected from the metrics in this "
                        "window.")
    if not proposals:
        proposals.append(
            "Keep current cadence; no operational change recommended."
        )

    summary = (
        f"Heuristic operations review over the last "
        f"{stats['window_hours']}h: {pending} pending / "
        f"{failed_recent} failed approvals, {new_refs} new referrals, "
        f"{err_count} error events, {turns} chat turns. {note}"
    )

    return {
        "summary": summary,
        "working_well": working_well,
        "problems": problems,
        "proposals": proposals,
    }


# ---------------------------------------------------------------------------
# LLM report
# ---------------------------------------------------------------------------

def _coerce_report(obj) -> dict | None:
    """Validate/normalise a model reply into the four-field report shape.

    Returns a clean report dict, or ``None`` if the object cannot be coerced
    (missing/blank summary, wrong type) — signalling the caller to fall back
    to the heuristic path rather than ship a half-built report.
    """
    if not isinstance(obj, dict):
        return None
    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None

    def _as_list(v) -> list:
        if isinstance(v, list):
            return [str(x) for x in v]
        if v in (None, ""):
            return []
        return [str(v)]  # tolerate a bare string / scalar.

    return {
        "summary": summary.strip(),
        "working_well": _as_list(obj.get("working_well")),
        "problems": _as_list(obj.get("problems")),
        "proposals": _as_list(obj.get("proposals")),
    }


def _extract_json(text: str):
    """Best-effort parse of a JSON object out of an LLM text reply.

    Handles a bare JSON object as well as one wrapped in prose or fenced code
    (```json ... ```). Returns the parsed object or ``None``.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    # Fall back to the first balanced-looking {...} slice.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (TypeError, ValueError):
            return None
    return None


async def _llm_report(stats: dict, provider) -> dict | None:
    """Ask the provider to write the report from ``stats``.

    Returns a coerced four-field report, or ``None`` on any failure (provider
    error, unparseable / malformed reply) so the caller falls back to the
    heuristic report. Only COUNTS are sent — no PHI — so this is safe on every
    provider including non-PHI-safe ones.
    """
    system = (
        "You are the operations manager for the back-office assistant at "
        f"{SETTINGS.BRAND} (Bala Cynwyd office — two brands, one office). "
        "You are given ONLY aggregate operational counts (no patient data). "
        "Write a concise, honest review. Do NOT invent metrics that are not "
        "in the data; if something is unknown, say so. Your 'proposals' are "
        "RECOMMENDATIONS only — you never change EMR-write behaviour, and "
        "every operational change must route to a human on-screen. "
        "Reply with ONLY a JSON object with exactly these keys: "
        "\"summary\" (string), \"working_well\" (array of strings), "
        "\"problems\" (array of strings), \"proposals\" (array of strings)."
    )
    user = (
        "Here are the operational stats (counts only) for the trailing "
        f"{stats['window_hours']}h window. Write the report.\n\n"
        + json.dumps(stats, ensure_ascii=False, indent=2)
    )

    try:
        result = await provider.complete(
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[],
        )
    except Exception as e:
        logger.error("manager: provider.complete failed: %s", e)
        return None

    parsed = _extract_json(result.get("text", "") if result else "")
    report = _coerce_report(parsed)
    if report is None:
        logger.warning("manager: LLM reply not in required shape; "
                       "falling back to heuristic report")
    return report


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_manager(window_hours: int = 24) -> dict:
    """Generate, persist, and return a manager report over ``window_hours``.

    Flow:
      1. Gather COUNTS-ONLY stats from the DB.
      2. If a provider is configured → ask it for the report; on any failure
         fall back to the heuristic report (same shape, honest note).
      3. If no provider → heuristic report tagged 'LLM unavailable'.
      4. Persist to ``manager_reports`` and audit the run.

    Never sends PHI to any provider (stats are aggregates), so the
    ``PHI_SAFE_LLM`` distinction does not gate report generation here — but the
    guarantee holds regardless of provider PHI-safety.
    """
    stats = _gather_stats(window_hours)
    provider = providers.get_provider()

    report: dict | None = None
    if provider is not None:
        report = await _llm_report(stats, provider)
        if report is None:
            report = _heuristic_report(
                stats,
                "LLM reply unusable — heuristic report substituted.",
            )
            used = "heuristic_fallback"
        else:
            used = "llm"
    else:
        report = _heuristic_report(
            stats, "LLM unavailable — heuristic report"
        )
        used = "heuristic"

    # Attach the raw stats so the UI can show what the report was built from.
    report["stats"] = stats
    report["generated_by"] = used

    _persist_report(window_hours, report)
    audit.log(
        actor="system",
        kind="manager",
        action="run",
        detail={
            "window_hours": window_hours,
            "generated_by": used,
            "problems": len(report.get("problems", [])),
        },
    )
    return report


def _persist_report(window_hours: int, report: dict) -> None:
    """Store the report JSON in ``manager_reports``."""
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO manager_reports (ts, window_hours, report) "
            "VALUES (?, ?, ?)",
            (_now_iso(), window_hours,
             json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()


def latest_report() -> dict | None:
    """Return the most recent stored report (JSON-decoded), or ``None``.

    Convenience for the home endpoint; the report dict carries its own
    ``generated_at`` inside ``stats``.
    """
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT report FROM manager_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None or not row["report"]:
        return None
    try:
        return json.loads(row["report"])
    except (TypeError, ValueError):
        return None

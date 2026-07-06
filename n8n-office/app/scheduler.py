"""
app/scheduler.py — In-app background scheduler.

A single asyncio background task, started from ``server`` on startup and
cancelled on shutdown, that runs the three recurring back-office jobs so the
box needs no external cron/launchd fan-out for routine work:

  1. comms poll  — pull new email/SMS/fax/voicemail every
     ``COMMS_POLL_MINUTES`` (when any comms cred is configured).
  2. manager     — the daily manager summary run, once per day at
     ``MANAGER_RUN_HOUR`` (local time).
  3. referral audit — the daily Gmail referral-audit scan, once per day at
     ``AUDIT_RUN_HOUR`` (local time), ONLY when the EMR is enabled and Gmail
     is configured (else skipped silently).

DESIGN / HONESTY NOTES
  - The loop ticks every ``_TICK_SECONDS`` (60s) and NEVER raises: each job is
    wrapped in its own try/except and leaves an ``audit.log(kind='scheduler')``
    row with the true outcome ('ok' / 'error'). One job's failure can never
    kill the loop or another job.
  - To avoid log spam the comms-poll job writes an audit row ONLY when it
    actually ingested new items (>0). A quiet poll is silent.
  - The whole scheduler is DISABLED when ``APP_SCHEDULER=0`` (see
    ``SETTINGS.SCHEDULER_ENABLED``): ``run()`` returns immediately, so the
    startup handle completes at once and nothing ever ticks.
  - Time comes from ``datetime.now()`` (local wall clock) — never a bare
    ``time.time()``/sleep-arithmetic scheme that would be awkward to reason
    about across the daily-hour jobs. The daily jobs fire on the FIRST tick at
    or after their target hour (``now.hour >= RUN_HOUR``) and are de-duplicated
    by local calendar DATE, so they fire at most once per local day regardless
    of how many ticks land in the window — AND a late wake (Mac asleep across
    the exact hour) still runs them once rather than skipping the whole day.
    The date guard is seeded from the audit trail at loop start so a restart
    inside the run hour does not double-fire.
  - Sync network work (``comms.poll_all``) is offloaded with
    ``asyncio.to_thread`` so a slow IMAP/RingCentral round trip never freezes
    the event loop. The manager / referral-audit coroutines are already async
    and run on this loop, so they are awaited directly.
  - No PHI ever reaches an audit row here: only counts, dates, and outcome
    labels are recorded.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.config import SETTINGS
from app import (audit, comms, email_request_triage, manager, referral_audit,
                 request_triage)

logger = logging.getLogger(__name__)

# Loop cadence. One tick per minute is fine: the comms poll is gated on an
# elapsed-minutes budget and the daily jobs are gated on the local hour + a
# once-per-date guard, so sub-minute precision buys nothing.
_TICK_SECONDS = 60


# ---------------------------------------------------------------------------
# Mutable loop state (plain module attrs, per the contract).
# ---------------------------------------------------------------------------

# Monotonic-ish wall-clock timestamp (datetime) of the last comms poll, or
# None if we have not polled yet.
last_poll_ts: datetime | None = None
# ISO date string (YYYY-MM-DD) of the last day the manager / audit jobs ran,
# so each fires at most once per local calendar day.
last_manager_date: str | None = None
last_audit_date: str | None = None
# When the loop actually started (None until run() begins its loop).
started_at: datetime | None = None


def _reset_state() -> None:
    """Reset all loop state to its pre-start values.

    Kept tiny and explicit so tests (and a restart within one process) get a
    clean slate without reaching into module globals by hand.
    """
    global last_poll_ts, last_manager_date, last_audit_date, started_at
    last_poll_ts = None
    last_manager_date = None
    last_audit_date = None
    started_at = None


# ---------------------------------------------------------------------------
# Cred / gating predicates (mirror the comms + referral_audit conventions).
# ---------------------------------------------------------------------------

def _comms_configured() -> bool:
    """True when ANY comms credential is present (email or RingCentral).

    Email is considered configured when the multi-account list has at least
    one entry (``SETTINGS.GMAIL_ACCOUNTS``) OR the back-compat single-sender
    pair is set. RingCentral needs its JWT client trio. Matches the 'not
    configured' degradation contract in ``app.comms`` — if nothing is wired we
    simply never schedule a poll.
    """
    accounts = getattr(SETTINGS, "GMAIL_ACCOUNTS", None) or []
    gmail = bool(accounts) or bool(
        getattr(SETTINGS, "GMAIL_ADDRESS", "")
        and getattr(SETTINGS, "GMAIL_APP_PASSWORD", "")
    )
    rc = bool(
        getattr(SETTINGS, "RC_CLIENT_ID", "")
        and getattr(SETTINGS, "RC_CLIENT_SECRET", "")
        and getattr(SETTINGS, "RC_JWT", "")
    )
    return gmail or rc


def _gmail_configured() -> bool:
    """True when at least one Gmail mailbox is configured (multi or legacy)."""
    accounts = getattr(SETTINGS, "GMAIL_ACCOUNTS", None) or []
    if accounts:
        return True
    return bool(
        getattr(SETTINGS, "GMAIL_ADDRESS", "")
        and getattr(SETTINGS, "GMAIL_APP_PASSWORD", "")
    )


def _last_run_date_from_audit(action: str) -> str | None:
    """Return the local date (YYYY-MM-DD) a daily job last RAN, from the audit
    trail — so the once-per-day guard survives a process restart.

    The in-memory ``last_manager_date`` / ``last_audit_date`` reset to None on
    every restart. Without this, a crash-restart INSIDE the run hour would see
    ``last_*_date is None`` and double-fire the daily job (a duplicate manager
    report / duplicate live-EMR referral-audit scan). We seed the guard at loop
    start from the most recent scheduler audit row for *action* whose outcome
    is NOT 'error' (an errored run did not claim the day and should retry).

    Audit ``ts`` is a UTC ISO timestamp but the ``detail.date`` we write is the
    LOCAL calendar date the job ran for; we read that back so the comparison
    stays in the same local-date space as ``now.date().isoformat()``. Returns
    None on any miss / parse failure — the caller then behaves as before (may
    run once today), which is the safe direction (run, don't skip).
    """
    try:
        import json
        # Filter by ACTION in SQL — not a shared kind='scheduler' recency window.
        # comms_poll/request_triage/email_request_triage also log kind='scheduler'
        # (several rows per poll), so a 200-row window could push a low-volume
        # daily manager_run/referral_audit row out of view → the once-per-day
        # guard fails to seed → the daily job double-fires after a restart.
        for row in audit.recent(limit=25, kind="scheduler", action=action):
            if str(row.get("outcome") or "").lower() == "error":
                # An errored run released its day-claim; don't let it suppress
                # today's run.
                continue
            raw = row.get("detail")
            if not raw:
                continue
            try:
                detail = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            date = detail.get("date") if isinstance(detail, dict) else None
            if date:
                return str(date)
    except Exception as exc:  # never let seeding break startup
        logger.warning("scheduler: could not seed %s last-run date: %s",
                       action, exc)
    return None


def _seed_daily_guards_from_audit() -> None:
    """Seed the in-memory daily-run guards from the audit trail at loop start.

    Makes the once-per-day dedupe durable across restarts: if today's manager /
    referral-audit already ran (an 'ok' audit row for today exists), a restart
    won't re-fire it.
    """
    global last_manager_date, last_audit_date
    today = datetime.now().date().isoformat()
    m = _last_run_date_from_audit("manager_run")
    if m == today:
        last_manager_date = today
    a = _last_run_date_from_audit("referral_audit")
    if a == today:
        last_audit_date = today


def _new_items(result: dict) -> int:
    """Sum the integer per-channel counts in a ``comms.poll_all`` result.

    ``poll_all`` values are ``int`` (new rows) or a string
    ('not configured' / 'error: …'); only the ints count as new work. Nested
    detail dicts (e.g. ``email_accounts``) are ignored — the top-level 'email'
    int already totals them.
    """
    total = 0
    for key, value in result.items():
        if key == "email_accounts":
            continue
        if isinstance(value, bool):  # guard: bool is an int subclass
            continue
        if isinstance(value, int):
            total += value
    return total


# ---------------------------------------------------------------------------
# Individual jobs — each is self-contained and NEVER raises.
# ---------------------------------------------------------------------------

async def _run_comms_poll(now: datetime) -> None:
    """Poll comms if the elapsed-minutes budget is due and creds exist.

    Records an audit row ONLY when new items were ingested (>0), to avoid
    spamming the audit trail with a row every few minutes on a quiet inbox.
    """
    minutes = int(getattr(SETTINGS, "COMMS_POLL_MINUTES", 5) or 0)
    if minutes <= 0:
        return
    if not _comms_configured():
        return

    global last_poll_ts
    if last_poll_ts is not None:
        elapsed_min = (now - last_poll_ts).total_seconds() / 60.0
        if elapsed_min < minutes:
            return

    # Mark the attempt time BEFORE the (possibly slow) poll so a long IMAP
    # round trip can't cause back-to-back polls on the next tick.
    last_poll_ts = now
    try:
        result = await asyncio.to_thread(comms.poll_all)
        new_items = _new_items(result if isinstance(result, dict) else {})
        if new_items > 0:
            audit.log("scheduler", "scheduler", "comms_poll",
                      {"new_items": new_items}, outcome="ok")
    except Exception as exc:  # never let a poll failure kill the loop
        logger.error("scheduler: comms poll failed: %s", exc)
        audit.log("scheduler", "scheduler", "comms_poll",
                  {"error": type(exc).__name__}, outcome="error")

    # After pulling new messages, lift any inbound SMS/fax/voicemail that read
    # like a patient request (schedule/reschedule/cancel/records/...) into the
    # approval queue. Deterministic + deduped; own try/except so a triage error
    # never affects the poll's own outcome or kills the loop.
    try:
        triage = await asyncio.to_thread(request_triage.scan_inbound_requests,
                                         "scheduler")
        if triage.get("cards_created"):
            audit.log("scheduler", "scheduler", "request_triage",
                      {"cards": triage["cards_created"],
                       "by_type": triage.get("by_type", {})}, outcome="ok")
    except Exception as exc:
        logger.error("scheduler: request triage failed: %s", exc)
        audit.log("scheduler", "scheduler", "request_triage",
                  {"error": type(exc).__name__}, outcome="error")

    # Same lift, EMAIL edition: pull inbound email that reads like a records /
    # scheduling / billing ask into the approval queue. Deterministic + deduped,
    # precision-first (self-mail / newsletters / marketing excluded). STAGES a
    # human-approval card ONLY — never sends records or touches the EMR. Own
    # try/except so an email-triage error never affects the poll or the loop.
    try:
        etriage = await asyncio.to_thread(
            email_request_triage.scan_inbound_email_requests, "scheduler")
        if etriage.get("cards_created"):
            audit.log("scheduler", "scheduler", "email_request_triage",
                      {"cards": etriage["cards_created"],
                       "by_intent": etriage.get("by_intent", {})},
                      outcome="ok")
    except Exception as exc:
        logger.error("scheduler: email request triage failed: %s", exc)
        audit.log("scheduler", "scheduler", "email_request_triage",
                  {"error": type(exc).__name__}, outcome="error")


async def _run_manager(now: datetime) -> None:
    """Run the daily manager summary once per local day at/after MANAGER_RUN_HOUR.

    Gate is ``now.hour >= hour`` (NOT exact-equality) + a once-per-local-date
    guard. Exact-equality would silently SKIP the whole day if the box was
    asleep / down / stuck in a slow tick across the single target hour (asyncio
    doesn't tick while the Mac sleeps) — with the default 02:00 hour on an
    office Mac that sleeps overnight, the job could effectively never run. With
    ``>=`` a late wake still fires it once; the date guard (seeded from the
    audit trail at loop start, so it survives a restart) keeps it to once/day.
    """
    hour = int(getattr(SETTINGS, "MANAGER_RUN_HOUR", 2))
    today = now.date().isoformat()
    global last_manager_date
    if now.hour < hour or last_manager_date == today:
        return

    # Claim the day BEFORE running so a slow run can't double-fire within the
    # target hour if the first run outlives a tick.
    last_manager_date = today
    try:
        await manager.run_manager()
        audit.log("scheduler", "scheduler", "manager_run",
                  {"date": today}, outcome="ok")
    except Exception as exc:
        logger.error("scheduler: manager run failed: %s", exc)
        # Release the day's claim (mirror _run_referral_audit) so a later tick
        # retries — otherwise an in-process failure silently suppresses the
        # manager report for the whole local day.
        last_manager_date = None
        audit.log("scheduler", "scheduler", "manager_run",
                  {"date": today, "error": type(exc).__name__},
                  outcome="error")


async def _run_referral_audit(now: datetime) -> None:
    """Run the daily Gmail referral audit once per local day at AUDIT_RUN_HOUR.

    Skipped SILENTLY (no audit row) when the EMR is disabled or Gmail is not
    configured — the contract does not want a daily 'skipped' row cluttering
    the trail.
    """
    hour = int(getattr(SETTINGS, "AUDIT_RUN_HOUR", 7))
    today = now.date().isoformat()
    global last_audit_date
    # ``>=`` (not exact-equality) so a late wake past AUDIT_RUN_HOUR still runs
    # the day's audit once; the date guard (seeded from the audit trail across
    # restarts) keeps it to once/day. See _run_manager for the full rationale.
    if now.hour < hour or last_audit_date == today:
        return
    if not (SETTINGS.EMR_ENABLED and _gmail_configured()):
        return

    # Claim the day BEFORE the (slow, live-EMR) scan so it fires at most once.
    last_audit_date = today
    try:
        report = await referral_audit.scan_gmail_and_audit(created_by="scheduler")
        # scan_gmail_and_audit NEVER raises — a total failure comes back as
        # {'status': 'error', ...} (or 'gmail_not_configured'). Discarding the
        # report and logging 'ok' unconditionally would claim success for a scan
        # that audited zero referrals (house rule: never claim success for work
        # that didn't happen). Inspect the status and record the truth; on a
        # real failure RELEASE the day-claim so the next eligible tick retries
        # instead of leaving a green trail while referrals go unaudited.
        status = report.get("status") if isinstance(report, dict) else None
        if status == "error":
            last_audit_date = None
            logger.error("scheduler: referral audit returned error: %s",
                         report.get("error"))
            audit.log("scheduler", "scheduler", "referral_audit",
                      {"date": today, "error": "scan_error"},
                      outcome="error")
        else:
            audit.log("scheduler", "scheduler", "referral_audit",
                      {"date": today}, outcome="ok")
    except Exception as exc:
        # Defensive: scan_gmail_and_audit is wrapped, but if it ever DID raise
        # (or an audit.log below throws), release the claim so we retry.
        last_audit_date = None
        logger.error("scheduler: referral audit failed: %s", exc)
        audit.log("scheduler", "scheduler", "referral_audit",
                  {"date": today, "error": type(exc).__name__},
                  outcome="error")


# ---------------------------------------------------------------------------
# The loop.
# ---------------------------------------------------------------------------

async def _tick() -> None:
    """Run every due job once. Each job swallows its own errors."""
    now = datetime.now()
    await _run_comms_poll(now)
    await _run_manager(now)
    await _run_referral_audit(now)


async def run() -> None:
    """Scheduler entrypoint: loop forever, ticking every ``_TICK_SECONDS``.

    Returns IMMEDIATELY when the scheduler is disabled
    (``SETTINGS.SCHEDULER_ENABLED`` false / ``APP_SCHEDULER=0``) so the startup
    task handle completes at once and nothing ticks.

    The loop itself never raises: ``_tick`` handles per-job failures, and any
    truly unexpected error at the loop level is logged and the loop continues.
    Cancellation (on server shutdown) propagates cleanly via ``CancelledError``.
    """
    if not getattr(SETTINGS, "SCHEDULER_ENABLED", True):
        logger.info("scheduler: disabled (APP_SCHEDULER=0); not starting")
        return

    global started_at
    started_at = datetime.now()
    # Make the once-per-day guards durable across restarts: if today's daily
    # jobs already ran, don't re-fire them after a crash-restart inside the run
    # hour. A miss (audit unreadable) simply falls back to the in-memory guard.
    _seed_daily_guards_from_audit()
    logger.info(
        "scheduler: started · comms_poll=%smin · manager_hour=%s · audit_hour=%s",
        getattr(SETTINGS, "COMMS_POLL_MINUTES", 5),
        getattr(SETTINGS, "MANAGER_RUN_HOUR", 2),
        getattr(SETTINGS, "AUDIT_RUN_HOUR", 7),
    )

    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            logger.info("scheduler: cancelled; stopping")
            raise
        except Exception as exc:  # pragma: no cover - defensive belt-and-braces
            logger.error("scheduler: unexpected loop error: %s", exc)
        try:
            await asyncio.sleep(_TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("scheduler: cancelled during sleep; stopping")
            raise


def get_status() -> dict:
    """Return a JSON-safe snapshot of scheduler state for /api/health.

    Timestamps/dates are rendered as strings (or ``None``) so the dict drops
    straight into a JSON response.
    """
    return {
        "enabled": bool(getattr(SETTINGS, "SCHEDULER_ENABLED", True)),
        "comms_poll_minutes": int(getattr(SETTINGS, "COMMS_POLL_MINUTES", 5) or 0),
        "last_poll_ts": last_poll_ts.isoformat() if last_poll_ts else None,
        "last_manager_date": last_manager_date,
        "last_audit_date": last_audit_date,
    }

"""app/referral_audit.py — Repeatable referral-vs-EMR audit.

A referral audit answers ONE question for each inbound "please see this
patient" lead: *is this patient already on the schedule, or is someone still
supposed to call and book them?* Referrals that fall through the cracks — the
lead came in, nobody booked — are the exact failure this catches.

For every referral we:

  1. Search BOTH EMRs (``combined_search``) to see if the patient exists at
     all (SIS Complete + Svigg/WEBeDoctor).
  2. If found, check whether their last name shows up on the upcoming Svigg
     appointment calendar (today .. today+N days).
  3. Classify:
       * ``not_in_emr``    — no EMR match → needs intake AND booking;
       * ``in_emr_no_appt`` — matched, but not on the upcoming calendar → needs
         booking;
       * ``scheduled``     — matched AND on the calendar → no action.

For the two action lanes we raise ONE ``manual_task`` approval card ("call +
book this referral") and record a ``referrals`` row, so the miss is visible and
actionable on-screen. A de-dupe guard means re-running the audit does NOT pile
up duplicate cards for the same patient.

Honesty / safety (house rules):
  * Every EMR call goes through the process singleton
    ``EMRSessionManager.get_instance()``, imported INSIDE functions so importing
    this module never drags in the heavy scraper stack.
  * Guarded by ``SETTINGS.EMR_ENABLED``: when the EMR is off we report an honest
    'EMR disabled' status per referral and create NO cards — we never guess a
    schedule state we could not verify.
  * A found-patient match is only ever asserted from a REAL match row the
    scraper returned (SIS + Svigg arms of ``combined_search``); a lookup error
    becomes ``found=False`` with a note, never a fabricated match.
  * PHI: patient names live only in the local approvals/referrals tables and the
    returned report; the server log gets referral counts, never names.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from app import approvals, db
from app.config import SETTINGS

logger = logging.getLogger(__name__)

# Placeholder the referrals lane uses for a missing field (house rule: never
# fabricate). Mirrors ``app.referrals._MISSING`` so a blank name is comparable.
_MISSING = "—"


def _today_iso() -> str:
    """Local calendar date YYYY-MM-DD (datetime.now, never Date.now)."""
    return datetime.now().strftime("%Y-%m-%d")


def _last_name_token(patient_name: str) -> str:
    """Return the lowercased last-name token for a referral / calendar name.

    Handles both "Last, First" (comma form the Svigg grid uses) and
    "First Last" (free-text referral form). Returns "" when nothing usable can
    be pulled out, so the caller can skip a nameless match rather than guess.
    """
    if not patient_name:
        return ""
    name = str(patient_name).strip()
    if not name or name == _MISSING:
        return ""
    if "," in name:
        # "Last, FirstInit" → the piece before the comma is the last name.
        last = name.split(",", 1)[0]
    else:
        # "First Last" → the final whitespace token is the last name.
        parts = name.split()
        last = parts[-1] if parts else ""
    return last.strip().lower()


# ---------------------------------------------------------------------------
# Upcoming-appointment index (Svigg calendar scrape)
# ---------------------------------------------------------------------------

async def _upcoming_appt_index(mgr, days: int) -> dict:
    """Scrape the Svigg calendar for today .. today+days into a last-name set.

    Calls ``mgr.svigg_appointment_calendar(date_iso)`` once per day and
    collects the lowercased last-name token from every ``patient_name`` cell
    (cell text is "LastName, FirstInit"). Never raises: a day that errors is
    skipped and counted, so one bad scrape can't sink the whole audit.

    Returns ``{'last_names': set(...), 'days_scanned': n, 'errors': k}``.
    """
    from datetime import timedelta

    last_names: set[str] = set()
    days_scanned = 0
    errors = 0

    base = datetime.now()
    span = max(int(days), 0) + 1  # inclusive of today .. today+days
    for offset in range(span):
        date_iso = (base + timedelta(days=offset)).strftime("%Y-%m-%d")
        days_scanned += 1
        try:
            cells = await mgr.svigg_appointment_calendar(date_iso)
        except Exception as exc:  # never raise — skip + count the bad day
            logger.warning("referral audit: calendar scrape failed for %s: %s",
                           date_iso, exc)
            errors += 1
            continue
        if not isinstance(cells, list):
            errors += 1
            continue
        # The scraper returns [{"error": ...}] for a failed day rather than
        # raising — treat that as an errored (not empty) day, don't count its
        # error dict as a patient.
        if len(cells) == 1 and isinstance(cells[0], dict) and "error" in cells[0]:
            errors += 1
            continue
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            token = _last_name_token(cell.get("patient_name", ""))
            if token:
                last_names.add(token)

    return {
        "last_names": last_names,
        "days_scanned": days_scanned,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# combined_search result inspection
# ---------------------------------------------------------------------------

def _match_count(result: dict) -> int:
    """Count real match rows across BOTH arms of a ``combined_search`` result.

    ``combined_search`` returns
    ``{"sis_results": [...], "svigg_results": [...], ...}`` (see
    emr_session_manager.py). Each arm is a list of match rows on success, or []
    on an isolated arm failure. We count list entries in both arms; anything
    non-list (belt-and-suspenders) contributes zero.
    """
    if not isinstance(result, dict):
        return 0
    total = 0
    for key in ("sis_results", "svigg_results"):
        arm = result.get(key)
        if isinstance(arm, list):
            total += len(arm)
    return total


# ---------------------------------------------------------------------------
# De-dupe guards (so re-running the audit doesn't pile up duplicate cards)
# ---------------------------------------------------------------------------

def _has_open_manual_task(patient_name: str) -> bool:
    """True if a PENDING manual_task approval already targets this patient.

    Re-running the audit must not stack duplicate "call + book" cards for a
    patient a human hasn't acted on yet. We match on the card's
    ``params.patient_name`` (set below when we enqueue).
    """
    for card in approvals.list_approvals(status="pending", limit=500):
        if card.get("action_type") != "manual_task":
            continue
        params = card.get("params") or {}
        if isinstance(params, dict) and \
                params.get("patient_name") == patient_name:
            return True
    return False


def _has_open_referral_row(patient_name: str) -> bool:
    """True if a referral row for this patient already exists in ANY status.

    The app's real referral vocabulary is {new, working, booked, declined}
    (server.py ``_REFERRAL_STATUSES``); there is no 'contacted' status. The old
    guard matched ('new', 'contacted'), so the moment staff advanced a row to
    'working'/'booked' — or explicitly rejected it to 'declined' — it became
    invisible and the daily scheduled audit re-raised a fresh duplicate card +
    'new' row for the SAME patient every single day. That is exactly the churn
    this guard exists to prevent.

    The correct rule is status-agnostic: if a referral for this patient is
    already tracked in the lane at all, a human has (or will have) seen it —
    whether it is still open ('new'/'working'/'booked') or was deliberately
    rejected ('declined'). Either way the audit must NOT spawn another card, so
    we match on patient_name alone.
    """
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM referrals WHERE patient_name = ? LIMIT 1",
            (patient_name,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _insert_audit_referral(patient_name: str, dob: str, phone: str,
                           referrer: str, reason: str, source: str,
                           emr_status: str) -> int:
    """Insert one 'new' referral row noting the audit that raised it.

    Reuses ``app.referrals._insert_referral`` (the single referral-insert
    helper) so the row shape / status default stay consistent with the rest of
    the referral lane. ``detail`` records that this came from the audit and the
    EMR status that justified the card.
    """
    from app import referrals as referrals_mod

    detail = {
        "audit": {
            "ts": _today_iso(),
            "emr_status": emr_status,
            "note": "raised by referral audit — not yet on the schedule",
        }
    }
    return referrals_mod._insert_referral(
        source=source or "audit",
        patient_name=patient_name or _MISSING,
        dob=dob or _MISSING,
        phone=phone or _MISSING,
        referrer=referrer or _MISSING,
        reason=reason or _MISSING,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Core audit
# ---------------------------------------------------------------------------

async def audit_referrals(referrals: list[dict], created_by: str,
                          appt_window_days: int = 21) -> dict:
    """Check each referral against the EMR; raise a card for any not scheduled.

    Args:
        referrals: each ``{patient_name, dob?, phone?, referrer?, reason?,
                   source?}``.
        created_by: username recorded as ``requested_by`` on any card.
        appt_window_days: how far ahead to scan the Svigg calendar for a match.

    Flow per referral:
      * ``combined_search(patient_name)`` (exceptions → found=False + note);
        ``found`` = there is at least one real match row (SIS + Svigg counted).
      * The upcoming-appointment index is built ONCE, and only if at least one
        referral was found in the EMR (a not-found patient cannot have an appt).
      * Classify: ``not_in_emr`` / ``in_emr_no_appt`` / ``scheduled``.
      * For the two action lanes, enqueue ONE ``manual_task`` card AND insert a
        referral row — unless a de-dupe guard already has an open card / row for
        this patient.

    Returns ``{'window_days', 'checked', 'scheduled', 'cards_created',
    'skipped_duplicate', 'details': [...]}``.
    """
    window_days = max(int(appt_window_days), 0)
    report = {
        "window_days": window_days,
        "checked": 0,
        "scheduled": 0,
        "cards_created": 0,
        "skipped_duplicate": 0,
        "details": [],
    }

    if not referrals:
        return report

    # HONESTY: with the EMR off we cannot verify a schedule state, so we do NOT
    # guess and we create NO cards — one honest 'EMR disabled' row per referral.
    if not SETTINGS.EMR_ENABLED:
        for ref in referrals:
            name = (ref.get("patient_name") or "").strip() or _MISSING
            report["checked"] += 1
            report["details"].append({
                "patient_name": name,
                "status": "emr_disabled",
                "found": False,
                "has_upcoming": False,
            })
        return report

    # Import the manager lazily — only reached for a real (EMR-on) audit.
    from emr_session_manager import EMRSessionManager

    mgr = EMRSessionManager.get_instance()

    # PASS 1: search each referral; remember which were found so we only build
    # the (expensive) calendar index when at least one patient actually exists.
    searched: list[dict] = []
    any_found = False
    for ref in referrals:
        name = (ref.get("patient_name") or "").strip() or _MISSING
        report["checked"] += 1
        if name == _MISSING:
            searched.append({"ref": ref, "name": name, "found": False,
                             "note": "no patient name to search",
                             "failed": False})
            continue
        try:
            result = await mgr.combined_search(name)
            found = _match_count(result) > 0
            note = None
            failed = False
        except Exception as exc:  # honest failure — never a fabricated match
            logger.warning("referral audit: combined_search failed: %s", exc)
            found = False
            note = f"EMR search failed: {exc}"
            failed = True  # search ERRORED — we did NOT establish absence
        if found:
            any_found = True
        searched.append({"ref": ref, "name": name, "found": found,
                         "note": note, "failed": failed})

    # Build the upcoming-appt index ONCE, only if some patient was found.
    upcoming: set[str] = set()
    if any_found:
        idx = await _upcoming_appt_index(mgr, window_days)
        upcoming = idx["last_names"]
        report["appt_days_scanned"] = idx["days_scanned"]
        report["appt_scan_errors"] = idx["errors"]

    # PASS 2: classify + raise cards.
    for item in searched:
        ref = item["ref"]
        name = item["name"]
        found = item["found"]
        note = item["note"]
        search_failed = item.get("failed", False)

        has_upcoming = False
        if found:
            token = _last_name_token(name)
            has_upcoming = bool(token) and token in upcoming

        if search_failed:
            # We could NOT check the EMR (session expired / network hiccup).
            # NEVER assert "no match — needs intake" off a failed search — that
            # fabricates absence and drives duplicate charts. Card it honestly
            # as uncheckable so staff verify manually instead.
            status = "emr_uncheckable"
        elif not found:
            status = "not_in_emr"
        elif has_upcoming:
            status = "scheduled"
        else:
            status = "in_emr_no_appt"

        detail_row = {
            "patient_name": name,
            "status": status,
            "found": found,
            "has_upcoming": has_upcoming,
        }
        if note:
            detail_row["note"] = note

        if status == "scheduled":
            report["scheduled"] += 1
            report["details"].append(detail_row)
            continue

        # Action lanes (not_in_emr / in_emr_no_appt): raise ONE card + row,
        # unless a de-dupe guard already has an open card / active row.
        if _has_open_manual_task(name) or _has_open_referral_row(name):
            report["skipped_duplicate"] += 1
            detail_row["skipped_duplicate"] = True
            report["details"].append(detail_row)
            continue

        dob = (ref.get("dob") or "").strip()
        phone = (ref.get("phone") or "").strip()
        referrer = (ref.get("referrer") or "").strip()
        reason = (ref.get("reason") or "").strip()
        source = (ref.get("source") or "").strip()

        if status == "emr_uncheckable":
            emr_line = ("EMR status: COULD NOT be checked (search error) — "
                        "verify the patient in SIS/Svigg manually BEFORE "
                        "creating an intake; do not assume they are new")
        elif status == "not_in_emr":
            emr_line = "EMR status: no match in SIS or Svigg — needs intake"
        else:
            emr_line = ("EMR status: patient exists but is NOT on the upcoming "
                        f"{window_days}-day calendar — needs booking")

        card_detail = "\n".join([
            f"Patient: {name}",
            f"DOB: {dob or _MISSING}",
            f"Phone: {phone or _MISSING}",
            f"Referrer: {referrer or _MISSING}",
            f"Reason: {reason or _MISSING}",
            f"Source: {source or _MISSING}",
            emr_line,
        ])

        card_title = (f"Verify referral in EMR: {name}"
                      if status == "emr_uncheckable"
                      else f"Call + book referral: {name}")
        card = approvals.enqueue(
            action_type="manual_task",
            params={
                "title": card_title,
                "detail": card_detail,
                "patient_name": name,
                "referrer": referrer or _MISSING,
                "emr_status": status,
            },
            reason=(f"Referral audit {_today_iso()}: {name} — {status}, "
                    "not yet on the schedule"),
            requested_by=created_by,
        )
        # Record the miss in the referral lane too so it's visible there.
        try:
            _insert_audit_referral(name, dob, phone, referrer, reason, source,
                                   status)
        except Exception as exc:  # a failed row must not lose the card
            logger.warning("referral audit: could not insert referral row for "
                           "audit card #%s: %s", card.get("id"), exc)

        report["cards_created"] += 1
        detail_row["card_id"] = card.get("id")
        report["details"].append(detail_row)

    return report


# ---------------------------------------------------------------------------
# Gmail-driven referral extraction + audit
# ---------------------------------------------------------------------------

# Subject-line noise we strip to recover a bare patient name. Longest / most
# specific tokens first so a subject like "New Patient Referral: Doe, Jane"
# reduces cleanly to "Doe, Jane".
_SUBJECT_NOISE = [
    "new patient referral:",
    "new patient referral",
    "medical referral",
    "#medicalreferral",
    "new patient",
    "referral:",
    "referral",
    "fwd:",
    "re:",
]

_DOB_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")
_PHONE_RE = re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)

# Positive referral signal in a subject/body. A non-empty subject alone is NOT
# a referral — routine mail (invoices, EOBs, newsletters, vendor spam) all have
# subjects. Requiring a real referral marker keeps the unattended daily audit
# from firing live-EMR searches (and raising approval cards) for every inbound
# email. Matched case-insensitively against subject+body as one blob.
_REFERRAL_SIGNAL_RE = re.compile(
    r"referr|refer to|refer for|new\s+patient|#medicalreferral|"
    r"please\s+see|please\s+evaluate|please\s+schedule|"
    r"consult(?:ation)?\s+request|patient\s+referral",
    re.IGNORECASE,
)


def _looks_like_referral(subject: str, body: str) -> bool:
    """True when an email carries a real referral signal (not routine mail).

    Guards ``parse_referral_emails`` so the unattended daily audit does not
    treat EVERY inbound email as a referral candidate — otherwise an 'Your
    invoice is ready' subject would fire a live SIS+Svigg search and raise a
    'Call + book referral: Your invoice is ready' card. We accept a row as a
    referral candidate only when a referral marker appears in the subject or
    body. This is deliberately a POSITIVE gate: a false-negative (a genuine
    referral phrased with none of these words) merely goes un-audited by the
    automated scan — safe — whereas the false-positive it prevents is junk PHI
    cards flooding the human approval queue and bulk junk queries against the
    production EMRs.
    """
    blob = f"{subject or ''}\n{body or ''}"
    return bool(_REFERRAL_SIGNAL_RE.search(blob))


def _name_from_subject(subject: str) -> str:
    """Best-effort patient name from an email subject.

    Strips common referral noise tokens ('New Patient', 'MEDICAL REFERRAL',
    'Referral', hashtags, reply/forward prefixes) and returns what remains,
    trimmed. Returns "" when nothing usable is left (the caller then skips the
    row — we never invent a name).
    """
    if not subject:
        return ""
    cleaned = str(subject)
    low = cleaned.lower()
    for token in _SUBJECT_NOISE:
        idx = low.find(token)
        while idx != -1:
            cleaned = cleaned[:idx] + cleaned[idx + len(token):]
            low = cleaned.lower()
            idx = low.find(token)
    # Collapse leftover separators / whitespace.
    cleaned = cleaned.strip(" -:#\t")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _referrer_from_sender(sender: str) -> str:
    """Derive a referrer label from an email 'From' value.

    Prefers a display name ("Dr. Jones <j@clinic.com>" → "Dr. Jones"); falls
    back to the sender's email domain ("j@northside.com" → "northside.com").
    Returns "" when neither can be pulled out.
    """
    if not sender:
        return ""
    s = str(sender).strip()
    # "Display Name <addr@dom>" → prefer the display name.
    if "<" in s:
        display = s.split("<", 1)[0].strip().strip('"').strip()
        if display:
            return display
        s = s.split("<", 1)[1].split(">", 1)[0].strip()
    if "@" in s:
        domain = s.split("@", 1)[1].strip().strip(">").strip()
        return domain
    return s


def _first_sentence(body: str) -> str:
    """First sentence of a body (referral reason), capped for a card line.

    Splits on the first sentence terminator or newline; trims to a sane length
    so a huge email body never bloats a referral row.
    """
    if not body:
        return ""
    text = re.sub(r"\s+", " ", str(body)).strip()
    if not text:
        return ""
    m = re.search(r"[.!?\n]", text)
    sentence = text[:m.start()] if m else text
    sentence = sentence.strip()
    return sentence[:240]


def parse_referral_emails(rows: list[dict]) -> list[dict]:
    """Extract best-effort referral candidates from inbound email message rows.

    Heuristic, never authoritative: for each 'email' row we recover a patient
    name from the subject, a DOB / phone from the body via regex, a referrer
    from the sender, and a reason from the first sentence of the body.

    TWO gates keep routine mail out of the audit:
      1. The email must carry a real referral SIGNAL (``_looks_like_referral``)
         — a non-empty subject alone is NOT a referral. Without this, every
         inbound invoice/EOB/newsletter would be searched against the live EMRs
         and raise a junk 'Call + book referral: <subject>' approval card.
      2. A usable patient name must survive noise-stripping (we never invent a
         name), so the caller only audits candidates that actually name a
         patient.
    """
    candidates: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        subject = row.get("subject") or ""
        body = row.get("body") or ""
        if not _looks_like_referral(subject, body):
            continue  # routine mail — not a referral, skip
        name = _name_from_subject(subject)
        if not name:
            continue  # nothing usable — skip (no fabricated names)

        dob_m = _DOB_RE.search(body)
        phone_m = _PHONE_RE.search(body)
        candidates.append({
            "patient_name": name,
            "dob": dob_m.group(1) if dob_m else "",
            "phone": phone_m.group(0) if phone_m else "",
            "referrer": _referrer_from_sender(row.get("sender") or ""),
            "reason": _first_sentence(body),
            "source": "gmail",
        })
    return candidates


async def scan_gmail_and_audit(created_by: str, since_days: int = 21,
                               appt_window_days: int = 21) -> dict:
    """Ingest recent Gmail, parse referral candidates, and audit them.

    Returns ``{'status': 'gmail_not_configured'}`` when Gmail creds are absent.
    Otherwise polls Gmail (best-effort), reads the last ``since_days`` of
    inbound email rows, parses referral candidates, and runs ``audit_referrals``
    on them — returning that report with a ``gmail`` sub-dict noting the poll
    result and candidate count. Wrapped so it never raises: an unexpected error
    comes back as ``{'status': 'error', 'error': ...}``.
    """
    # Gate on the SAME source of truth the scheduler uses (multi-account list OR
    # the legacy scalar pair). Gating only on the scalars here would let the
    # scheduler fire the audit (it checks GMAIL_ACCOUNTS) while this returned
    # 'gmail_not_configured' — a silent no-op audit that logs a clean run.
    _accounts = getattr(SETTINGS, "GMAIL_ACCOUNTS", None) or []
    _legacy = (getattr(SETTINGS, "GMAIL_ADDRESS", "")
               and getattr(SETTINGS, "GMAIL_APP_PASSWORD", ""))
    if not (_accounts or _legacy):
        return {"status": "gmail_not_configured"}

    try:
        import asyncio

        from app import comms

        # Ingest recent mail (best-effort: a poll error is data, not a raise).
        # OFFLOAD the blocking IMAP work to a thread — poll_gmail is synchronous
        # and iterates up to three mailboxes serially; called directly here it
        # would freeze the app's single event loop (every concurrent HTTP
        # request) for the whole fetch, potentially 30s+ on a slow/wedged
        # connection, every morning at AUDIT_RUN_HOUR. The comms-poll scheduler
        # job offloads for exactly this reason; match it.
        try:
            polled = await asyncio.to_thread(comms.poll_gmail, since_days)
        except Exception as exc:
            logger.warning("referral audit: gmail poll failed: %s", exc)
            polled = f"error: {exc}"

        # Read the inbound email rows we now have and parse candidates.
        rows = comms.list_messages(channel="email", limit=500)
        inbound = [r for r in rows if isinstance(r, dict)
                   and r.get("direction") == "in"]
        candidates = parse_referral_emails(inbound)

        report = await audit_referrals(
            candidates, created_by, appt_window_days=appt_window_days
        )
        report["gmail"] = {
            "polled": polled,
            "candidates": len(candidates),
        }
        return report
    except Exception as exc:  # never raise out of the scan
        logger.error("referral audit: scan_gmail_and_audit failed: %s", exc)
        return {"status": "error", "error": str(exc)}

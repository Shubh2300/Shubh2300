"""app/verify_need.py — on-demand "does this pending card still need attention?"

Cross-references ONE approval card against LIVE Svigg data (via the same
EMRSessionManager the rest of the app uses) so staff can spot a request that has
already been handled before they act on it.

Deliberately ON-DEMAND and TARGETED (one card per click, at most one patient
search + one single-day calendar read) — never a background sweep and never the
21-day calendar scan the referral audit uses — so a click returns in seconds and
never hammers the EMR.

Honesty (house rule): the verdict only ever states what Svigg actually returned.
A lookup error is reported as an error, never a fabricated "resolved". A booking
is only called "likely handled" when the patient is POSITIVELY found on the
requested day's calendar; otherwise we hedge honestly. Records/billing requests
have no reliable EMR "was it fulfilled" signal, so they return "review manually".
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_NAME_NOISE = re.compile(
    r"\b(our client|client|patient|re|req(?:uest)?|for|appointment|appt|info|"
    r"medical records?|records?|billing|dob|please|the)\b[:\-]?", re.IGNORECASE)


def _patient_full(card: dict) -> str:
    """Best-effort full patient name from a card (structured params win)."""
    p = card.get("params") or {}
    struct = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).strip()
    if struct:
        return struct
    if p.get("patient_name"):
        return str(p["patient_name"]).strip()
    blob = str(p.get("title") or card.get("reason") or "")
    m = re.search(r"client[:\s]+([A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){0,2})", blob)
    return m.group(1).strip() if m else ""


def _name_parts(card: dict) -> tuple[str, str]:
    """Return (last_name, first_name) for a Svigg search. Structured wins."""
    p = card.get("params") or {}
    if p.get("last_name"):
        return str(p["last_name"]).strip(), str(p.get("first_name") or "").strip()
    full = _patient_full(card)
    if not full:
        return "", ""
    toks = full.split()
    return (toks[-1], toks[0]) if len(toks) > 1 else (toks[0], "")


def _name_on_day(day_rows, last: str, first: str) -> bool:
    """True if a PER-APPOINTMENT row on the day matches this patient.

    Row-level (not a substring of the whole-day JSON, which would false-positive
    on ANY unrelated same-surname patient). When a first name is known we require
    BOTH the last name and the first name (or its initial) in the SAME row, so a
    different 'Smith' on the calendar does not read as 'already handled'.
    """
    ln = (last or "").strip().lower()
    fn = (first or "").strip().lower()
    if not ln:
        return False
    for row in (day_rows or []):
        blob = (" ".join(str(v) for v in row.values()) if isinstance(row, dict)
                else str(row)).lower()
        if ln not in blob:
            continue
        if not fn or fn in blob or (fn[:1] and fn[:1] + "." in blob):
            return True
    return False


def _to_iso(date_mdy: str) -> str:
    """MM/DD/YYYY -> YYYY-MM-DD (get_appointment_calendar wants ISO). '' on fail."""
    try:
        return datetime.strptime(str(date_mdy).strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


async def verify_need(card: dict, ) -> dict:
    """Return {verdict, detail}. verdict ∈ {likely_resolved, still_open,
    exists_manual, no_patient, error}."""
    action = card.get("action_type") or ""
    params = card.get("params") or {}
    last, first = _name_parts(card)
    display = _patient_full(card) or last
    if not last:
        return {"verdict": "no_patient",
                "detail": "No clear patient name on this card to look up — verify "
                          "manually from the message thread."}

    try:
        from emr_session_manager import EMRSessionManager
        mgr = EMRSessionManager.get_instance()
        matches = await mgr.svigg_search(last, first)
    except Exception as exc:  # honest failure — never a fabricated verdict
        logger.warning("verify_need: svigg_search failed for %r: %s", last, exc)
        return {"verdict": "error",
                "detail": f"Could not reach Svigg to check — verify {display} manually."}

    if not matches:
        return {"verdict": "still_open",
                "detail": f"{display} was not found in Svigg — this request still "
                          f"stands (patient needs intake/booking)."}

    booking_like = (action in ("book_appointment", "reschedule_appointment")
                    or params.get("request_type") == "scheduling")
    iso = _to_iso(params.get("date_mdy", ""))

    # Fast path: a booking with a concrete date → read ONLY that day's calendar.
    if (booking_like or action == "cancel_appointment") and iso:
        try:
            day = await mgr.svigg_appointment_calendar(iso)
        except Exception as exc:
            logger.warning("verify_need: calendar read failed: %s", exc)
            return {"verdict": "exists_manual",
                    "detail": f"{display} is in Svigg, but the {params['date_mdy']} "
                              f"calendar could not be read — verify manually."}
        on_day = _name_on_day(day, last, first)
        if action == "cancel_appointment":
            if on_day:
                return {"verdict": "still_open",
                        "detail": f"{display} still appears on the {params['date_mdy']} "
                                  f"calendar — the cancel has not happened yet."}
            return {"verdict": "likely_resolved",
                    "detail": f"{display} is not on the {params['date_mdy']} calendar "
                              f"— may already be cancelled. Confirm before acting."}
        if on_day:
            return {"verdict": "likely_resolved",
                    "detail": f"{display} is ALREADY on the {params['date_mdy']} "
                              f"calendar — this booking may already be handled. "
                              f"Confirm before acting."}
        return {"verdict": "still_open",
                "detail": f"{display} is in Svigg but NOT on the {params['date_mdy']} "
                          f"calendar — still needs booking."}

    # Found, but no concrete date to check cheaply, or a non-booking request.
    if booking_like:
        return {"verdict": "exists_manual",
                "detail": f"{display} is an established Svigg patient, but this card "
                          f"has no specific date — open the Svigg calendar to confirm "
                          f"whether they're already scheduled."}
    return {"verdict": "exists_manual",
            "detail": f"{display} is in Svigg. A records/billing request can't be "
                      f"auto-verified as fulfilled — review the thread manually."}

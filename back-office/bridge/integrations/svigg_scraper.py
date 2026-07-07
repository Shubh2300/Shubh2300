# VENDORED from mainlinesurgery-a11y/n8n-office @ backoffice-autopilot-live-20260705, commit eec3888
# Source path: python/integrations/svigg_scraper.py
# Re-vendored from the REAL, HAR-verified production repo (do not edit lightly).
# Svigg/WEBeDoctor browser RPA client: reads + triple-gated writes, fail-closed identity guard, proof screenshot on every terminal outcome, enc-keyed cancel.
#!/usr/bin/env python3
"""
svigg_scraper.py - Playwright scraper for Svigg/Dr.Com/WEBeDoctor EMR

Real selectors discovered 2026-06-28 by reading the live portal.
Svigg = billing + appointments EMR for Atlantic Pain & Wellness.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BOOKING COMMIT KILL-SWITCH (safety locks — fail-closed, triple-gated)
# ---------------------------------------------------------------------------
# The Doctor.com/Svigg appointment-COMMIT path (POST to bk_p) writes to the
# live production calendar. The bk_p route + form-field contract were mapped
# from the 2026-07-01 HAR (dates MM/DD/YYYY; times HH:MMAM per the grid labels;
# cpt00/prov are <select>s; slot x/y encodes day+time). It is gated behind
# THREE independent, fail-closed locks:
#   1. BOOKING_EXECUTE_ENABLED — env SVIGG_BOOKING_EXECUTE must be truthy. NOT
#      hard-coded True, so a commit is never left open in source.
#   2. the caller must pass execute=True AND confirm_unverified=True.
#   3. the patient acct must be in BOOKING_ALLOWED_ACCTS, checked AT the POST
#      site — so a real patient can never be committed even if 1+2 are open.
BOOKING_EXECUTE_ENABLED = os.environ.get("SVIGG_BOOKING_EXECUTE", "").lower() in ("1", "true", "yes")

# Hard allowlist: only these accts may ever be committed.
# "22041163" = "Patients 1, Test" — the designated test record.
#
# GO-LIVE: to book real patients, EXTEND this allowlist WITHOUT editing source by
# setting env SVIGG_BOOKING_ALLOWLIST to a comma-separated list of accts, e.g.
#   SVIGG_BOOKING_ALLOWLIST="22041163,10456789"
# or the literal "*" to allow ANY acct (only after the booking path is trusted in
# production — a wildcard removes the per-acct guard entirely). DEFAULTS ARE
# FAIL-CLOSED: with no env set, only the test acct above can ever be committed.
BOOKING_ALLOWED_ACCTS = {"22041163"}
_booking_allowlist_env = os.environ.get("SVIGG_BOOKING_ALLOWLIST", "")
for _entry in _booking_allowlist_env.split(","):
    _entry = _entry.strip()
    if _entry:
        BOOKING_ALLOWED_ACCTS.add(_entry)

# CANCEL is destructive AND locates the appointment by patient NAME on the grid,
# so an allowlisted acct alone does not bind the deletion to the intended record.
# Bind each cancel-allowed acct to a required last-name token: the caller's
# last_name must contain it, so passing an allowlisted acct + a real patient's
# name cannot reach/delete that real patient's appointment.
#
# GO-LIVE: to cancel real patients, EXTEND this map WITHOUT editing source by
# setting env SVIGG_CANCEL_ALLOWLIST to comma-separated "acct:nametoken" pairs,
# e.g. SVIGG_CANCEL_ALLOWLIST="22041163:patient,10456789:smith"
# A literal "*" entry sets CANCEL_ALLOW_ANY=True, which drops the acct→name
# binding entirely and permits cancelling any appointment whose last_name is
# non-empty (only after the cancel path is trusted in production — the name
# binding is the last guard preventing an allowlisted acct from being aimed at a
# real patient's row). DEFAULTS ARE FAIL-CLOSED: with no env set, only the test
# acct/name above can be cancelled and CANCEL_ALLOW_ANY stays False.
CANCEL_ALLOWED_ACCT_NAMES = {"22041163": "patient"}
CANCEL_ALLOW_ANY = False
_cancel_allowlist_env = os.environ.get("SVIGG_CANCEL_ALLOWLIST", "")
for _entry in _cancel_allowlist_env.split(","):
    _entry = _entry.strip()
    if not _entry:
        continue
    if _entry == "*":
        CANCEL_ALLOW_ANY = True
        continue
    if ":" in _entry:
        _acct, _tok = _entry.split(":", 1)
        _acct = _acct.strip()
        _tok = _tok.strip().lower()
        if _acct and _tok:
            CANCEL_ALLOWED_ACCT_NAMES[_acct] = _tok

# ---------------------------------------------------------------------------
# FORM-FAITHFUL BOOKING CONTRACT — pure helpers (HAR-verified 2026-07-05)
# ---------------------------------------------------------------------------
# A real human booking+cancel HAR (websrv01.physician-to-go.net, captured
# 2026-07-05, Test Patient acct 22041163) showed the successful commit leaves
# the conf form's slot-hunt block (FromDate/FromTime1/ToDate/ToTime1/weekday
# checkboxes) at whatever the server rendered — the human only ever touches
# cpt00, Duration00, note00, and Submit. The bound slot comes from the staged
# grid cell (add.htm?x&y -> addLookup -> calAddLookup?rowid&acct), NOT from
# those date/time fields. Overriding FromDate synthetically is what collides
# with the server's Callback-Table "From Appt Date" check and bounces the
# whole booking (documented live incident, 2026-07-07) — see book_appointment.
#
# These are pure (no I/O, no Playwright) so they can be unit-tested offline;
# the async method below only wires them to the live DOM.

def _apply_overrides(
    serialized: dict,
    appt_type: str,
    duration_min: int,
    note: str,
    incident: Optional[str] = None,
) -> dict:
    """Return a COPY of `serialized` with ONLY the human-touched keys changed.

    `serialized` is the verbatim {name: value} form-field snapshot read live
    from form[name="conf"] (including FromDate/FromTime1/ToDate/ToTime1/off/
    weekday checkboxes/Incident/TFORMCOUNT/etc. exactly as rendered). This
    function must NOT touch FromDate, FromTime1, ToDate, ToTime1, or any
    weekday key (Sunday..Saturday) — those stay at the server's rendered
    defaults, per the 2026-07-05 HAR. It overrides only:
      cpt00, Duration00 (as str), note00, Note.
    Incident is left as rendered UNLESS the caller explicitly passed one
    (incident is not None) — an explicit empty string ("") still counts as
    "explicitly passed" and will clear it.
    """
    out = dict(serialized)
    out["cpt00"] = appt_type
    out["Duration00"] = str(duration_min)
    out["note00"] = note
    out["Note"] = note
    if incident is not None:
        out["Incident"] = incident
    return out


def _has_overbook_control(html: str) -> bool:
    """True iff `html` contains a submit control offering Submit=Overbook.

    HAR-verified recovery path: on a slot conflict the server re-presents the
    SAME conf form with an extra control — an <input name="Submit"
    value="Overbook"> (or a <button>...Overbook...</button>) — that the human
    clicks to resubmit the identical fields and force the booking. Detection
    is case-insensitive and is scoped to actual submit CONTROLS, never to
    unrelated prose (e.g. an error message that happens to say "over
    allocated. Hit Overbook to force." must NOT match here).
    """
    if not html:
        return False
    return bool(re.search(
        r'<(?:input|button)\b[^>]*\bname\s*=\s*["\']?submit["\']?[^>]*\bvalue\s*=\s*["\']?overbook\b',
        html, re.IGNORECASE
    )) or bool(re.search(
        r'<(?:input|button)\b[^>]*\bvalue\s*=\s*["\']?overbook["\']?[^>]*\bname\s*=\s*["\']?submit\b',
        html, re.IGNORECASE
    )) or bool(re.search(
        r'<button\b[^>]*>\s*overbook\s*<\s*/\s*button\s*>', html, re.IGNORECASE
    ))


def _is_callback_conflict(html: str) -> bool:
    """True iff `html` shows the server's Callback-Table collision message.

    HAR/live-verified (2026-07-07 incident): a residual entry in the site's
    Callback Table for this patient/date — typically left by a prior CANCEL
    that populated AutoReserve (see cancel_appointment / _autoreserve_action) —
    causes any subsequent booking for that same patient/date to bounce with
    this exact message, creating nothing.
    """
    if not html:
        return False
    return "already exists in callback table" in html.lower()


def _autoreserve_action(field_kind: str, current_value, is_checked: Optional[bool] = None) -> tuple[str, object]:
    """Decide how to neutralize the cancel_p form's AutoReserve field.

    The HAR's clean human cancel submits AutoReserve EMPTY; a populated
    AutoReserve is what seeds the Callback Table (Defect 2). This is a pure
    decision helper — it does not touch the DOM.

    Args:
        field_kind: "checkbox" | "select" | "text" | "hidden" | "absent".
        current_value: the field's current value (ignored for "absent").
        is_checked: for field_kind=="checkbox", whether it is currently checked.

    Returns (result_label, new_value):
        result_label: one of "cleared" | "already_empty" | "not_present" —
            this is exactly the string recorded in cancel_appointment's result
            under the "auto_reserve" key.
        new_value: what to set the field to (None means "uncheck"/no DOM
            write needed); meaningless when result_label=="not_present".
    """
    if field_kind == "absent":
        return ("not_present", None)
    if field_kind == "checkbox":
        if is_checked:
            return ("cleared", False)
        return ("already_empty", False)
    # select / text / hidden — empty-string comparison.
    if current_value in (None, ""):
        return ("already_empty", "")
    return ("cleared", "")


# ---------------------------------------------------------------------------
# GRID DAY-COLUMN MAPPING — pure helper (fixed 2026-07-05, live incident)
# ---------------------------------------------------------------------------
# LIVE INCIDENT (2026-07-05): book_appointment's slot picker took the FIRST
# free add.htm?x&y anchor on the whole book.htm grid without checking which
# day-column that anchor belonged to. book.htm renders MULTIPLE days
# side-by-side (each day is its own small <table>, matching the "23 tables"
# discovery noted in get_appointment_calendar's docstring) — on a day with NO
# clinic session the first free anchor found is silently from some OTHER
# day's column, so the booking lands on a date nobody asked for (and, in the
# incident, on a day with no session at all) while reporting a false
# "submitted_overbooked" success. This helper is the fix: it parses the
# grid's day-column headers so callers can restrict slot selection to the
# REQUESTED date's column only. Pure (no I/O, no Playwright) — unit-testable
# offline against synthetic book.htm-like HTML.

def _grid_day_columns(html: str) -> dict[int, str]:
    """Map each grid day-column's `x` index to its calendar date.

    WEEK-MODE UTILITY, NOT USED FOR BOOKING (retired from the booking path
    2026-07-05 per the live-proven ONE-DAY MODE fix — see
    `_apply_oneday_filter`/`_oneday_applied`). Week/multi-day book.htm views
    were found live to render header ORDER independent of x-coordinate order
    (x=3 was Jul 8, x=1 was Jul 13, x=2 was Jul 14 in one capture), and
    free-slot anchors used entirely different x values than the booked cells
    in the SAME view — so mapping x→date from week-mode headers is
    fundamentally unreliable for staging a slot. `book_appointment` and
    `_verify_booking_on_grid` now use `_apply_oneday_filter` (Svigg's OneDay
    checkbox), which renders exactly one day and makes every anchor's x=0 —
    no day-column disambiguation is needed at all in that mode. This function
    and its tests are kept only as a still-useful, still-tested week-mode
    parsing utility; do not wire it back into booking/cancel/verification.

    book.htm's free/booked-slot anchors (`add.htm?x=N&y=M`, `mre?x=N&y=M&r=R`)
    carry an `x` that selects a DAY-COLUMN, not an absolute date — the grid
    shows a multi-day window and each day is rendered as its own table
    (mirrors the "23 tables" structure get_appointment_calendar already
    walks). This walks EVERY `<table>` in the page and, for each one, looks
    for a header cell (a `<th>`, or a `<td>` that carries no `add.htm`/`mre`
    anchor of its own) containing an MM/DD or MM/DD/YYYY date. Every
    `add.htm?x=` / `mre?x=` anchor found inside that SAME table is then
    recorded under that date, keyed by its `x` value — so a table spanning
    several x's (sub-columns for provider/room) still maps every one of
    those x's to the one date the whole table represents.

    2-digit-year or year-less header dates (`MM/DD` or `MM/DD/YY`) are
    resolved against `year_hint`'s year (falling back to the current year
    when no hint is given) — Svigg's rendered headers have been observed to
    omit the year entirely on the day-column labels.

    Args:
        html: raw HTML of the book.htm frame content (whole page, may
            contain many tables — only tables that actually carry a
            recognizable header date AND at least one x-tagged anchor
            contribute entries).

    Returns:
        {x_index: 'MM/DD/YYYY'} for every day-column x discovered. Empty
        dict if the page has no recognizable day-column headers (e.g. an
        error page, an empty grid, or a page shape this parser doesn't
        recognize) — callers must treat that as "unknown", never guess.
    """
    if not html:
        return {}
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {}

    from datetime import datetime as _dt

    soup = BeautifulSoup(html, "html.parser")
    current_year = _dt.now().year

    # MM/DD/YYYY, MM/DD/YY, or bare MM/DD (year resolved against current_year).
    date_re = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")

    def _resolve_date(mo: "re.Match") -> Optional[str]:
        mm, dd, yy = mo.group(1), mo.group(2), mo.group(3)
        try:
            month, day = int(mm), int(dd)
        except ValueError:
            return None
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        if yy:
            year = int(yy)
            if year < 100:
                year += 2000
        else:
            year = current_year
        try:
            return _dt(year, month, day).strftime("%m/%d/%Y")
        except ValueError:
            return None

    x_re = re.compile(r"[?&]x=(\d+)")

    mapping: dict[int, str] = {}

    for table in soup.find_all("table"):
        # Collect every x-tagged anchor (add.htm or mre) that belongs to
        # THIS table (BeautifulSoup's find_all on a Tag is already scoped to
        # its own descendants, so nested tables are only double-counted if
        # they are truly nested — acceptable: a nested day-table still gets
        # the correct header applied to it independently on its own pass).
        table_xs: set[int] = set()
        header_date: Optional[str] = None

        for link in table.find_all("a", href=True):
            href = link.get("href", "")
            if "add.htm" not in href and "mre?" not in href:
                continue
            xm = x_re.search(href)
            if xm:
                table_xs.add(int(xm.group(1)))

        if not table_xs:
            continue  # this table has no day-column anchors at all

        # Look for the header date among <th> cells first (most explicit),
        # then any <td> that itself carries no day-column anchor (a plain
        # label cell, not a slot cell) — scan in document order and take the
        # FIRST date-shaped text found, which is the table's own day label.
        header_cells = table.find_all("th")
        if not header_cells:
            header_cells = [
                td for td in table.find_all("td")
                if not td.find("a", href=re.compile(r"add\.htm|mre\?"))
            ]
        for cell in header_cells:
            text = cell.get_text(" ", strip=True)
            mo = date_re.search(text)
            if mo:
                resolved = _resolve_date(mo)
                if resolved:
                    header_date = resolved
                    break

        if header_date is None:
            continue  # no recognizable date label for this table — skip it

        for x in table_xs:
            mapping[x] = header_date

    return mapping


# ---------------------------------------------------------------------------
# ONE-DAY MODE — pure helpers (live-proven 2026-07-05)
# ---------------------------------------------------------------------------
# LIVE-PROVEN CONTRACT: checking calfilt's `input[name="OneDay"]`, filling
# `dt`, and clicking `input[name="thismon"]` (an IMAGE button — NOT the `GO`
# submit) makes book.htm render EXACTLY ONE day. In that mode every add.htm/
# mre anchor's `x` is 0 (single column) and the frame's own header carries
# `oneday.htm?dt=YYYYMMDD` link(s) that name the day actually rendered — this
# is the authoritative, checkable signal that the filter actually retargeted
# the view (fill-dt + click GO was live-proven UNRELIABLE: a cancel refused
# with "date filter not applied" and a booking landed on 2026-07-13 instead
# of the requested date). `_oneday_applied` is the pure, offline-testable
# check factored out of `_apply_oneday_filter` below.

_ONEDAY_HEADER_RE = re.compile(r"oneday\.htm\?dt=(\d{8})")


def _oneday_applied(html: str, date_mdY: str) -> bool:
    """Check whether a book.htm page is genuinely a ONE-DAY view of `date_mdY`.

    Success = the set of `oneday.htm?dt=YYYYMMDD` header links found in
    `html` is EXACTLY `{YYYYMMDD}` for the requested `date_mdY` (MM/DD/YYYY)
    — one link, matching. Anything else (zero header links, more than one
    distinct date, or a single link for the WRONG date) means the filter did
    not retarget the view the way we need, and callers must treat that as
    "not applied" rather than guessing. Pure (no I/O) so it is unit-testable
    against synthetic HTML.

    Args:
        html: raw HTML to scan for `oneday.htm?dt=` header links (whole
            frameset/frame content — regex scan, no DOM parse required).
        date_mdY: the date that was requested, "MM/DD/YYYY".

    Returns:
        True iff exactly one distinct header date is present and it matches
        `date_mdY`; False otherwise (including on malformed/empty input —
        never raises).
    """
    if not html or not date_mdY:
        return False
    try:
        from datetime import datetime as _dt
        wanted = _dt.strptime(date_mdY.strip(), "%m/%d/%Y").strftime("%Y%m%d")
    except ValueError:
        return False
    found = set(_ONEDAY_HEADER_RE.findall(html))
    return found == {wanted}


def _cancel_reread_ok(cal_after: list, filter_applied: bool) -> bool:
    """Decide whether a post-cancel calendar re-read is TRUSTWORTHY proof.

    The re-read is trustworthy iff the ONE-DAY filter was confirmed applied
    (``filter_applied``) AND the re-read did not hard-error. Crucially, a
    filter-confirmed EMPTY day (``cal_after == []``) IS a trustworthy re-read
    — that is exactly the successful-cancel case where the day's only
    appointment was just deleted (the marker that normally rides on row dicts
    is gone precisely BECAUSE the cancel worked). Every failure path in
    ``get_appointment_calendar`` returns a NON-empty ``[{"error": ...}]`` list,
    never ``[]``, so an error is detectable independently of emptiness.

    Args:
        cal_after: the list returned by ``get_appointment_calendar`` after the
            cancel — ``[]`` for a genuinely empty day, ``[{"error": ...}]`` on
            failure, or a list of appointment dicts.
        filter_applied: the authoritative ONE-DAY-filter confirmation from
            ``get_appointment_calendar``'s ``meta`` out-param (True only when
            ``_apply_oneday_filter`` confirmed the requested single day).

    Returns:
        True iff the filter was confirmed applied and the re-read did not
        hard-error (an empty filtered day counts as a valid, trustworthy
        re-read); False otherwise. Pure (no I/O), never raises.
    """
    if not filter_applied:
        return False
    if cal_after and isinstance(cal_after[0], dict) and cal_after[0].get("error"):
        return False
    return True


# ---------------------------------------------------------------------------
# HAR-FAITHFUL ENCOUNTER-KEYED CANCEL — pure helpers (HAR-verified 2026-07-05)
# ---------------------------------------------------------------------------
# The 2026-07-05 HAR of a human's WORKING manual cancel (websrv01.physician-to-
# go.net, done twice) proved the PERSISTING delete is keyed on the appointment's
# ENCOUNTER id (enc), NOT the book.htm grid cell (mre?x=&y=&r=). The old cancel
# drove the mre grid-edit dialog -> input[name="Delete"] -> a `cancel_p` confirm
# form; NEITHER `mre?` NOR `cancel_p` appears anywhere in the working HAR
# (0 occurrences each), and that path did NOT remove the appointment. The real,
# persisting sequence, per encounter, is:
#   1. GET /proxy.cgi/<SESSION>/resched.htm?enc=<ENC>
#        (reached from the patient chart's appt/encounter list plist.htm, whose
#         body carries the <SESSION>-tokened resched links, one per appt; the
#         9-digit <SESSION> is minted at that plist->resched hop)
#   2. GET /proxy.cgi/<SESSION>/resched_p.htm?TFORMCOUNT=<N>&Note1=&Delete=Delete
#        (Delete-confirmation page)
#   3. GET /proxy.cgi/<SESSION>/cancel2_p?TFORMCOUNT=<N+1>&CancelReason=<or|pr>&Yes=Yes
#        (COMMITS the delete; CancelReason "or"=Office, "pr"=Patient)
# TFORMCOUNT is a SESSION-GLOBAL incrementing form counter — observed 3->4 for
# the first appt and 13->14 for the second (every intermediate page load bumps
# it), so it is NOT computable and MUST be read from each rendered form. The enc
# is obtained from an appt_e.htm?...&enc=<ENC> link on the per-day schedule
# report (get_schedule_day / appt_b.htm already extract it as encounter_id).
#
# These helpers are pure (no I/O, no Playwright) so they can be unit-tested
# offline; the async cancel method only wires them to the live DOM/navigation.

# Valid Svigg CancelReason select values (HAR-verified): "or"=Office Requested,
# "pr"=Patient Requested. Anything else is a caller error (fail-closed).
_CANCEL_REASON_VALUES = ("or", "pr")


def _map_cancel_reason(reason: str) -> str:
    """Validate + normalize a CancelReason to a Svigg-accepted value.

    Accepts the raw Svigg codes ("or"/"pr", case-insensitive) OR the two
    human words the approvals layer may pass ("office"/"patient"). Returns the
    canonical two-letter code. Raises ValueError on anything else — the cancel
    flow MUST fail closed rather than submit an unknown reason (the confirm
    form bounces unchanged on a bad/absent reason, which would look like a
    silent no-op).
    """
    r = (reason or "").strip().lower()
    if r in _CANCEL_REASON_VALUES:
        return r
    if r in ("office", "office requested", "office_requested"):
        return "or"
    if r in ("patient", "patient requested", "patient_requested"):
        return "pr"
    raise ValueError(
        f"reason must be one of {_CANCEL_REASON_VALUES} (or office/patient), "
        f"got {reason!r}")


def _enc_matches_time(link_time: str, want_time: str) -> bool:
    """True iff a schedule link's time (e.g. "09:15AM"/"9:15 am"/"Noon") equals
    the requested time. Both sides are normalized to the grid's 15-min row index
    so "09:15AM" == "9:15AM" == "9:15 am". "Noon" maps to 12:00PM. An empty
    ``want_time`` is a wildcard (matches any) — callers that pass no time accept
    any single same-day appt (ambiguity is caught separately). Unparseable
    inputs never match (fail-closed), never raise.
    """
    if not want_time:
        return True
    if not link_time:
        return False

    def _row(t: str):
        s = (t or "").strip().upper().replace(" ", "")
        if s in ("NOON", "12NOON"):
            s = "12:00PM"
        if s in ("MIDNIGHT",):
            s = "12:00AM"
        from datetime import datetime as _dt
        for fmt in ("%I:%M%p", "%H:%M"):
            try:
                p = _dt.strptime(s, fmt)
                return p.hour * 60 + p.minute
            except ValueError:
                continue
        return None

    a, b = _row(link_time), _row(want_time)
    return a is not None and b is not None and a == b


def _extract_encs_from_schedule(html: str, last_name: str,
                                date_iso: str, time: str = "") -> list[dict]:
    """Resolve the target appointment's encounter id(s) from a per-day schedule
    report's HTML (the appt_b.htm page that get_schedule_day scrapes).

    Every appointment row carries an ``<a href=...appt_e.htm?date=MM/DD/YYYY&
    time=HH:MMAM&enc=NNNN&prov=...>`` link. This scans those links, keeps only
    the ones whose ``date`` matches ``date_iso`` AND whose surrounding row text
    contains ``last_name`` (case-insensitive) AND (when ``time`` is given) whose
    ``time`` matches, and returns the distinct matches as
    ``[{"enc","date","time","prov"}]``.

    Returns [] when nothing matches (caller -> not_found). More than one entry
    means genuine ambiguity (caller -> ambiguous refusal; NEVER auto-pick).
    Pure: parses a string, no I/O, never raises (a parse failure -> []).
    """
    if not html or not last_name:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    def _iso(mdy: str) -> str:
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", mdy or "")
        if not m:
            return ""
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    ln = last_name.strip().lower()
    out: list[dict] = []
    seen: set = set()
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if "appt_e.htm" not in href:
            continue
        q = parse_qs(urlparse(href.replace("&amp;", "&")).query)
        enc = (q.get("enc") or [""])[0].strip()
        if not enc:
            continue
        row_mdy = (q.get("date") or [""])[0]
        if _iso(row_mdy) != date_iso:
            continue
        link_time = (q.get("time") or [""])[0]
        prov = (q.get("prov") or [""])[0]
        # Bind by patient last name: match against the appointment ROW text, not
        # just the link, so a bare "Edit" link still binds to its patient.
        tr = link.find_parent("tr")
        row_text = (tr.get_text(" ", strip=True) if tr else link.get_text(
            " ", strip=True)) or ""
        if ln not in row_text.lower():
            continue
        if not _enc_matches_time(link_time, time):
            continue
        if enc in seen:
            continue
        seen.add(enc)
        out.append({"enc": enc, "date": date_iso,
                    "time": link_time, "prov": prov})
    return out


def _extract_resched_link(plist_html: str, enc: str) -> Optional[dict]:
    """Find the session-tokened resched link for ``enc`` in a patient-chart
    plist page body and return ``{"path","session","enc"}``.

    The plist page (reached from the encounter-module patient search) renders,
    per appointment, an ``<a href=".../proxy.cgi/<SESSION>/resched.htm?enc=<ENC>">``
    (the 9-digit <SESSION> is minted for this page). This returns the FIRST link
    whose parsed ``enc`` equals the requested one, with:
      path    — the "/proxy.cgi/<SESSION>/resched.htm?enc=<ENC>" path (leading
                slash, no host) ready to join onto BASE_URL,
      session — the numeric session token (str),
      enc     — the confirmed enc (str).
    Returns None when no matching resched link is present (caller -> fail
    closed, never fabricate a token). Pure; never raises.
    """
    if not plist_html or not enc:
        return None
    want = str(enc).strip()
    # Match /proxy.cgi/<digits>/resched.htm?...enc=<want>... in any href. The
    # session token is >=6 digits (9 in practice); enc appears in the query.
    pat = re.compile(
        r'/proxy\.cgi/(\d{6,})/resched\.htm\?([^"\'<>\s]*)', re.IGNORECASE)
    for m in pat.finditer(plist_html.replace("&amp;", "&")):
        session = m.group(1)
        query = m.group(2)
        q = parse_qs(query)
        if (q.get("enc") or [""])[0].strip() == want:
            return {
                "path": f"/proxy.cgi/{session}/resched.htm?enc={want}",
                "session": session,
                "enc": want,
            }
    return None


def _build_resched_p_delete_url(base_url: str, session: str,
                                tformcount: str) -> str:
    """Build the resched_p Delete-confirmation URL, HAR-exact:
      {base}/proxy.cgi/<SESSION>/resched_p.htm?TFORMCOUNT=<N>&Note1=&Delete=Delete
    ``tformcount`` is scraped live from the resched.htm form (session-global,
    not computable). Raises ValueError on a missing session/tformcount so the
    flow fails closed instead of firing a malformed delete.
    """
    if not session or tformcount in (None, ""):
        raise ValueError("resched_p URL needs both session and TFORMCOUNT")
    return (f"{base_url}/proxy.cgi/{session}/resched_p.htm"
            f"?TFORMCOUNT={tformcount}&Note1=&Delete=Delete")


def _build_cancel2_p_url(base_url: str, session: str, tformcount: str,
                         reason: str) -> str:
    """Build the cancel2_p COMMIT URL, HAR-exact:
      {base}/proxy.cgi/<SESSION>/cancel2_p?TFORMCOUNT=<N>&CancelReason=<or|pr>&Yes=Yes
    ``tformcount`` is scraped from the resched_p confirmation form (the counter
    the server rendered AFTER the Delete step — observed N+1 vs the Delete URL).
    ``reason`` is validated via _map_cancel_reason. Raises ValueError on a bad
    reason or a missing session/tformcount so the commit fails closed.
    """
    if not session or tformcount in (None, ""):
        raise ValueError("cancel2_p URL needs both session and TFORMCOUNT")
    code = _map_cancel_reason(reason)  # raises on unknown reason
    return (f"{base_url}/proxy.cgi/{session}/cancel2_p"
            f"?TFORMCOUNT={tformcount}&CancelReason={code}&Yes=Yes")


def _extract_tformcount(html: str) -> Optional[str]:
    """Read the session-global TFORMCOUNT the server rendered into a resched /
    resched_p form. Prefers a ``<input name="TFORMCOUNT" value="N">`` hidden
    field; falls back to a ``TFORMCOUNT=N`` occurrence in a resched_p/cancel2_p
    href on the page. Returns the digit string or None (caller fails closed).
    Pure; never raises.
    """
    if not html:
        return None
    m = re.search(
        r'<input\b[^>]*\bname\s*=\s*["\']?TFORMCOUNT["\']?[^>]*\bvalue\s*=\s*'
        r'["\']?(\d+)', html, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(
        r'<input\b[^>]*\bvalue\s*=\s*["\']?(\d+)["\']?[^>]*\bname\s*=\s*'
        r'["\']?TFORMCOUNT', html, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'TFORMCOUNT=(\d+)', html)
    if m:
        return m.group(1)
    return None


def _choose_rowid_from_matches(matches: list, acct: str) -> dict:
    """Decide the chart-open rowid from a list of search_patient() hits, keyed
    by ACCT (never by a parsed patient name).

    The cancel flow must open the patient chart (plist.htm?rowid&acct) to read
    the session-tokened resched link, but our booking flow leaves rowid empty,
    so it is resolved live. The account number is the reliable identifier — the
    app's parsed last_name can differ from the Svigg record (e.g. acct 22041163
    is passed as "Patients 1" but the record's bound name is "patient"), so the
    rowid is chosen by a UNIQUE acct match via _pick_acct_match, and only when
    that single match actually carries a plausible Svigg rowid.

    Returns ``{"rowid": str, "acct": str, "name": str, "dob": str,
    "status": str, "match_count": int}`` where status is:
      "ok"          — exactly one acct match with a plausible rowid; use it.
      "no_match"    — zero acct matches (fail closed).
      "ambiguous"   — >1 acct matches (fail closed, never guess).
      "no_rowid"    — the single acct match had no/implausible rowid (fail
                      closed rather than open a chart with a bogus id).
    ``dob`` carries the picked chart's own DOB when search_patient parsed one
    ("" otherwise) — surfaced so the cancel path's pre-delete identity guard
    can bind on name+DOB, not name alone. Pure (no I/O), never raises. The
    caller does the live search + navigation.
    """
    pick = _pick_acct_match(matches, acct)
    match_count = pick["match_count"]
    if pick["picked"] is None:
        return {"rowid": "", "acct": acct, "name": "", "dob": "",
                "status": ("ambiguous" if match_count > 1 else "no_match"),
                "match_count": match_count}
    picked = pick["picked"]
    rowid = str(picked.get("rowid", "") or "").strip()
    if not rowid or not _is_plausible_svigg_rowid(rowid):
        return {"rowid": "", "acct": picked.get("acct", "") or acct,
                "name": (picked.get("name") or "").strip(),
                "dob": (picked.get("dob") or "").strip(),
                "status": "no_rowid", "match_count": match_count}
    return {"rowid": rowid, "acct": picked.get("acct", "") or acct,
            "name": (picked.get("name") or "").strip(),
            "dob": (picked.get("dob") or "").strip(),
            "status": "ok", "match_count": match_count}


def _parse_mre_label(label: str) -> dict:
    """Parse a book.htm `mre?x=&y=&r=` cell label into its components.

    Real shape (live-proven 2026-07-05, &nbsp;-laden): `'(15)&nbsp;Patients
    1,&nbsp;Tes&nbsp;/Est'`. The label's separators are rendered as `&nbsp;`
    (or, once the browser/BeautifulSoup decodes entities, `\xa0`) rather than
    plain spaces — a label-matching pass that does not normalize BOTH forms
    to spaces first will silently miss real patients (this broke a live
    search for the test patient the night this helper was written). This
    parser normalizes both forms up front, then strips the leading
    `(NN)` duration and splits on the LAST `/` (appointment-type suffix) and
    the FIRST `,` (last-name / first-name+type boundary) — the last-name
    segment may itself contain spaces/digits (e.g. "Patients 1"), so it is
    everything before the first comma, not a single token.

    Args:
        label: raw or already-decoded cell text, e.g.
            "(15)&nbsp;Patients 1,&nbsp;Tes&nbsp;/Est",
            "(15)\xa0Carter, Ode\xa0/Est", or a label with no leading
            duration prefix at all.

    Returns:
        {"last": str, "first_frag": str, "type_frag": str} — any piece that
        can't be identified is "" (never fabricated), not omitted, so
        callers can always safely index all three keys. Whitespace-only
        input or a totally unparseable shape returns all-empty strings
        rather than raising.
    """
    text = (label or "").replace("&nbsp;", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return {"last": "", "first_frag": "", "type_frag": ""}

    # Strip a leading "(NN)" duration prefix, if present.
    dur_m = re.match(r"^\((\d+)\)\s*(.*)$", text)
    rest = dur_m.group(2).strip() if dur_m else text

    # Split off the appointment-type suffix on the LAST '/'.
    type_frag = ""
    if "/" in rest:
        rest, _, type_frag = rest.rpartition("/")
        rest = rest.strip()
        type_frag = type_frag.strip()

    # Split last name / first-name+fragment on the FIRST ','. The last-name
    # side may contain spaces (e.g. "Patients 1"), so we deliberately do NOT
    # take just the first token.
    last, _, first_frag = rest.partition(",")
    last = last.strip()
    first_frag = first_frag.strip()

    return {"last": last, "first_frag": first_frag, "type_frag": type_frag}


def _normalize_acct(value: str) -> str:
    """Whitespace/leading-zero-tolerant normalization for acct comparisons.

    Shared by the patient_select retry match (added 2026-07-05) and the
    acct-first resolve_patient path (added 2026-07-05) so both stages treat
    e.g. " 022041163 " and "22041163" as the same account.
    """
    v = (value or "").strip()
    stripped = v.lstrip("0")
    return stripped if stripped else v


def _is_plausible_svigg_rowid(r: str) -> bool:
    """True iff `r` looks like a real Svigg internal rowid.

    Live incident (2026-07-05, card with acct anchor rowid="46747"): the chat
    model can carry over a numeric SIS-style patientId it saw in an unrelated
    emr_search result and pass it through as a Svigg `rowid`. A real Svigg
    rowid is an opaque token containing "@main" (HAR-verified, e.g.
    "AAAYCzAFhAAO!k1AAC@main01") — nothing like a plain numeric id ever has
    this shape. Used to refuse to trust a bogus rowid so the resolve_patient
    stage below re-resolves it fresh instead of short-circuiting.

    Pure function, no I/O.
    """
    return bool(r) and isinstance(r, str) and "@main" in r


def _name_tokens_present(body_text: str, last: str, first: str) -> bool:
    """True iff every usable name token from `last`+`first` appears
    (case-insensitive) in `body_text`.

    Fixed 2026-07-05 (coordinator correction): the conf (bk_p) form has NO
    acct or name FIELD at all (HAR-verified — its inputs are only
    Incident/TFORMCOUNT/cpt00/Dept00/Duration00/note00/FromDate/FromTime1/
    prov/Note/ToDate/ToTime1/off/weekday-checkboxes/Submit). The page
    identifies the patient purely by DISPLAYED NAME in the page body (e.g.
    "Patients 1, Test"), so the fast-path identity check must also accept a
    name-token match, not only an acct match.

    Tokens are `last` and `first` split on whitespace, lowercased, with
    empty and single-character tokens dropped (e.g. "Patients 1" -> only
    "patients" survives; the trailing "1" is discarded as a 1-char token,
    matching the coordinator's worked example). ALL surviving tokens must
    be present as substrings of the (also lowercased) `body_text` — a
    partial match is not enough.

    FAIL-CLOSED (critical): if there are ZERO surviving tokens (e.g. both
    `last` and `first` are empty, or every token is <=1 char), this returns
    False — an absence of usable name material must never be treated as a
    vacuous match. Pure function, no I/O.
    """
    raw_tokens = f"{last or ''} {first or ''}".split()
    tokens = [t.lower() for t in raw_tokens if len(t) > 1]
    if not tokens:
        return False
    haystack = (body_text or "").lower()
    if not haystack:
        return False
    return all(tok in haystack for tok in tokens)


def _pick_acct_match(matches: list, acct: str) -> dict:
    """Pick the single search result whose acct matches `acct` (normalized).

    Pure function, no I/O — used by book_appointment's resolve_patient stage
    (fixed 2026-07-05, live incident: card #263 failed to resolve because the
    chat model split a pathological patient name differently than card #260,
    even though a valid, unique acct was present in the params; a name split
    must never be able to veto an exact, unique acct match).

    Args:
        matches: list of dicts as returned by search_patient(), each with at
            least an "acct" key (rowid/name optional-but-expected).
        acct: the caller-supplied account number to resolve.

    Returns:
        {"picked": dict|None, "match_count": int} — match_count is how many
        of `matches` have an acct that normalizes equal to `acct` (0, 1, or
        >1). "picked" is only non-None when match_count == 1 — an ambiguous
        (>1) or absent (0) acct match is never guessed at; the caller decides
        how to fail.
    """
    if not acct:
        return {"picked": None, "match_count": 0}
    norm_acct = _normalize_acct(acct)
    hits = [
        m for m in (matches or [])
        if m.get("acct") and _normalize_acct(m.get("acct", "")) == norm_acct
    ]
    if len(hits) == 1:
        return {"picked": hits[0], "match_count": 1}
    return {"picked": None, "match_count": len(hits)}


def _resolve_name_variants(last: str, first: str) -> list[tuple[str, str]]:
    """Bounded, ordered, deduped ladder of (last_name, first_name) search
    variants to try when resolving a patient by name — added 2026-07-05 to
    make resolve_patient deterministic when acct is present but the chat
    model may have split a pathological patient name incorrectly (live
    example: acct 22041163's true chart name is "Patients 1, Test" — i.e.
    LastName="Patients 1", FirstName="Test" — but the model sometimes hands
    us last="Test Patients", first="1").

    Pure function, no I/O. Each variant is meant to be tried in order against
    search_patient(), with results filtered through _pick_acct_match(...,
    acct) by the caller — a variant is only "accepted" via a unique acct
    match, never by name similarity alone (see book_appointment's
    resolve_patient stage). When no acct is available at all, the caller
    should use ONLY variant (a) (index 0) with the pre-existing
    exactly-one-match rule; it should not run the looser single-token
    variants without an acct filter to lean on.

    Variants, in order:
      a. (last, first) exactly as given.
      b. (first, last) — swapped, in case the model transposed the fields.
      c. (first whitespace-token of last, first whitespace-token of first)
         — handles a multi-word field being over-captured.
      d. Each distinct alphabetic token (len >= 3) from last+first, tried
         as LastName ALONE (FirstName=""), longest-name-recall style — this
         is what turns "Test Patients"/"1" into a search on LastName=
         "patients" alone, which prefix-matches the true chart "Patients 1,
         Test" (the "1" and "test" tokens are also candidates but a name
         search is PREFIX-based on LastName, so only alphabetic
         LastName-shaped tokens are useful here).

    Rules:
      - Tokens/pairs are lowercased for dedup comparison but returned in
        their original case (Svigg's search is case-insensitive per the
        office's HAR, so case doesn't matter functionally, but we don't
        invent casing).
      - Empty, whitespace-only, and tokens of length <= 2 are dropped
        (never search on "1" or "").
      - The final list is capped at ~6 entries total (a+b+c contribute at
        most 3; the token ladder (d) fills the remainder).
      - Order is preserved; duplicates (by lowercased pair) are removed,
        keeping the first occurrence.
    """
    last = (last or "").strip()
    first = (first or "").strip()

    candidates: list[tuple[str, str]] = []

    def _add(pair: tuple[str, str]) -> None:
        l, f = pair
        l = (l or "").strip()
        f = (f or "").strip()
        if not l and not f:
            return
        # A lone single/double-char field (e.g. FirstName="1") is not
        # useful on its own, but is fine when paired with a real LastName —
        # so we only drop it here if it's the ONLY content in the pair.
        if l and len(l) <= 2 and not f:
            return
        if f and len(f) <= 2 and not l:
            return
        candidates.append((l, f))

    # (a) as given
    _add((last, first))

    # (b) swapped
    _add((first, last))

    # (c) first whitespace-token of each side
    last_first_tok = last.split()[0] if last.split() else ""
    first_first_tok = first.split()[0] if first.split() else ""
    _add((last_first_tok, first_first_tok))

    # (d) each distinct alphabetic token (len >= 3), LastName-alone
    seen_tokens: set[str] = set()
    for raw_tok in f"{last} {first}".split():
        tok = raw_tok.strip()
        if len(tok) < 3 or not tok.isalpha():
            continue
        key = tok.lower()
        if key in seen_tokens:
            continue
        seen_tokens.add(key)
        _add((tok, ""))

    # Dedupe (case-insensitive on the pair), preserve order, cap at 6.
    out: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for pair in candidates:
        key = (pair[0].lower(), pair[1].lower())
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        out.append(pair)
        if len(out) >= 6:
            break
    return out


def build_cal_add_lookup_url(base_url: str, rowid: str, acct: str) -> str:
    """Build the HAR-verified calAddLookup URL for a slot already staged by
    the server session (add.htm click), letting patient_select skip the
    in-grid name/acct search entirely when both rowid and acct are already
    known (2026-07-05, per a real human-browser HAR: after clicking a free
    add.htm slot the human flow is a psrch POST *or*, when the identity is
    already known, a direct
    ``GET .../proxy.cgi/<SESSION>/calAddLookup?rowid=<rowid>&acct=<acct>``
    relative to the same psrch/addLookup frame's own URL).

    Pure function, no I/O. `base_url` is expected to be the psrch (or
    addLookup) frame's OWN url — e.g.
    ``https://websrv01.physician-to-go.net/proxy.cgi/218897906/psrch`` — and
    urljoin() with the relative ``calAddLookup?...`` replaces only the last
    path segment, preserving the rotating ``proxy.cgi/<SESSION>/`` base.

    rowid/acct are inserted RAW (NOT urlencoded) — the HAR shows the server
    itself renders rowid with literal ``!`` and ``@`` characters
    unescaped (e.g. ``AAAYCzAFhAAO!k1AAC@main01``), so encoding them here
    would produce a URL the server never actually issues.
    """
    return urljoin(base_url, f"calAddLookup?rowid={rowid}&acct={acct}")


def _norm_name_for_compare(name: str) -> str:
    """Loose normalization for the "does the resolved name look like the
    params name" advisory check only (never used to gate/veto a match).

    Case-insensitive, whitespace-collapsed, punctuation-stripped so trivial
    formatting differences ("Test Patients,1" vs "Patients 1, Test") don't
    trigger a spurious mismatch warning.
    """
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = sorted(text.split())
    return " ".join(tokens)


def _name_token_bag(name: str) -> set:
    """Normalized, punctuation-stripped, case-folded set of the >1-char word
    tokens in `name`. Pure. Mirrors _name_tokens_present's tokenization (drop
    empty and single-char tokens — "Patients 1" -> {"patients"}) but returns a
    SET so an order-independent comparison can be made across the two ways a
    name can be split ("Test Patients"/"1" vs "Patients 1, Test" -> same bag).
    """
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    return {t for t in text.split() if len(t) > 1}


def _norm_dob_for_compare(dob: str) -> str:
    """Digits-only normalization of a DOB for a deterministic equality check.

    Accepts MM/DD/YYYY, MM-DD-YYYY, ISO YYYY-MM-DD, or already-bare digits and
    returns an 8-digit MMDDYYYY string (ISO is reordered to MMDDYYYY so the two
    input orders compare equal). Returns "" for anything that is not exactly a
    recognizable 8-digit date — an unparseable/absent DOB compares as "no DOB",
    never as a match. Pure, never raises.
    """
    s = str(dob or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)  # ISO YYYY-MM-DD
    if m:
        yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
        return f"{int(mm):02d}{int(dd):02d}{int(yyyy):04d}"
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)  # M/D/YYYY
    if m:
        mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
        return f"{int(mm):02d}{int(dd):02d}{int(yyyy):04d}"
    digits = re.sub(r"\D", "", s)  # already-bare 8 digits (MMDDYYYY)
    return digits if len(digits) == 8 else ""


def _norm_phone_for_compare(phone: str) -> str:
    """Digits-only normalization of a phone number for the update_patient
    verify-after equality check. Pure, never raises.

    Svigg renders phones with varying punctuation ("(856) 536-7901",
    "856-536-7901", "8565367901") — comparing the raw strings would spuriously
    report a mismatch on a correctly-saved number. Reducing both the value we
    submitted and the value we re-read to their digits makes the verify robust
    to formatting. A leading US country-code '1' on an 11-digit number is
    dropped so "18565367901" and "8565367901" compare equal. Returns "" for a
    value with no digits (an honest "nothing to compare", never a match).
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _is_phone_field(form_field: str) -> bool:
    """True iff a Svigg edit-form field holds a phone number (so the verify uses
    digits-only comparison). Pure. The phone fields on the edit form are
    Number (home), CellNumber (cell), and WorkNumber (work)."""
    return form_field in ("Number", "CellNumber", "WorkNumber")


def _format_phone_for_svigg(phone: str) -> tuple[str, bool]:
    """Reformat a phone number into Svigg's required XXX-XXX-XXXX input shape.

    DIAGNOSIS (confirmed live, 2026-07-06): the Svigg edit-form phone inputs
    (Number/CellNumber/WorkNumber, maxlength="12") silently reject a raw-digit
    fill — a write of "8565551234" clicked Save with no error and the correct
    form_action, but the value NEVER persisted (verify re-read came back blank).
    The account's existing home-phone value matches ^\\d{3}-\\d{3}-\\d{4}$ exactly,
    so Svigg expects the DASHED 12-char format. Filling that shape is the fix.

    Pure, never raises. Strips ALL non-digits from the input first (so any input
    shape normalizes identically: "8565551234", "(856) 555-1234", "856.555.1234",
    "856-555-1234" -> "856-555-1234"). Then:
      * exactly 10 digits          -> "XXX-XXX-XXXX"                (reformatted)
      * 11 digits, leading '1'     -> drop the '1', format the rest (reformatted)
      * any other digit count      -> return the ORIGINAL value UNCHANGED
        (we never fabricate structure onto a number we can't confidently parse)

    Returns (value, reformatted): `value` is the string to fill; `reformatted`
    is True iff we produced the dashed shape, False iff we passed the caller's
    original value through untouched (so callers can note "couldn't reformat").
    """
    original = "" if phone is None else str(phone)
    digits = re.sub(r"\D", "", original)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}", True
    return original, False


def _identity_matches(
    req_last: str,
    req_first: str,
    req_dob: str,
    emr_name: str,
    emr_dob: str,
) -> tuple[bool, str]:
    """DETERMINISTIC identity guard for a Svigg WRITE (book/cancel): does the
    account the EMR actually resolved belong to the patient the caller asked
    to act on? Pure — string/date comparison only, NO LLM judgment, NO I/O.

    This is a safety guard, not a UX feature: it exists so an account-number
    mix-up/typo is caught and REFUSED (fail-closed) instead of silently
    booking/cancelling the wrong person's appointment. It is ADDITIVE — it
    never relaxes the allowlist, the execute flag, or the human-approval gate.

    Args:
        req_last/req_first: the name the caller/approval requested to act on.
        req_dob: the caller-side DOB if available ("" when the card/caller
            carries none — the booking path usually does not).
        emr_name: the resolved account's OWN name as the EMR reports it
            (search_patient returns "Last, First").
        emr_dob: the resolved account's OWN DOB as the EMR reports it (""/None
            when search_patient could not parse a DOB off the result row).

    Rules (fail-closed):
      * NAME (always checked): both sides are reduced to a normalized token
        bag (_name_token_bag: case-folded, punctuation-stripped, >1-char
        tokens). They MATCH iff the bags are equal OR one is a non-empty
        subset of the other (handles the known "Patients 1, Test" vs the chat
        model's "Test Patients"/"1" re-split of the SAME person, and honors a
        caller who passed last-name only). DISJOINT bags — a DIFFERENT person's
        name — is a HARD refusal. If EITHER side yields ZERO usable tokens
        (no name material to bind on), that is ALSO a refusal — an absence of
        identity material must never read as a vacuous "match".
      * DOB (checked only when BOTH sides have a parseable DOB): the two
        normalized (MMDDYYYY) values must be equal; otherwise HARD refusal.
        When a DOB is missing on EITHER side we degrade to the name-only
        check above — we never fabricate or assume a DOB, and a missing DOB
        never by itself blocks a booking (the booking path routinely has no
        caller DOB), but a name mismatch always does.

    Returns (ok: bool, reason: str). `reason` is "" on match; on a refusal it
    is a specific, honest, PHI-free-shaped message the caller surfaces as
    `identity_mismatch` (it names neither the requested nor the resolved
    patient — only which check failed — so it is safe to log/echo).
    """
    req_bag = _name_token_bag(f"{req_last or ''} {req_first or ''}")
    emr_bag = _name_token_bag(emr_name)
    if not req_bag or not emr_bag:
        return False, (
            "identity_mismatch: no usable name material to bind the write to "
            f"(requested_tokens={len(req_bag)}, account_tokens={len(emr_bag)}) "
            "— refusing to write without a confirmed name match")
    if not (req_bag == emr_bag
            or req_bag.issubset(emr_bag)
            or emr_bag.issubset(req_bag)):
        return False, (
            "identity_mismatch: requested patient name does not match the "
            "name on file for this account — refusing to act on the wrong "
            "patient's record")
    req_d = _norm_dob_for_compare(req_dob)
    emr_d = _norm_dob_for_compare(emr_dob)
    if req_d and emr_d and req_d != emr_d:
        return False, (
            "identity_mismatch: requested DOB does not match the DOB on file "
            "for this account — refusing to act on the wrong patient's record")
    return True, ""


def _build_alternatives(
    same_day_open_slots: Optional[list] = None,
    other_days: Optional[list] = None,
    overbook_available: bool = False,
    other_days_error: Optional[str] = None,
) -> dict:
    """Pure shaping of the "alternatives" block added to a refused booking
    result (a full/no-session slot with allow_overbook=False) so the
    dashboard can offer the human a real choice instead of a dead end.

    NEVER invents data: ``same_day_open_slots`` defaults to ``[]`` (honest
    empty — "none, or none readable"), ``other_days`` defaults to ``[]``
    (find_bookable_days' own honest-empty convention). ``other_days_error``
    is set ONLY when the find_bookable_days probe itself raised — it never
    replaces ``other_days`` (which stays ``[]`` in that case), it just
    documents why the probe came back empty so a human can tell "no other
    bookable days within the horizon" apart from "the probe broke".

    Args:
        same_day_open_slots: list of free-slot text labels read off the
            requested day's own one-day grid (``[]`` if none free or the
            grid couldn't be read).
        other_days: ``find_bookable_days``-shaped list of
            ``{"date_mdY": ..., "free_slots": N}`` entries for other days
            with open slots (``[]`` if none found within the horizon).
        overbook_available: True iff the Overbook submit control was
            actually detected on the response (never asserted otherwise).
        other_days_error: optional message describing why the other-days
            probe could not run/complete; omitted entirely when None.

    Returns:
        {"same_day_open_slots": [...], "other_days": [...],
         "overbook_available": bool} plus an "alternatives_error" key only
        when ``other_days_error`` was given.
    """
    alternatives = {
        "same_day_open_slots": list(same_day_open_slots or []),
        "other_days": list(other_days or []),
        "overbook_available": bool(overbook_available),
    }
    if other_days_error:
        alternatives["alternatives_error"] = other_days_error
    return alternatives


# ---------------------------------------------------------------------------
# SAME-DAY OPEN-SLOT TIME LABELS — pure helpers (fixed 2026-07-05, live bug)
# ---------------------------------------------------------------------------
# LIVE BUG (found in live testing 2026-07-05): the slot_conflict path's
# same_day_open_slots list was built from each free add.htm anchor's own
# rendered TEXT — which Svigg always renders as the literal word "Add" (see
# doctorcom-booking-contract.md §5: "the anchor text is literally `Add`").
# That made every entry in the list read "Add" (9 identical "Add" entries in
# the live incident) instead of a clock time, so the dashboard's same-day
# reschedule picker had nothing usable to show.
#
# THE FIX: the ONE-DAY grid is midnight-anchored in strict 15-minute rows —
# already live-proven and load-bearing elsewhere in this file via
# `_time_to_slot_y` (`y=40 == 10:00AM`, live-anchored 2026-07-01). That
# mapping is deterministic from the anchor's own `y=` coordinate alone, so it
# is used here in reverse instead of trying to scrape a separate "time label"
# table cell (no capture in this codebase confirms the one-day grid renders
# such a cell as separately parseable text next to each add.htm anchor — see
# doctorcom-booking-contract.md §6, which explicitly lists the slot→datetime
# resolution as unconfirmed by HAR). Deriving the label from `y=` reuses an
# already-verified fact instead of guessing at unverified markup.
def _slot_y_to_time(y: int) -> str:
    """Convert a book-grid 15-min row index (y) back to a clock-time label.

    Exact inverse of `SviggScraper._time_to_slot_y` (`y=40 == 10:00AM`,
    live-anchored 2026-07-01, midnight-anchored 15-minute rows). Output uses
    the SAME "%I:%M%p" shape (e.g. "10:00AM") that `_time_to_slot_y` accepts
    as input — so a label returned here can be fed straight back in as
    `start_time` on a restage/booking call without any reformatting.

    Raises ValueError for a negative or out-of-day (>= 24h) row index —
    callers must treat that as "unparseable", never invent a time.
    """
    if y < 0 or y >= 24 * 4:
        raise ValueError(f"row index {y!r} is outside a single 24h day (0-95)")
    minutes = y * 15
    from datetime import datetime as _dt, timedelta as _td
    t = (_dt(2000, 1, 1) + _td(minutes=minutes)).time()
    return t.strftime("%I:%M%p").lstrip("0") or "12:00AM"


def pair_open_anchors_to_times(hrefs: list, cap: int = 16) -> list:
    """Map free add.htm anchor hrefs to DEDUPED, in-order clock-time labels.

    NEVER returns anchor text (which Svigg always renders as "Add" — see the
    module comment above) and NEVER returns "Add" or any non-time text.
    Pure/offline: takes raw href strings (as read from `a[href*="add.htm?x="]`
    anchors on the one-day grid), not a live page/frame — so it is
    unit-testable without Playwright.

    Args:
        hrefs: raw `href` attribute strings, e.g. "add.htm?x=0&y=40". Anchors
            with an unparseable/missing y= are silently skipped (never
            fabricated). An open time that recurs across multiple
            rooms/columns at the same row is only added once, in the order
            first seen.
        cap: maximum number of time labels to return (default 16 — a full
            day's worth of rows is already far more choice than a picker UI
            needs).

    Returns:
        Deduped, in-order list of time-label strings (e.g. ["9:00AM",
        "9:15AM"]). Empty list if `hrefs` is empty, none carry a parseable
        y=, or anything about the mapping fails — this function never raises
        and never returns "Add" or placeholder text.
    """
    try:
        y_re = re.compile(r'add\.htm\?x=\d+&y=(\d+)')
        seen: set = set()
        out: list = []
        for href in hrefs or []:
            if not href:
                continue
            m = y_re.search(href)
            if not m:
                continue
            try:
                y = int(m.group(1))
                label = _slot_y_to_time(y)
            except (ValueError, TypeError):
                continue
            if label in seen:
                continue
            seen.add(label)
            out.append(label)
            if len(out) >= cap:
                break
        return out
    except Exception as exc:  # noqa: BLE001 — never let a parse bug surface "Add"
        logger.warning("pair_open_anchors_to_times: mapping failed (%s); "
                        "returning [] rather than guessing", exc)
        return []


# ---------------------------------------------------------------------------
# NEW-PATIENT CREATE KILL-SWITCH (safety locks — fail-closed, double-gated)
# ---------------------------------------------------------------------------
# create_patient() writes a brand-new chart to the live Svigg/Dr.Com database.
# The ENTRY path (login -> patientEntry_new.htm -> name-search de-dupe POST ->
# patientEntry_add.htm add form) AND the final SAVE POST are now HAR-confirmed
# (base websrv01.physician-to-go.net.har: the add-form Save is the single POST
# to pentry.htm with Referer patientEntry_add.htm — ~53 urlencoded params,
# submit trigger NextTab, 200 OK). BUT that is a SINGLE capture, never
# live-tested, so:
#   * DRY-RUN (dry_run=True, the DEFAULT) discovers the add form's fields LIVE
#     from the rendered DOM and returns what it WOULD submit — it NEVER clicks
#     Save, creating nothing.
#   * The SAVE is wired but comes from ONE unverified capture. Committing is
#     gated behind TWO independent, fail-closed locks so a real Save is never
#     left open in source:
#       1. CREATE_EXECUTE_ENABLED — env SVIGG_CREATE_EXECUTE must be truthy.
#       2. the caller must pass dry_run=False AND confirm_unverified=True.
#     Even with both open the method still refuses unless it can positively
#     identify the exact HAR-confirmed NextTab submit control (never guesses a
#     target), and it NEVER claims success from an HTTP 200 — it re-searches
#     for the patient after saving and reports created_verified ONLY on a
#     positive re-read, else created_unverified. First live use MUST be
#     supervised on a test account.
CREATE_EXECUTE_ENABLED = os.environ.get("SVIGG_CREATE_EXECUTE", "").lower() in ("1", "true", "yes")

# EDIT (demographic update) execute gate — see update_patient(). Same fail-closed
# shape as CREATE_EXECUTE_ENABLED. The EDIT save contract IS HAR-confirmed (base
# websrv01.physician-to-go.net.har: 10 edit-saves = POST pentry.htm, Referer
# pentry.htm, ~54 urlencoded params, submit trigger the image button imageField
# -> imageField.x/.y — distinct from the ADD save's NextTab/Referer
# patientEntry_add.htm). Even so an EDIT touches a REAL existing chart, so the
# commit path stays triple-gated (this env flag + dry_run=False +
# confirm_unverified=True) AND identity-guarded AND verify-after; it never
# trusts an HTTP 200. Default OFF — the owner flips SVIGG_EDIT_EXECUTE=1 only for
# the supervised first live edit on the test account.
EDIT_EXECUTE_ENABLED = os.environ.get("SVIGG_EDIT_EXECUTE", "").lower() in ("1", "true", "yes")

# Lazy import — Playwright may not be installed in all envs
_pw_module = None

def _get_pw():
    global _pw_module
    if _pw_module is None:
        from playwright.async_api import async_playwright
        _pw_module = async_playwright
    return _pw_module


def _load_env():
    """Load .env from Antigravity dir."""
    env_path = Path(os.environ.get("ANTIGRAVITY_DIR", "/Users/shubh/Documents/Antigravity")) / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


class SviggScraper:
    """Playwright-based scraper for Svigg/Dr.Com/WEBeDoctor."""

    BASE_URL = "https://websrv01.physician-to-go.net"

    def __init__(self, headless: bool = True):
        _load_env()
        self.headless = headless
        self.url = os.environ.get("WEBEDOCTOR_URL", f"{self.BASE_URL}/proxy.cgi/off/home/login.htm")
        self.username = os.environ.get("WEBEDOCTOR_USER", "")
        self.password = os.environ.get("WEBEDOCTOR_PASS", "")
        self._pw = None
        self._browser = None
        self._page = None

    async def start(self):
        """Launch browser.

        Idempotent / safe to re-call: if a browser is already running (e.g. a
        prior session that hit a transient portal error and was marked
        disconnected by emr_session_manager WITHOUT being stopped), tear it down
        first. Without this, each session-expiry reconnect overwrote
        self._pw/_browser/_page and orphaned the previous chromium process +
        context, leaking file descriptors and memory until the server could no
        longer launch browsers — and silently abandoned any half-filled write
        form on the old page (audit finding #8). SISClient.start() already guards
        this way; Svigg now matches.
        """
        if self._browser is not None or self._pw is not None:
            await self.stop()
        pw_factory = _get_pw()
        self._pw = await pw_factory().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        ctx = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        self._page = await ctx.new_page()

    async def stop(self):
        """Close browser.

        Best-effort: a close/stop on an already-dead browser can itself raise,
        but this is also the teardown path start() uses before a reconnect, so a
        failure here must NOT prevent the fresh launch or leave stale handles.
        We log and press on, always clearing the references.
        """
        if self._browser:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                logger.warning("Svigg browser close failed (ignoring): %s", exc)
        if self._pw:
            try:
                await self._pw.stop()
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                logger.warning("Svigg playwright stop failed (ignoring): %s", exc)
        self._browser = None
        self._pw = None
        self._page = None

    async def _goto(self, url: str, *, wait_until: str = "networkidle",
                    timeout: int = 15000, retries: int = 2):
        """
        Navigate with a bounded retry on transient net::ERR_ABORTED.

        The physician-to-go.net proxy intermittently aborts a navigation when
        requests arrive back-to-back on the shared page (observed ~20% of the
        time under rapid sequential load). ERR_ABORTED is transient — the page
        is still alive and an immediate re-goto succeeds. We retry only that
        specific error so genuine failures (timeout, DNS, auth) still surface.
        """
        last_exc = None
        for attempt in range(retries + 1):
            try:
                return await self._page.goto(url, wait_until=wait_until, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 — narrow on message below
                last_exc = exc
                if "ERR_ABORTED" not in str(exc) or attempt == retries:
                    raise
                logger.warning(
                    "Svigg goto ERR_ABORTED (attempt %d/%d), retrying: %s",
                    attempt + 1, retries, url,
                )
                await asyncio.sleep(0.4 * (attempt + 1))
        raise last_exc  # unreachable, kept for clarity

    async def login(self) -> bool:
        """Log into Svigg. Returns True on success."""
        if not self.username or not self.password:
            raise ValueError("WEBEDOCTOR_USER and WEBEDOCTOR_PASS must be set in .env")

        await self._goto(self.url, wait_until="networkidle", timeout=20000)

        # The login page is a frameset. Check if we're already logged in
        # by looking for the main menu frame.
        title = await self._page.title()
        if "Sunny Vigg" in title or "Atlantic" in title:
            # Check if there's a "Main Menu" or similar logged-in indicator
            # by navigating to a known authenticated page
            await self._goto(
                f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry.htm",
                wait_until="networkidle", timeout=15000,
            )
            test_title = await self._page.title()
            if "Patient Entry" in test_title:
                logger.info("Already logged in to Svigg")
                return True

        # Not logged in — try to find login form in frames
        # The login form is likely in the mainFrame of the frameset
        frames = self._page.frames
        login_frame = None
        for frame in frames:
            try:
                user_input = await frame.query_selector('input[type="text"]')
                pass_input = await frame.query_selector('input[type="password"]')
                if user_input and pass_input:
                    login_frame = frame
                    break
            except Exception:
                continue

        if not login_frame:
            # Try the main page itself
            user_input = await self._page.query_selector('input[type="text"]')
            pass_input = await self._page.query_selector('input[type="password"]')
            if user_input and pass_input:
                login_frame = self._page
            else:
                logger.error("Could not find login form in any frame")
                return False

        # Fill credentials and submit
        await login_frame.fill('input[type="text"]', self.username)
        await login_frame.fill('input[type="password"]', self.password)

        # Find and click submit button
        submit = await login_frame.query_selector(
            'input[type="submit"], input[type="image"], button[type="submit"]'
        )
        if submit:
            await submit.click()
        else:
            await login_frame.press('input[type="password"]', 'Enter')

        # Wait for navigation
        await self._page.wait_for_load_state("networkidle", timeout=15000)

        # Verify login succeeded
        await self._goto(
            f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry.htm",
            wait_until="networkidle", timeout=15000,
        )
        title = await self._page.title()
        if "Patient Entry" in title:
            logger.info("Svigg login successful")
            return True

        logger.error(f"Svigg login failed — page title: {title}")
        return False

    async def search_patient(
        self, last_name: str = "", first_name: str = "", acct: str = "",
    ) -> list[dict]:
        """
        Search for patients by name (or, when `acct` is given, by account
        number — added 2026-07-05 so resolve_patient can search acct-first;
        see book_appointment's resolve_patient stage). Returns list of
        patient dicts. Each dict has: name, acct, rowid, dob, gender, phone,
        address, insurance_class, insurance_carrier, summary_url,
        display_url.

        Acct search is DEFENSIVE: patientEntry.htm's rendered search form is
        only known (HAR-verified) to expose LastName/FirstName. We probe for
        an account-number input by a candidate name list and fill it ONLY if
        the DOM actually has one — we never invent a field or a different
        endpoint. If no such field exists, the caller gets an honest 0-match
        result rather than a silently-wrong name search.
        """
        page = self._page

        await self._goto(
            f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry.htm",
            wait_until="networkidle", timeout=15000,
        )

        used_acct_field = False
        if acct:
            # Candidate field-name aliases, same defensive-discovery pattern
            # used by create_patient's alias_map (never invent a target).
            # "Number" is FIRST — HAR-verified (2026-07-05, office capture of
            # the real pentry.htm search form) as the actual account-number
            # input on this form; the others are speculative fallbacks kept
            # in case a different Svigg build renders differently. None of
            # them being present is safe: used_acct_field stays False and we
            # fall through to name search below.
            for cand in ("Number", "Account", "Chart", "Acct", "AcctNo", "acct"):
                el = await page.query_selector(f'input[name="{cand}"]')
                if el:
                    await page.fill(f'input[name="{cand}"]', acct)
                    used_acct_field = True
                    break

        if not used_acct_field:
            # Fall back to (or supplement with) name fields — this is the
            # only path when acct was empty, or when the form has no
            # acct-capable input at all.
            if last_name:
                await page.fill('input[name="LastName"]', last_name)
            if first_name:
                await page.fill('input[name="FirstName"]', first_name)

        # Click the NameSearch button
        await page.click('input[name="NameSearch"]')
        await page.wait_for_load_state("networkidle", timeout=15000)

        # Parse results
        title = await page.title()
        if "Patient Entry" in title and "View" not in title:
            # No results — still on search form, or form validation error
            return []

        results = []

        # Find all patient name links (they link to pentry.htm with rowid and acct)
        patient_links = await page.query_selector_all('a[href*="pentry.htm?rowid="]')

        for link in patient_links:
            href = await link.get_attribute("href") or ""
            name = (await link.inner_text()).strip()

            # Extract rowid and acct from href
            rowid = ""
            acct = ""
            if "rowid=" in href:
                rowid_match = re.search(r'rowid=([^&|]+)', href)
                if rowid_match:
                    rowid = rowid_match.group(1)
            if "acct=" in href:
                acct_match = re.search(r'acct=([^&|]+)', href)
                if acct_match:
                    acct = acct_match.group(1)

            # Get the parent row to extract more details
            # The result row is in a table structure
            row_el = await link.evaluate_handle(
                """el => {
                    // Walk up to find the containing table row or cell
                    let parent = el.closest('tr') || el.parentElement?.parentElement;
                    return parent;
                }"""
            )

            row_text = ""
            try:
                row_text = await row_el.inner_text()
            except Exception:
                pass

            # Parse DOB from row text
            dob_match = re.search(r'DOB:\s*(\d{2}/\d{2}/\d{4})', row_text)
            dob = dob_match.group(1) if dob_match else ""

            # Parse gender
            gender = ""
            if "Male" in row_text:
                gender = "Male"
            elif "Female" in row_text:
                gender = "Female"

            # Parse phone
            phone_match = re.search(r'(?:Cell|Home|Work):\s*([\d\-\(\)\s]+)', row_text)
            phone = phone_match.group(1).strip() if phone_match else ""

            result = {
                "name": name,
                "acct": acct,
                "rowid": rowid,
                "dob": dob,
                "gender": gender,
                "phone": phone,
                "raw_text": row_text[:500],  # truncated for cache
            }
            results.append(result)

        return results

    async def get_patient_summary(self, rowid: str, acct: str) -> dict:
        """
        Navigate to the Patient Summary page and parse all fields.
        Returns a dict with demographics, insurance, cases, contacts, etc.
        """
        page = self._page

        url = (
            f"{self.BASE_URL}/proxy.cgi/off/maint/pentrySummary.htm?"
            f"srchpg=/proxy.cgi/off/maint/patientEntry.htm"
            f"&title=Patient+Entry"
            f"&rowid={rowid}"
            f"&acct={acct}"
            f"&cb=/proxy.cgi/off/maint/pentry.htm|rowid={rowid}|acct={acct}"
        )

        await self._goto(url, wait_until="networkidle", timeout=15000)

        # The summary page renders as plain text — extract everything
        text = await page.inner_text("body")

        return self._parse_summary_text(text, acct, rowid)

    def _parse_summary_text(self, text: str, acct: str, rowid: str) -> dict:
        """Parse the raw text from the Patient Summary page into structured data."""

        result: dict = {
            "acct": acct,
            "rowid": rowid,
            "source": "svigg_live",
        }

        full_text = text

        # Name — first line after "Patient Summary - "
        name_match = re.search(r'Patient Summary - (.+)', full_text)
        if name_match:
            result["name"] = name_match.group(1).strip()

        # Account
        acct_match = re.search(r'Acct #:\s*(\S+)', full_text)
        if acct_match:
            result["acct"] = acct_match.group(1)

        # SSN
        ssn_match = re.search(r'Social:\s*(\S+)', full_text)
        if ssn_match:
            result["ssn"] = ssn_match.group(1)

        # DOB
        dob_match = re.search(r'DOB:\s*(\d{2}/\d{2}/\d{4})', full_text)
        if dob_match:
            result["dob"] = dob_match.group(1)

        # Age
        age_match = re.search(r'Age:\s*(\d+y\s*\d*m?)', full_text)
        if age_match:
            result["age"] = age_match.group(1).strip()

        # Gender
        gender_match = re.search(r'Gender:\s*(\w+)', full_text)
        if gender_match:
            result["gender"] = gender_match.group(1)

        # Chart number
        chart_match = re.search(r'Chart:\s*(\S+)', full_text)
        if chart_match and chart_match.group(1) != "Sig.":
            result["chart"] = chart_match.group(1)

        # Address — multi-line, after "Address" label
        addr_match = re.search(
            r'Address\s*\n\s*(.+?)\n\s*(.+?,\s*[A-Z]{2}\s*\d{5})',
            full_text
        )
        if addr_match:
            result["address"] = f"{addr_match.group(1).strip()}, {addr_match.group(2).strip()}"

        # Phone numbers
        phones: dict = {}
        for phone_match in re.finditer(r'(Cell|Home|Work|Fax)\s+(\d[\d\-\(\)\s]+)', full_text):
            phones[phone_match.group(1).lower()] = phone_match.group(2).strip()
        if phones:
            result["phones"] = phones

        # Email
        email_match = re.search(r'Email Address\s*\n?\s*(\S+@\S+)', full_text)
        if email_match:
            result["email"] = email_match.group(1)

        # Primary Office
        office_match = re.search(r'Primary Office:\s*(.+)', full_text)
        if office_match:
            result["primary_office"] = office_match.group(1).strip()

        # Referring Provider
        ref_match = re.search(r'Referring Provider:\s*(.+)', full_text)
        if ref_match and ref_match.group(1).strip():
            result["referring_provider"] = ref_match.group(1).strip()

        # Provider
        prov_match = re.search(r'Provider:\s*(.+)', full_text)
        if prov_match:
            result["provider"] = prov_match.group(1).strip().rstrip(",")

        # Class (insurance type)
        class_match = re.search(r'Class:\s*(.+)', full_text)
        if class_match:
            result["insurance_class"] = class_match.group(1).strip()

        # Insurance carrier
        # Look for the insurance section — carrier name is typically after "Insurance" header
        # and before "Accept Assignment"
        carrier_match = re.search(
            r'Insurance Carrier.*?\n(.+?)(?:\n.*?Accept Assignment)',
            full_text, re.DOTALL
        )
        if carrier_match:
            carrier_lines = [l.strip() for l in carrier_match.group(1).strip().split("\n") if l.strip()]
            if carrier_lines:
                result["insurance_carrier"] = carrier_lines[0]

        # Policy number
        policy_match = re.search(r'Policy #:\s*(\S+)', full_text)
        if policy_match:
            result["policy_number"] = policy_match.group(1)

        # Group number
        group_match = re.search(r'Group #:\s*(\S+)', full_text)
        if group_match:
            result["group_number"] = group_match.group(1)

        # Copay
        copay_match = re.search(r'Copay:\s*([\d\.]+)', full_text)
        if copay_match:
            result["copay"] = copay_match.group(1)

        # Case info
        cases = []
        for case_match in re.finditer(r'(WC|Standard|Auto|PIP|Lien)\s+(\d{2}/\d{2}/\d{4})', full_text):
            cases.append({
                "type": case_match.group(1),
                "date": case_match.group(2),
            })
        if cases:
            result["cases"] = cases

        # Responsible party
        resp_match = re.search(r'Responsible:\s*(.+)', full_text)
        if resp_match:
            result["responsible_party"] = resp_match.group(1).strip()

        return result

    async def get_patient_ledger(self, account_number: str, rowid: str = "") -> dict:
        """
        Navigate to the visit-entry ledger for a patient and parse it with BeautifulSoup.

        Discovery (re-verified live 2026-06-29):
          URL: /proxy.cgi/apps/ven/elist.htm?acct={acct}&rowid={rowid}

          *** rowid is REQUIRED to reach the ledger. *** The 2026-06-28 note
          that "Svigg handles a blank rowid" is WRONG: with acct-only the portal
          serves the Visit-Entry SEARCH FORM (header "Visit Entry", no charges
          table), so the parser silently found nothing and returned zero charges.
          That was the 2026-06-29 regression. Pass the rowid harvested from
          search_patient() (pentry.htm?rowid=...&acct=...) to land on the actual
          ledger. If no rowid is supplied we still issue the request (and log a
          warning) — useful for the legacy/diagnostic path — but expect zero
          charges because the portal will show the search form.

        Real ledger structure (acct+rowid, observed 2026-06-29 for a live acct):
          - Section banners are <th> cells: "Visit Entry -<name>", "Batch Review",
            "Bill Review". The CHARGES live under "Batch Review".
          - The charges COLUMN HEADER is a <td> row (NOT <th>) reading:
              Batch # | Visit Date | Tran Date | Bill Office | Treat Office |
              Provider | Skip Vst Tab | Conv? | Expected | Entered | (action)
          - Data rows are <td> rows under that header. A degenerate
            "No Batch Control" row (8 cells) may appear and is skipped.
          - A separate "Bill Review" section follows with its own <td> header
            (Bill | Visit Date | Incident | ...). We STOP at that banner so bill
            rows are not misparsed as charges.

        Parsing is HEADER-DRIVEN: we locate the charges header row by its column
        names, build a {column_name: index} map, and read every field by NAME
        (with synonyms + a legacy positional fallback). The header text is logged
        so a future column-layout change is diagnosable from logs alone.

        Payments + a computed balance come from a SEPARATE two-step report,
        /proxy.cgi/off/reports/ledgerRptcases.htm (NOT the dead /apps/pay/ root,
        which returns Svigg's 'Sorry' page). See _fetch_payments() for the form
        fields and the rendered RetrieveReport table layout. Payments are fetched
        after charges are parsed, so a payments failure never regresses charges.

        Returns:
          {
            "charges": [{"batch": ..., "visit_date": ..., "tran_date": ...,
                         "provider": ..., "expected": ..., "entered": ...}, ...],
            "payments": [...], # patient/copay payments + adjustments (ledgerRptcases.htm)
            "balance": <float>, # total charges - total payments/adjustments (or None)
            "count": N,
            "account_number": "...",
            "source": "svigg_live",
            "note": "...",
          }
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {
                "account_number": account_number,
                "error": "BeautifulSoup (beautifulsoup4) not installed",
                "charges": [],
                "payments": [],
                "balance": None,
                "source": "svigg_live",
            }

        page = self._page
        url = f"{self.BASE_URL}/proxy.cgi/apps/ven/elist.htm?acct={account_number}"
        if rowid:
            url += f"&rowid={rowid}"
        else:
            # acct-only lands on the Visit-Entry search form, not the ledger.
            # We still issue the request, but warn so a caller that forgot to
            # plumb the rowid through can see why charges came back empty.
            logger.warning(
                "Svigg get_patient_ledger called WITHOUT rowid (acct=%s); the "
                "portal will serve the search form, not the charges ledger. "
                "Pass the rowid from search_patient() to get charges.",
                account_number,
            )
        await self._goto(url, wait_until="networkidle", timeout=15000)

        # Check for the 'Sorry' page (Svigg's generic 'not found / no session' page)
        body_text = await page.inner_text("body")
        if len(body_text.strip()) < 100 and "sorry" in body_text.lower():
            return {
                "account_number": account_number,
                "error": "Svigg returned 'Sorry' — account may not exist or session expired",
                "charges": [],
                "payments": [],
                "balance": None,
                "source": "svigg_live",
            }

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        charges = []

        def _norm(text: str) -> str:
            """Lowercase, strip nbsp/punctuation, collapse whitespace."""
            text = text.replace("\xa0", " ")
            return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

        # --- 1. Locate the charges COLUMN-HEADER row --------------------------
        # The header is a <td> row (NOT <th>) whose cells read
        #   Batch # | Visit Date | Tran Date | Bill Office | Treat Office |
        #   Provider | Skip Vst Tab | Conv? | Expected | Entered | (action)
        # We scan every <tr> in the document and pick the row that looks like
        # this header (contains a 'batch' cell AND a 'visit date' cell). That
        # row anchors both the column map and where the data rows begin.
        all_rows = soup.find_all("tr")
        header_row = None
        header_idx_in_doc = -1
        header_cells = []
        for ri, row in enumerate(all_rows):
            cell_texts = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            norm_texts = [_norm(t) for t in cell_texts]
            has_batch = any(t.startswith("batch") for t in norm_texts)
            has_visit = any("visit date" in t for t in norm_texts)
            if has_batch and has_visit:
                header_row = row
                header_idx_in_doc = ri
                header_cells = cell_texts
                break

        # Log the raw header text so a future column-layout change is diagnosable
        # from logs alone (the 2026-06-29 regression returned zero charges silently).
        logger.info(
            "Svigg ledger acct=%s rowid=%s charges-header columns: %r",
            account_number, rowid or "(none)", header_cells,
        )

        # --- 2. Build {normalized_header: index} and resolve fields by NAME ---
        col_index = {}
        for i, h in enumerate(header_cells):
            key = _norm(h)
            if key and key not in col_index:
                col_index[key] = i

        def _find_col(*candidates) -> Optional[int]:
            """Index of the first header matching any candidate.

            Each candidate is matched exactly first, then as a substring, so
            'expected' matches '$ Expected' / 'Amt Expected' / 'Expected'.
            """
            for cand in candidates:
                cn = _norm(cand)
                if cn in col_index:
                    return col_index[cn]
            for cand in candidates:
                cn = _norm(cand)
                for key, idx in col_index.items():
                    if cn and cn in key:
                        return idx
            return None

        idx_batch = _find_col("batch #", "batch", "batch number")
        idx_visit = _find_col("visit date", "visit", "service date", "dos")
        idx_tran = _find_col("tran date", "transaction date", "tran")
        idx_bill_office = _find_col("bill office", "billing office")
        idx_treat_office = _find_col("treat office", "treating office", "treatment office")
        idx_provider = _find_col("provider", "rendering provider")
        idx_expected = _find_col("expected", "$ expected", "amt expected", "amount expected")
        idx_entered = _find_col("entered", "$ entered", "amt entered", "amount entered")

        # Legacy positional fallback (only if a named header is missing), from the
        # layout observed 2026-06-29: Batch#=0, Visit=1, Tran=2, BillOff=3,
        # TreatOff=4, Provider=5, Skip=6, Conv=7, Expected=8, Entered=9.
        FALLBACK = {
            "batch": 0, "visit": 1, "tran": 2, "bill_office": 3,
            "treat_office": 4, "provider": 5, "expected": 8, "entered": 9,
        }
        idx_batch = idx_batch if idx_batch is not None else FALLBACK["batch"]
        idx_visit = idx_visit if idx_visit is not None else FALLBACK["visit"]
        idx_tran = idx_tran if idx_tran is not None else FALLBACK["tran"]
        idx_bill_office = idx_bill_office if idx_bill_office is not None else FALLBACK["bill_office"]
        idx_treat_office = idx_treat_office if idx_treat_office is not None else FALLBACK["treat_office"]
        idx_provider = idx_provider if idx_provider is not None else FALLBACK["provider"]
        idx_expected = idx_expected if idx_expected is not None else FALLBACK["expected"]
        idx_entered = idx_entered if idx_entered is not None else FALLBACK["entered"]

        def _cell(cells, idx) -> str:
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].get_text(strip=True).replace("\xa0", " ").strip()

        # --- 3. Walk data rows AFTER the header, STOP at the next section ------
        # The "Bill Review" section that follows has its own <td> header
        # (Bill | Visit Date | Incident | ...) — we must not parse those rows as
        # charges. We stop as soon as we hit a row that is a new section banner
        # ('bill review' / 'payment') or a different column header ('incident').
        if header_row is not None:
            # The maximum named index we read; a real charge row must be ≥ this.
            min_width = max(idx_batch, idx_visit, idx_tran, idx_provider) + 1
            for row in all_rows[header_idx_in_doc + 1:]:
                row_text = _norm(row.get_text(" ", strip=True))
                # Stop at the start of the next section (bill review / payments).
                if "bill review" in row_text or row_text.startswith("payment"):
                    break
                # A <th> here is a section banner — skip (don't treat as data).
                if row.find("th") and not row.find("td"):
                    continue
                # Another column-header row (e.g. the Bill table header) → stop.
                if "incident" in row_text and "visit date" in row_text:
                    break
                cells = row.find_all("td")
                if len(cells) < min_width:
                    continue
                batch_cell = cells[idx_batch] if idx_batch < len(cells) else None
                if batch_cell is None:
                    continue
                batch_link = batch_cell.find("a")
                batch_num = (
                    batch_link.get_text(strip=True)
                    if batch_link else batch_cell.get_text(strip=True)
                )
                batch_num = batch_num.replace("\xa0", " ").strip()
                # A real charge row carries a numeric batch number. The
                # degenerate "No Batch Control" placeholder row has batch '0'
                # with a 'No Batch Control' provider — drop non-numeric and the
                # zero placeholder, keep real batches.
                if not batch_num or not batch_num.isdigit() or batch_num == "0":
                    continue
                charges.append({
                    "batch": batch_num,
                    "visit_date": _cell(cells, idx_visit),
                    "tran_date": _cell(cells, idx_tran),
                    "bill_office": _cell(cells, idx_bill_office),
                    "treat_office": _cell(cells, idx_treat_office),
                    "provider": _cell(cells, idx_provider),
                    "expected": _cell(cells, idx_expected),
                    "entered": _cell(cells, idx_entered),
                })

        # --- 4. Payments + computed balance via the ledgerRptcases.htm report --
        # The charges above came from apps/ven/elist.htm. Payments live in a
        # separate two-step report (ledgerRptcases.htm → Execute Report). We
        # fetch them AFTER charges are already in hand, so a payments failure
        # can never regress the charges output.
        pay = await self._fetch_payments(account_number)

        return {
            "account_number": account_number,
            "charges": charges,
            "payments": pay.get("payments", []),
            "balance": pay.get("balance"),
            "count": len(charges),
            "source": "svigg_live",
            "columns": header_cells,
            "payments_columns": pay.get("payments_header", []),
            "note": (
                "Charges from apps/ven/elist.htm (acct+rowid). Payments + computed "
                "balance from the ledgerRptcases.htm report "
                "(/proxy.cgi/off/reports/ledgerRptcases.htm?acct=...): check "
                "InclFP+ShowCopayAdj+AllVisits, Execute Report, then parse the "
                "RetrieveReport.htm ReportBody table (Bill|Service|Procedure|"
                "Description|Diag|Charge). balance = total charges - total "
                "payments/adjustments. The old /apps/pay/ path is a dead app root "
                "(returns Svigg's 'Sorry' page) and is NOT used."
            ),
        }

    async def _fetch_payments(self, account_number: str) -> dict:
        """Fetch payments + computed balance via the ledgerRptcases.htm report.

        Two-step report (discovered live 2026-06-29):
          1. GET  /proxy.cgi/off/reports/ledgerRptcases.htm?acct={acct}
             — a parameter FORM. Check InclFP (Include Fully Paid — required to
             print copays/payments), ShowCopayAdj, AllVisits, selCase1, selCase2.
             Do NOT check ShowPtntPaym / ShowCopayOnly together: they are
             mutually-exclusive restrictive filters ("Please Check Off Only One").
          2. Submit input[name="Submit"] ("Execute Report"). The response is a
             FRAMESET (<title>Navigator</title>) whose <frame name="ReportBody">
             src is /proxy.cgi/apps/event/RetrieveReport.htm?dt=..&tm=..&pg=0
             (dt/tm are a server-generated report id). Navigate directly to that
             src to read the rendered transaction table.

        Results table (pg=0) — <th> header row:
          "" | Bill | Service | Procedure | Description | Diag | Charge |
          OrigPlan/A | LastPlanBilled/A
        Data <td> rows are charge lines; "Bill Balance" rows are per-bill
        subtotals (skipped). Payment/copay/adjustment rows carry a payment
        keyword in Description and/or a negative/parenthesized amount.

        Returns {"payments": [...], "balance": float|None,
                 "payments_header": [...], "payment_total": float,
                 "charge_total_from_report": float}. Never raises — on an
        unexpected shape it logs a warning and returns empty payments / None
        balance so the charges output is never put at risk.
        """
        EMPTY = {"payments": [], "balance": None, "payments_header": []}
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return EMPTY

        page = self._page

        def _norm(text: str) -> str:
            text = (text or "").replace("\xa0", " ")
            return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

        def _money_or_none(text: str):
            """Strict money parse: returns a signed float, or None if the cell is
            not a single money number. Used to locate the amount column robustly
            (see _amount_of) because on this report the amount cell SHIFTS left by
            one on adjustment/writeoff rows (8 cells) vs charge rows (9 cells), so
            a fixed column index reads the wrong cell. Multi-token cells (e.g. a
            Diag list "722.52 724.8 ...") and plan cells ("Aetna/Y") return None."""
            t = (text or "").replace("\xa0", " ").strip()
            if not t:
                return None
            neg = t.startswith("(") and t.endswith(")")
            t = t.strip("()").replace(",", "").replace("$", "").strip()
            if t.startswith("-"):
                neg = True
                t = t[1:]
            if not re.fullmatch(r"\d+(?:\.\d+)?", t):
                return None
            val = float(t)
            return -val if neg else val

        def _money(text: str) -> float:
            val = _money_or_none(text)
            return val if val is not None else 0.0

        def _amount_of(cell_texts):
            """Return (signed_amount, raw_str) reading the RIGHTMOST cleanly
            money-parseable cell of the row. The transaction amount is always the
            last numeric field before the (non-numeric) plan/blank trailing cells,
            so this is stable across the 8-cell and 9-cell row shapes that a fixed
            idx_charge gets wrong."""
            for c in reversed(cell_texts):
                v = _money_or_none(c)
                if v is not None:
                    return v, c
            return 0.0, ""

        try:
            form_url = (
                f"{self.BASE_URL}/proxy.cgi/off/reports/ledgerRptcases.htm"
                f"?acct={account_number}"
            )
            await self._goto(form_url, wait_until="networkidle", timeout=20000)

            # Set filters to include payments + copays + fully-paid bills.
            for box in ("InclFP", "ShowCopayAdj", "AllVisits", "selCase1", "selCase2"):
                try:
                    el = await page.query_selector(f'input[name="{box}"]')
                    if el and not await el.is_checked():
                        await el.check()
                except Exception:  # noqa: BLE001 — best-effort filter set
                    pass

            submit = await page.query_selector('input[name="Submit"], input[type="submit"]')
            if submit is None:
                logger.warning(
                    "Svigg payments: Execute-Report submit not found (acct=%s)",
                    account_number,
                )
                return EMPTY
            try:
                async with page.expect_navigation(wait_until="networkidle", timeout=25000):
                    await submit.click()
            except Exception as exc:  # noqa: BLE001 — navigation may already be done
                logger.warning("Svigg payments: submit navigation note: %s", exc)

            # The Execute returns a frameset; the real table is in ReportBody.
            frameset_html = await page.content()
            m = re.search(r'name="ReportBody"\s+src="([^"]+)"', frameset_html)
            if not m:
                logger.warning(
                    "Svigg payments: ReportBody frame not found after Execute "
                    "(acct=%s) — report may have bounced to the form (filter "
                    "validation). No payments parsed.",
                    account_number,
                )
                return EMPTY
            body_src = m.group(1).replace("&amp;", "&")
            body_url = body_src if body_src.startswith("http") else f"{self.BASE_URL}{body_src}"
            await self._goto(body_url, wait_until="networkidle", timeout=20000)

            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            # --- Locate the transaction header row (Bill + Procedure + Charge) --
            all_rows = soup.find_all("tr")
            header_idx = -1
            header_cells = []
            for ri, row in enumerate(all_rows):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                norm = [_norm(c) for c in cells]
                if (any(c == "bill" for c in norm)
                        and any("procedure" in c for c in norm)
                        and any("charge" in c for c in norm)):
                    header_idx = ri
                    header_cells = cells
                    break

            logger.info(
                "Svigg payments acct=%s report-header columns: %r",
                account_number, header_cells,
            )
            if header_idx < 0:
                logger.warning(
                    "Svigg payments: transaction header row not found (acct=%s); "
                    "table shape unexpected — returning no payments.",
                    account_number,
                )
                return EMPTY

            col_index = {}
            for i, h in enumerate(header_cells):
                key = _norm(h)
                if key and key not in col_index:
                    col_index[key] = i

            def _find_col(*cands):
                for cand in cands:
                    cn = _norm(cand)
                    if cn in col_index:
                        return col_index[cn]
                for cand in cands:
                    cn = _norm(cand)
                    for key, idx in col_index.items():
                        if cn and cn in key:
                            return idx
                return None

            idx_bill = _find_col("bill")
            idx_service = _find_col("service", "service date", "dos")
            idx_proc = _find_col("procedure", "cpt")
            idx_desc = _find_col("description", "desc")
            idx_diag = _find_col("diag", "diagnosis")
            idx_charge = _find_col("charge", "amount", "amt")

            FB = {"bill": 1, "service": 2, "proc": 3, "desc": 4, "diag": 5, "charge": 6}
            idx_bill = idx_bill if idx_bill is not None else FB["bill"]
            idx_service = idx_service if idx_service is not None else FB["service"]
            idx_proc = idx_proc if idx_proc is not None else FB["proc"]
            idx_desc = idx_desc if idx_desc is not None else FB["desc"]
            idx_diag = idx_diag if idx_diag is not None else FB["diag"]
            idx_charge = idx_charge if idx_charge is not None else FB["charge"]

            def _cell(cells, idx):
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx].get_text(strip=True).replace("\xa0", " ").strip()

            PAY_KEYWORDS = (
                "payment", "copay", "adjustment", "adjust", "write off",
                "writeoff", "refund", "credit", "insurance pmt", "pt pmt",
            )

            payments = []
            total_charges = 0.0
            total_payments = 0.0
            for row in all_rows[header_idx + 1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                texts = [
                    c.get_text(strip=True).replace("\xa0", " ").strip()
                    for c in cells
                ]
                desc = _cell(cells, idx_desc)
                ndesc = _norm(desc)
                row_norm = _norm(" ".join(texts))
                # Skip per-bill subtotal rows and fully blank rows.
                if "bill balance" in row_norm:
                    continue
                proc = _cell(cells, idx_proc)
                # Amount is the rightmost money cell — NOT a fixed idx_charge,
                # which mis-reads the shifted adjustment/writeoff rows (was
                # returning $0 for every deduction → balance never net of
                # payments). amt is SIGNED: charges +, deductions -.
                amt, amount_str = _amount_of(texts)
                if not desc and not proc and amt == 0.0:
                    continue
                # A row is a payment/adjustment/writeoff (a deduction) if it
                # carries a pay keyword OR its amount is negative. Everything
                # else with a positive amount is a charge.
                is_payment = (
                    any(k in ndesc for k in PAY_KEYWORDS) or amt < 0
                )
                if is_payment:
                    total_payments += abs(amt)
                    payments.append({
                        "date": _cell(cells, idx_service),
                        "type": desc,
                        "code": proc,
                        "amount": amount_str,
                        "bill": _cell(cells, idx_bill),
                    })
                elif amt > 0:
                    total_charges += amt

            # Balance = signed net of every transaction amount = charges minus
            # the absolute value of all deductions.
            balance = round(total_charges - total_payments, 2)
            return {
                "payments": payments,
                "balance": balance,
                "payments_header": header_cells,
                "payment_total": round(total_payments, 2),
                "charge_total_from_report": round(total_charges, 2),
            }
        except Exception as exc:  # noqa: BLE001 — never put charges at risk
            logger.warning(
                "Svigg payments fetch failed (acct=%s): %s — returning no "
                "payments / null balance.",
                account_number, exc,
            )
            return EMPTY

    async def get_patient_bills_fast(self, account_number: str, rowid: str) -> dict:
        """FAST, ADD-ONLY billing read via the pdisplay* endpoint family.

        *** NOT the system-of-record. *** The authoritative net balance remains
        get_patient_ledger()/_fetch_payments() (the ledgerRptcases.htm report),
        which verify_all_v2.py checks. This method reads the cleaner
        pdisplayBilling1.htm data frame directly (no frameset, no two-step report)
        and is ~7-8x faster (measured 2026-07-01: ~1.0s vs ~8.4s wall-clock on the
        three baseline accts). Use it for a quick balance/aging glance or as an
        A/B/diagnostic cross-check — NOT to replace the verified ledger.

        DISCREPANCY (measured live 2026-07-01, A/B on the 3 baseline accts):
          acct 2086507  -> pdisplay Balance col = 0.00   ; ledgerRptcases net = 324.0  (MISMATCH)
          acct 21832702 -> pdisplay Balance col = 8447.00 ; ledgerRptcases net = 8447.0 (match)
          acct 18866600 -> pdisplay Balance col = 0.00   ; ledgerRptcases net = 0.0    (match)
        The two Svigg reports scope charges differently (pdisplay shows full
        account history with an EMR-computed Balance column; ledgerRptcases sums a
        filtered case view). Because they DISAGREE on 2086507, this fast source is
        NOT promoted to system-of-record. Callers must treat `balance` here as the
        EMR's own Balance-column figure, distinct from the verified ledger net.

        Flow (needs rowid — reuse the one from search_patient(); no session token
        needed, the plain /proxy.cgi/apps/pdisplay/... path is cookie-authed):
          GET /proxy.cgi/apps/pdisplay/pdisplayBilling1.htm
              ?rowid={rowid}&acct={acct}&caseno=0&sortcol=Date&direction=down
              &billFilterFrom=&billFilterTo=&ProvFilter=
          The response is a single data table. Its column header row is:
            Date | Description | Office | Provider | TrOffice | Balance | PtntBal |
            InsBal | Charges | InsPaid | GuarPaid | Collections | Adjusted | Incident
          A final <tr> whose first non-empty cell is 'TOTALS' carries the column
          sums. We read that TOTALS row, header-driven (map column name -> index),
          and expose Balance/Charges/InsPaid/GuarPaid/Collections/Adjusted.

        Returns (never raises — on any unexpected shape returns error + null figures
        so it can never regress the authoritative path):
          {
            "account_number": ..., "rowid": ...,
            "balance": <float or None>,          # pdisplay Balance column (NOT the verified net)
            "charges_total": <float or None>,
            "ins_paid": ..., "guar_paid": ..., "collections": ..., "adjusted": ...,
            "totals_columns": {name: value, ...},
            "source": "svigg_pdisplay_fast",
            "authoritative": False,
            "note": "...",
          }
        """
        NULL = {
            "account_number": account_number,
            "rowid": rowid,
            "balance": None,
            "charges_total": None,
            "ins_paid": None,
            "guar_paid": None,
            "collections": None,
            "adjusted": None,
            "totals_columns": {},
            "source": "svigg_pdisplay_fast",
            "authoritative": False,
        }
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {**NULL, "error": "BeautifulSoup (beautifulsoup4) not installed"}

        if not rowid:
            return {**NULL, "error": "rowid is required for the pdisplay fast path"}

        def _money(text):
            t = (text or "").replace("\xa0", " ").strip()
            if not t:
                return None
            neg = t.startswith("(") and t.endswith(")")
            t = t.strip("()").replace(",", "").replace("$", "").strip()
            if t.startswith("-"):
                neg = True
                t = t[1:]
            if not re.fullmatch(r"\d+(?:\.\d+)?", t):
                return None
            val = float(t)
            return -val if neg else val

        def _norm(text):
            text = (text or "").replace("\xa0", " ")
            return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

        page = self._page
        url = (
            f"{self.BASE_URL}/proxy.cgi/apps/pdisplay/pdisplayBilling1.htm"
            f"?rowid={rowid}&acct={account_number}&caseno=0&sortcol=Date"
            f"&direction=down&billFilterFrom=&billFilterTo=&ProvFilter="
        )
        try:
            await self._goto(url, wait_until="networkidle", timeout=20000)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Locate the column-header row (has 'balance' AND 'charges' AND
            # 'adjusted' cells). Skip the degenerate mega-row where the whole
            # table collapses into one cell (its cell count is huge but the
            # individual header cells are still separately present in a normal
            # sibling row — we require a plausible width 10..20).
            header_cells = []
            for row in soup.find_all("tr"):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not (10 <= len(cells) <= 20):
                    continue
                norm = [_norm(c) for c in cells]
                if ("balance" in norm and "charges" in norm and "adjusted" in norm):
                    header_cells = cells
                    break
            if not header_cells:
                logger.warning(
                    "Svigg fast bills: header row not found (acct=%s) — pdisplay "
                    "layout unexpected; returning null figures.", account_number)
                return {**NULL, "error": "pdisplay header row not found"}

            col_index = {}
            for i, h in enumerate(header_cells):
                key = _norm(h)
                if key and key not in col_index:
                    col_index[key] = i

            # Locate the TOTALS row: a plausibly-wide row containing a 'TOTALS'
            # cell (skip the collapsed mega-row via the same width guard).
            totals_cells = []
            for row in soup.find_all("tr"):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not (10 <= len(cells) <= 20):
                    continue
                if any(c.strip().upper() == "TOTALS" for c in cells):
                    totals_cells = cells
                    break
            if not totals_cells:
                logger.warning(
                    "Svigg fast bills: TOTALS row not found (acct=%s); returning "
                    "null figures.", account_number)
                return {**NULL, "error": "pdisplay TOTALS row not found"}

            logger.info(
                "Svigg fast bills acct=%s pdisplay header=%r totals=%r",
                account_number, header_cells, totals_cells)

            def _col(name):
                idx = col_index.get(_norm(name))
                if idx is None or idx >= len(totals_cells):
                    return None
                return _money(totals_cells[idx])

            balance = _col("balance")
            charges_total = _col("charges")
            ins_paid = _col("inspaid")
            guar_paid = _col("guarpaid")
            collections = _col("collections")
            adjusted = _col("adjusted")

            totals_columns = {}
            for name, idx in col_index.items():
                if idx < len(totals_cells):
                    v = _money(totals_cells[idx])
                    if v is not None:
                        totals_columns[name] = v

            return {
                "account_number": account_number,
                "rowid": rowid,
                "balance": balance,
                "charges_total": charges_total,
                "ins_paid": ins_paid,
                "guar_paid": guar_paid,
                "collections": collections,
                "adjusted": adjusted,
                "totals_columns": totals_columns,
                "source": "svigg_pdisplay_fast",
                "authoritative": False,
                "note": (
                    "pdisplayBilling1.htm TOTALS row. 'balance' is the EMR's own "
                    "Balance column, NOT the verified ledgerRptcases net (which is "
                    "the system-of-record via get_patient_ledger). ~7-8x faster but "
                    "disagreed with the verified net on acct 2086507 (0.00 vs 324.0) "
                    "as of 2026-07-01, so it is not authoritative."
                ),
            }
        except Exception as exc:  # noqa: BLE001 — never put the authoritative path at risk
            logger.warning(
                "Svigg fast bills failed (acct=%s): %s — returning null figures.",
                account_number, exc)
            return {**NULL, "error": f"pdisplay fast fetch failed: {exc}"}

    async def get_patient_appointments(self, account_number: str) -> list[dict]:
        """
        Get appointment history for a patient inferred from visit-ledger dates.

        Discovery (2026-06-28): No dedicated per-patient appointment endpoint
        exists in Svigg. Probed ~15 candidate URL patterns — all returned 'Sorry'.
        The patient pentry.htm frameset has ZERO appt/enc/schedule/sched launcher links.

        WORKAROUND: extract past visit dates from the ledger (ven/elist.htm) as a proxy
        for appointment history. Each ledger batch represents a booked visit with a
        visit_date, provider, and billing type.

        Returns list of {visit_date, provider, batch, source} dicts.
        """
        ledger = await self.get_patient_ledger(account_number)
        if "error" in ledger:
            return []

        appointments = []
        for charge in ledger.get("charges", []):
            visit_date = charge.get("visit_date", "")
            if not visit_date:
                continue
            appointments.append({
                "visit_date": visit_date,
                "provider": charge.get("provider", "—"),
                "batch": charge.get("batch", ""),
                "bill_office": charge.get("bill_office", ""),
                "source": "svigg_live",
                "note": "Inferred from visit ledger — Svigg has no standalone appointments endpoint",
            })
        return appointments

    async def get_schedule_day(self, day_offset: int = 0) -> dict:
        """READ-ONLY: scrape the Svigg/Doctor.com per-day 'Appointments' schedule
        report for a single day, selected by DAY OFFSET from today.

        Endpoint (verified live 2026-07-03):
          GET {BASE_URL}/proxy.cgi/off/home/appt_b.htm?todayonly=N
          where N is the integer day offset (0=today, 1=tomorrow, ...). The server
          302-redirects to the office-prefixed .../01/appt_b.htm?todayonly=N which
          returns the day's schedule table. The page shows exactly ONE calendar
          date; every appointment row carries an <a href> to appt_e.htm whose query
          holds date=MM/DD/YYYY, time=HH:MMAM|Noon, enc=NNNN, prov=SGUPTA.

        This is the SCHEDULE-VIEW report (the 'Schedule All Offices/All' the front
        desk sees), NOT the book.htm booking grid — it has a reliable per-day date
        and one row per appointment, so day counts are trustworthy. READ-ONLY: it
        only GETs appt_b.htm; it never posts to any book/cancel/commit route.

        Returns:
          {date: 'YYYY-MM-DD'|'', day_offset, count, appointments:[{...}], source}
          appointments rows: {date, start_time, atime, patient_name, provider,
                              visit_type, cpt, insurance, note, encounter_id, status}
          On failure returns {..., error, appointments: []} (never raises).
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {"date": "", "day_offset": day_offset, "count": 0,
                    "appointments": [], "error": "beautifulsoup4 not installed",
                    "source": "svigg_live"}

        n = int(day_offset)
        url = f"{self.BASE_URL}/proxy.cgi/off/home/appt_b.htm?todayonly={n}"
        try:
            await self._goto(url, wait_until="networkidle", timeout=25000)
            await self._page.wait_for_timeout(1200)
            html = await self._page.content()
        except Exception as exc:  # noqa: BLE001
            return {"date": "", "day_offset": n, "count": 0, "appointments": [],
                    "error": f"schedule fetch failed: {exc}", "source": "svigg_live"}

        soup = BeautifulSoup(html, "html.parser")

        def _cell(cells, idx):
            if 0 <= idx < len(cells):
                return cells[idx].get_text(" ", strip=True)
            return ""

        def _iso_from_mdy(mdy):
            m = re.match(r"(\d{2})/(\d{2})/(\d{4})", mdy or "")
            if m:
                return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
            return ""

        page_date_iso = ""
        appointments = []
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "appt_e.htm" not in href:
                continue
            q = parse_qs(urlparse(href.replace("&amp;", "&")).query)
            row_mdy = (q.get("date") or [""])[0]
            row_date_iso = _iso_from_mdy(row_mdy)
            if row_date_iso and not page_date_iso:
                page_date_iso = row_date_iso
            enc = (q.get("enc") or [""])[0]
            link_time = (q.get("time") or [""])[0]
            prov_code = (q.get("prov") or [""])[0]

            tr = link.find_parent("tr")
            cells = tr.find_all(["td", "th"]) if tr else []
            date_stime = _cell(cells, 1)
            atime = _cell(cells, 2)
            patient_name = _cell(cells, 3)
            provider = _cell(cells, 6) or prov_code
            visit_type = _cell(cells, 9)
            cpt = _cell(cells, 10)
            insurance = _cell(cells, 11)
            note = _cell(cells, 15)

            # Prefer the scheduled start time from the link (HH:MMAM/Noon); fall
            # back to the Date/STime cell's time portion.
            start_time = link_time or (date_stime.split(None, 1)[1]
                                       if " " in date_stime else date_stime)
            # Status: the leftmost cell wraps a check-in icon/link; treat presence
            # of a 'checkin=' link as scheduled/booked, else blank. We do NOT invent
            # a status the portal doesn't show.
            status = ""
            sts_cell = cells[0] if cells else None
            if sts_cell is not None:
                a0 = sts_cell.find("a", href=True)
                if a0 and "checkin=" in a0.get("href", ""):
                    status = "scheduled"

            appointments.append({
                "date": row_date_iso,
                "start_time": start_time,
                "atime": atime,
                "patient_name": patient_name,   # PHI — kept in row, never logged
                "provider": provider,
                "visit_type": visit_type,
                "cpt": cpt,
                "insurance": insurance,
                "note": note,
                "encounter_id": enc,
                "status": status or "scheduled",
            })

        # Fallback page date if no rows carried one.
        if not page_date_iso:
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", html)
            if m:
                page_date_iso = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"

        return {"date": page_date_iso, "day_offset": n,
                "count": len(appointments), "appointments": appointments,
                "source": "svigg_live"}

    async def get_appointment_calendar(self, date: str = None, *, meta: dict | None = None) -> list[dict]:
        """
        Scrape the Svigg global appointment calendar (book.htm frame) for a given date.

        Discovery (2026-06-28, verified live):
          - /proxy.cgi/app/enc/cal.htm is a frameset — the outer doc body is empty.
          - Data lives in the BOOK frame: /proxy.cgi/{session_token}/book.htm
          - Session token is a 9-digit number allocated fresh per browser session.
          - The book frame has 23 tables, ~18KB HTML; patient cells are:
              <a href='mre?x=N&y=M&r=R'>(LENGTH)&nbsp;LastName,&nbsp;FirstInit&nbsp;/Type</a>
          - Appointment status encoded in cell bgcolor:
              #FFCCFF = booked, #CCFFCC = arrived/checked-in, #FFFF99 = confirmed.

        ONE-DAY MODE (fixed 2026-07-05, live-proven — replaces the old
        fill-dt + click-GO calfilt_p pattern): when `date` is given, the
        calendar is filtered via `_apply_oneday_filter`, Svigg's OneDay
        checkbox + `thismon` click. This genuinely renders a SINGLE day —
        FIXES the long-standing multi-day misattribution where rows from
        NEIGHBORING days bled into the result under the old date filter
        (confirmed live 2026-07-02: a 07/13 appt appeared in the 07/09
        window; one incident returned 46 rows for a single requested day).
        `date_filter_applied` on every returned row now means "confirmed
        ONE-DAY view for this exact date" rather than "a filter POST was
        attempted". Cell labels are parsed via `_parse_mre_label`, which
        normalizes the &nbsp;/\\xa0 separators Svigg renders between the
        duration/last-name/first-name/type fragments (a plain
        `.get_text(strip=True)` split on those separators silently drops or
        mangles multi-word last names, e.g. "Patients 1").

        When NO date is given, this falls back to whatever view is
        currently rendered (no OneDay filter is applied) — every returned
        row then carries `date_filter_applied=False` and callers must NOT
        assume the rows are scoped to any particular day.

        Args:
            date: ISO date string YYYY-MM-DD (defaults to today).
            meta: optional out-dict; when provided, ``meta["date_filter_applied"]``
                is set to the authoritative ONE-DAY-filter confirmation even when
                the returned appointment list is EMPTY (rows otherwise carry that
                marker, but an emptied day has no rows). Lets a caller trust a
                filter-confirmed empty grid. Only written when ``date`` is given.

        Returns list of appointment dicts from the calendar page.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return [{"error": "BeautifulSoup (beautifulsoup4) not installed"}]

        if date is None:
            from datetime import datetime as _dt
            date = _dt.now().strftime("%Y-%m-%d")

        page = self._page
        import re as _re

        date_filter_applied = False
        book_frame = None
        header_dates: list[str] = []

        if date:
            from datetime import datetime as _dt
            try:
                svigg_date = _dt.strptime(date, "%Y-%m-%d").strftime("%m/%d/%Y")
            except ValueError:
                return [{"error": f"date {date!r} is not a valid YYYY-MM-DD "
                                   "ISO date string"}]
            oneday = await self._apply_oneday_filter(svigg_date)
            date_filter_applied = bool(oneday.get("applied"))
            if meta is not None:
                meta["date_filter_applied"] = date_filter_applied
            book_frame = oneday.get("book_frame")
            header_dates = oneday.get("header_dates", [])
        else:
            # No date requested — open the calendar frameset and use whatever
            # view is currently rendered (no OneDay filter applied).
            try:
                cal_url = f"{self.BASE_URL}/proxy.cgi/app/enc/cal.htm"
                await self._goto(cal_url, wait_until="load", timeout=20000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                return [{"error": f"could not open calendar frameset: {e}"}]
            for frame in page.frames:
                if "book.htm" in frame.url:
                    book_frame = frame
                    break

        if book_frame is None:
            return [{
                "error": "Could not locate book.htm frame in Svigg calendar",
                "notes": (
                    "cal.htm is a frameset; book frame should appear after load. "
                    "Session may have expired or calendar may require re-login."
                ),
            }]

        if date and not date_filter_applied:
            logger.warning(
                "Svigg calendar OneDay filter: could not confirm date=%s on "
                "the grid (header_dates=%s) — returned rows are NOT scoped "
                "to this date.", date, header_dates,
            )

        # Read the book frame content
        try:
            html = await book_frame.content()
        except Exception as e:
            return [{"error": f"Could not read book frame content: {e}"}]

        soup = BeautifulSoup(html, "html.parser")

        # Parse appointment cells — each has an <a href='mre?...'> link
        appointments = []
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "mre?" not in href:
                continue
            raw_text = link.get_text(strip=True)
            if not raw_text:
                continue

            parsed = _parse_mre_label(raw_text)
            # Preserve the historical "LastName, FirstInit" combined shape
            # for patient_name (callers match on it elsewhere), now built
            # from the &nbsp;/\xa0-normalized fragments instead of a naive
            # split on the raw (unnormalized) text.
            if parsed["first_frag"]:
                name_type = f"{parsed['last']}, {parsed['first_frag']}"
            else:
                name_type = parsed["last"]
            appt_type = parsed["type_frag"]

            dur_m = _re.match(r'^\((\d+)\)', raw_text.replace("&nbsp;", " ")
                               .replace("\xa0", " ").strip())
            duration = int(dur_m.group(1)) if dur_m else None

            # Cell background encodes status
            td = link.find_parent("td")
            bgcolor = td.get("bgcolor", "").upper() if td else ""
            status = {
                "#FFCCFF": "booked",
                "#CCFFCC": "arrived",
                "#FFFF99": "confirmed",
            }.get(bgcolor, "unknown")

            # Extract x/y/r coords from href.
            # r= is the Svigg internal rowid for the calendar cell. It is a
            # stable per-appointment reference within a session, but it is NOT
            # the patient acct number. Resolving r -> acct requires a follow-up
            # GET on the mre? link per cell (deferred to Phase 2).
            coord_match = _re.search(r'x=(\d+)&y=(\d+)(?:&r=(\d+))?', href)
            x = coord_match.group(1) if coord_match else None
            y = coord_match.group(2) if coord_match else None
            r = coord_match.group(3) if coord_match else None
            appointment_ref = str(r) if r is not None else ""

            appointments.append({
                "patient_name": name_type,
                "appointment_type": appt_type,
                "duration_minutes": duration,
                "status": status,
                "cell_x": x,
                "cell_y": y,
                # Svigg internal rowid (from href r=); a stable appointment
                # reference, not necessarily the patient acct. acct resolution
                # is a Phase 2 follow-up (one GET per cell).
                "appointment_ref": appointment_ref,
                "date_requested": date,
                "date_filter_applied": date_filter_applied,
                "source": "svigg_live",
            })

        return appointments

    @staticmethod
    def _session_token_from_url(url: str) -> Optional[str]:
        """Extract the rotating 9-digit Svigg session token from a proxy URL.

        Real path is /proxy.cgi/{SESSION}/… where {SESSION} rotates nearly every
        request (see doctorcom-booking-contract.md §2). NEVER cache this — always
        re-read it from the current live page/frame URL at each step.
        """
        m = re.search(r'/proxy\.cgi/(\d{7,12})/', url or "")
        return m.group(1) if m else None

    async def _apply_oneday_filter(self, date_mdY: str) -> dict:
        """Apply Svigg's ONE-DAY calendar filter for `date_mdY` (live-proven).

        THE WORKING CONTRACT (live-proven twice 2026-07-05, against 07/08 AND
        07/13, each rendering exactly the requested single day): the calfilt
        form is SINGLE-SHOT — after any post the frame moves to calfilt_p
        with ZERO controls — so EVERY filter action starts from a FRESH
        cal.htm frameset navigation. Then, on the fresh `calfilt.htm` frame:
        CHECK `input[name="OneDay"]`, fill `input[name="dt"]` with `date_mdY`
        (MM/DD/YYYY), and click the `input[name="GO"]` submit ("Refresh").
        Do NOT use `thismon` — it is the mini-month IMAGE MAP whose posted
        date derives from the clicked PIXEL (the HAR's varying thismon.x/y
        are day-cell coordinates), so a center-of-image Playwright click
        selects an arbitrary day; that is how a live booking landed on
        2026-07-13 instead of the requested date. In OneDay mode book.htm
        renders EXACTLY ONE day: every add.htm/mre anchor's x=0, and the
        frame's own header carries `oneday.htm?dt=YYYYMMDD` link(s) naming
        the day actually rendered — the checkable proof the filter
        retargeted the view (see `_oneday_applied`).

        ALWAYS re-navigates to the cal.htm frameset first (fresh single-shot
        form), including before the single retry.

        Args:
            date_mdY: requested date, "MM/DD/YYYY".

        Returns:
            {"applied": bool, "header_dates": [...], "book_frame": Frame|None}
            — `header_dates` is every distinct `oneday.htm?dt=` value found
            (for diagnosing a wrong-day render), and `book_frame` is the
            live Playwright frame handle for the (re-)rendered book.htm, or
            None if it could not be located at all. NEVER raises — a
            navigation/DOM failure is reported as `applied: False` with
            `header_dates: []`, not an exception.
        """
        page = self._page

        def _find_frame(marker: str):
            for fr in page.frames:
                if marker in fr.url:
                    return fr
            return None

        last_header_dates: list[str] = []
        for attempt in range(2):  # one retry on failure
            # ALWAYS start from a fresh frameset: the calfilt form is
            # single-shot (post moves the frame to calfilt_p, zero controls),
            # so a stale frame is the live-proven "filter silently not
            # applied" failure mode.
            try:
                cal_url = f"{self.BASE_URL}/proxy.cgi/app/enc/cal.htm"
                await self._goto(cal_url, wait_until="load", timeout=20000)
                await page.wait_for_timeout(3000)
            except Exception as exc:
                logger.warning("_apply_oneday_filter: could not open cal.htm "
                               "frameset (attempt %d) for date=%s: %s",
                               attempt + 1, date_mdY, exc)
                continue
            calfilt_frame = _find_frame("calfilt.htm")
            if calfilt_frame is None:
                logger.warning("_apply_oneday_filter: calfilt.htm frame not "
                               "found (attempt %d) for date=%s", attempt + 1,
                               date_mdY)
                continue
            try:
                await calfilt_frame.check('input[name="OneDay"]')
                await calfilt_frame.fill('input[name="dt"]', date_mdY)
                await calfilt_frame.click('input[name="GO"]')
                await page.wait_for_timeout(3500)
            except Exception as exc:
                logger.warning("_apply_oneday_filter: check/fill/click failed "
                               "(attempt %d) for date=%s: %s", attempt + 1,
                               date_mdY, exc)
                continue

            book_frame = _find_frame("book.htm")
            if book_frame is None:
                logger.warning("_apply_oneday_filter: book.htm frame not "
                               "found after filter (attempt %d) for date=%s",
                               attempt + 1, date_mdY)
                continue

            try:
                html = await book_frame.content()
            except Exception as exc:
                logger.warning("_apply_oneday_filter: could not read book "
                               "frame content (attempt %d) for date=%s: %s",
                               attempt + 1, date_mdY, exc)
                continue

            last_header_dates = sorted(set(_ONEDAY_HEADER_RE.findall(html)))
            if _oneday_applied(html, date_mdY):
                return {"applied": True, "header_dates": last_header_dates,
                        "book_frame": book_frame}
            # Not applied — fall through to the retry (attempt 1 only).

        return {"applied": False, "header_dates": last_header_dates,
                "book_frame": _find_frame("book.htm")}

    async def find_bookable_days(self, start_date_mdY: str, horizon: int = 7,
                                 need: int = 2, progress_cb=None) -> list[dict]:
        """Probe successive days for open slots (FEATURE 3a — bookability guidance).

        Fixed 2026-07-05 (live incidents: bookings staged for Thu 07/09 and Fri
        07/10 — days with NO clinic session at all — failed with a bare
        ``no_free_slots`` and the owner read that as "booking is broken", when
        the real issue was simply that those specific days have no session).
        This probes forward day-by-day from ``start_date_mdY`` via the SAME
        live-proven ``_apply_oneday_filter`` the booking flow itself uses, and
        counts free ``add.htm`` anchors on each day's one-day grid — giving
        staff (via the booking executor's honest error message) concrete
        alternative days instead of a dead end.

        Args:
            start_date_mdY: first date to probe, "MM/DD/YYYY".
            horizon: maximum number of calendar days to probe forward
                (inclusive of the start date) before giving up.
            need: stop early once this many bookable days have been found.
            progress_cb: optional ``callable(stage_key: str, label: str)``,
                same contract as ``book_appointment``'s — called once per
                probed day with stage_key='probe_day' and a label naming the
                date being checked. Guarded try/except; a callback bug never
                affects the probe.

        Returns:
            Up to ``need`` entries ``{"date_mdY": ..., "free_slots": N}`` for
            days that DO have at least one free ``add.htm`` anchor, in the
            order probed (soonest first). Days that error out during the
            probe (filter could not be confirmed, frame not found) are
            silently skipped — never reported as "bookable" and never raising
            out of this method; a probe failure just means one fewer
            candidate day, not a crash. Returns ``[]`` if nothing bookable
            was found within ``horizon`` days (an honest empty result, never
            fabricated).
        """
        from datetime import datetime as _dt, timedelta as _td

        def _report(stage_key: str, label: str) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(stage_key, label)
            except Exception:
                pass  # a progress-callback bug must never affect the probe

        try:
            start = _dt.strptime(str(start_date_mdY).strip(), "%m/%d/%Y")
        except ValueError:
            logger.warning("find_bookable_days: unparseable start_date_mdY=%r",
                           start_date_mdY)
            return []

        horizon = max(int(horizon or 0), 0)
        need = max(int(need or 0), 0)
        found: list[dict] = []

        for offset in range(horizon):
            if len(found) >= need:
                break
            probe_date = start + _td(days=offset)
            probe_mdY = probe_date.strftime("%m/%d/%Y")
            _report("probe_day", f"Checking {probe_mdY} for open slots")
            try:
                oneday = await self._apply_oneday_filter(probe_mdY)
            except Exception as exc:
                logger.warning("find_bookable_days: probe failed for %s: %s",
                              probe_mdY, exc)
                continue
            if not oneday.get("applied"):
                continue
            book_frame = oneday.get("book_frame")
            if book_frame is None:
                continue
            try:
                free_count = await book_frame.locator(
                    'a[href*="add.htm?x="]'
                ).count()
            except Exception as exc:
                logger.warning("find_bookable_days: could not count free "
                              "slots for %s: %s", probe_mdY, exc)
                continue
            if free_count > 0:
                found.append({"date_mdY": probe_mdY, "free_slots": free_count})

        return found

    async def _verify_booking_on_grid(self, *, date: str, last_name: str) -> dict:
        """Re-load the booking grid (ONE-DAY mode) and look for the just-booked patient.

        FIX 2 (post-submit verification, fixed 2026-07-05, live incident): the
        old code returned "submitted"/"submitted_overbooked" as terminal
        success immediately after the bk_p POST, with ZERO check that
        anything was actually created — a false success (the live incident
        showed a "submitted_overbooked" response with NOTHING on the grid).
        This helper independently re-navigates the calendar, re-applies
        `_apply_oneday_filter` for `date` (live-proven ONE-DAY mode — every
        anchor's x=0, so there is no day-column ambiguity left to resolve),
        and scans the single-day grid for an `mre?x=&y=` appointment-cell
        anchor whose parsed `last` name (via `_parse_mre_label`,
        &nbsp;/\\xa0-normalized) matches the patient's last name
        (case-insensitive).

        Args:
            date: the appointment date that was just booked, MM/DD/YYYY.
            last_name: patient last name to match against grid cell text.

        Returns:
            {"verified": True, "cell": {x, y, r, label}} when a matching
                cell is found on the one-day grid.
            {"verified": False, "evidence": {...}} otherwise — evidence
                carries the response page's title/first-200-chars so a
                human can diagnose why nothing was found (session drop,
                date not on grid, etc.). NEVER raises; a scrape failure
                during verification is itself evidence of "not verified".
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {"verified": False,
                    "evidence": {"error": "BeautifulSoup not installed — "
                                           "cannot verify"}}

        page = self._page

        oneday = await self._apply_oneday_filter(date) if date else \
            {"applied": False, "header_dates": [], "book_frame": None}
        book_frame = oneday.get("book_frame")

        if book_frame is None:
            # Fall back to whatever book.htm frame is currently open (no date
            # was given, or the frame could not be re-located at all).
            for frame in page.frames:
                if "book.htm" in frame.url:
                    book_frame = frame
                    break
        if book_frame is None:
            return {"verified": False,
                    "evidence": {"error": "book.htm frame not found on "
                                           "verification re-load (session "
                                           "may have expired)"}}

        try:
            html = await book_frame.content()
            title = await page.title()
        except Exception as exc:
            return {"verified": False,
                    "evidence": {"error": f"could not read grid for "
                                           f"verification: {exc}"}}

        def _clean(h):
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()

        if date and not oneday.get("applied"):
            return {
                "verified": False,
                "evidence": {
                    "response_title": title,
                    "response_excerpt": _clean(html)[:200],
                    "date": date,
                    "date_filter_applied": False,
                    "header_dates": oneday.get("header_dates", []),
                },
            }

        soup = BeautifulSoup(html, "html.parser")
        needle = (last_name or "").strip().lower()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "mre?" not in href:
                continue
            cm = re.search(r'x=(\d+)&y=(\d+)(?:&r=(\d+))?', href)
            if not cm:
                continue
            raw_label = link.get_text(strip=True)
            parsed = _parse_mre_label(raw_label)
            if needle and needle not in parsed["last"].lower():
                continue
            return {
                "verified": True,
                "cell": {
                    "x": int(cm.group(1)),
                    "y": int(cm.group(2)),
                    "r": cm.group(3),
                    "label": raw_label,
                },
            }

        return {
            "verified": False,
            "evidence": {
                "response_title": title,
                "response_excerpt": _clean(html)[:200],
                "date": date,
                "date_filter_applied": bool(date and oneday.get("applied")),
            },
        }

    async def _capture_proof(self, proof_path: Optional[str]) -> bool:
        """Best-effort screenshot of the current page (FEATURE 8 — proof
        capture). Returns True iff the screenshot was actually written.

        ``proof_path`` is optional and caller-provided (the approvals
        executor passes ``DATA_DIR/proof/approval_<id>.png``, parent dirs
        already created). Guarded try/except — a screenshot failure (disk
        full, page closed, etc.) must NEVER affect the booking/cancel result
        it is documenting; it just means no visual proof was captured this
        time, which the caller surfaces honestly (never assumed).
        """
        if not proof_path or self._page is None:
            return False
        try:
            await self._page.screenshot(path=proof_path, full_page=False)
            return True
        except Exception as exc:
            logger.warning("_capture_proof: screenshot failed for %s: %s",
                          proof_path, exc)
            return False

    async def book_appointment(
        self,
        *,
        acct: str = "",
        rowid: str = "",
        last_name: str = "",
        first_name: str = "",
        dob: str = "",
        date: str = "",
        start_time: str = "",
        duration_min: int = 15,
        appt_type: str = "EST",
        provider: str = "SGUPTA",
        note: str = "",
        incident: str = "",
        x: Optional[int] = None,
        y: Optional[int] = None,
        execute: bool = False,
        confirm_unverified: bool = False,
        allow_overbook: bool = False,
        progress_cb=None,
        proof_path: Optional[str] = None,
    ) -> dict:
        """Walk the Svigg add-appointment flow to the prepared `bk_p` (conf) form.

        SAFETY: By DEFAULT (execute=False) this is a DRY-RUN that prepares the
        booking form and returns WITHOUT submitting — it CREATES NOTHING. The
        commit (POST to bk_p) is gated behind TWO independent locks.

        FORM-FAITHFUL PAYLOAD (fixed 2026-07-05 per a real human booking+cancel
        HAR, websrv01.physician-to-go.net, Test Patient acct 22041163): the
        payload submitted is now a VERBATIM serialization of the live
        form[name="conf"] (every input/select/textarea, checkbox semantics
        included), with ONLY cpt00/Duration00/note00/Note (and Incident, if
        explicitly passed) overridden — see _apply_overrides(). The HAR showed
        the human's successful commit leaves FromDate/FromTime1/ToDate/ToTime1
        and the weekday checkboxes at the server's RENDERED DEFAULTS; the real
        appointment date+time is bound by the staged grid cell
        (add.htm?x&y -> calAddLookup?rowid&acct), not by those fields.

        ⚠️ KNOWN SERVER-SIDE CONFLICT (discovered live 2026-07-07, root-caused
        2026-07-05): the conf form carries a "Call If Cancellation"
        callback-list section alongside the appointment fields. If the SAME
        patient already has a pending entry in the site's Callback Table for
        that appointment date — a residual side effect of an earlier CANCEL
        that left AutoReserve populated (see cancel_appointment) — any
        subsequent booking attempt for that patient/date bounces with "From
        Appt Date already exists in Callback Table" and creates nothing. Prior
        to the 2026-07-05 fix this was misdiagnosed as pure EMR-side state;
        the HAR shows our OWN synthetic FromDate override was what collided
        with the check (the human's rendered-default FromDate never triggers
        it). Still re-verify via a calendar re-read (never trust "submitted"/
        "submitted_overbooked" alone) — a callback_conflict can also occur
        against genuinely stale server state.

        Flow (all read-only until the final bk_p Submit, per contract §7):
          /proxy.cgi/app/enc/cal.htm  (frameset)
            → /proxy.cgi/{SESSION}/book.htm  (grid; free slot = add.htm?x&y)
            → [read-only] _apply_oneday_filter (ONE-DAY mode, live-proven
              2026-07-05 — checks OneDay, fills dt, clicks thismon; renders
              exactly one day so every anchor's x=0)
            → click add.htm?x&y  (GET — renders patient-search form)
            → form name=psrch  POST addLookup  (read-only patient name search)
            → "Patient View" results
            → click calAddLookup?rowid&acct  (GET — renders the conf form)
            → form name=conf, action POST /proxy.cgi/{SESSION}/bk_p  (STOP HERE)

        Args:
            acct: numeric patient account (e.g. "2574961").
            rowid: Svigg internal patient rowid (e.g. "AAA...@main01").
            last_name/first_name: used to resolve rowid/acct if not supplied,
                AND to verify the patient-select link matches the intended
                patient before clicking it.
            dob: OPTIONAL caller-side DOB for the intended patient. When both
                this AND the resolved account's own DOB are known, the
                pre-commit identity guard (_identity_matches) additionally
                requires them to be equal — an extra deterministic bind so an
                acct typo that lands on a same/similar name but a different
                person is still refused. Missing on either side degrades to the
                name-only bind (never fabricated, never blocks on its own).
            date: appointment date, MM/DD/YYYY. Applied via the live-proven
                ONE-DAY filter (_apply_oneday_filter) — see date_not_on_grid
                below for what happens when it can't be confirmed.
            start_time: appointment start time string (e.g. "8:00a" / "08:00").
                Used to try to match the one-day grid's own 15-min row index
                (_time_to_slot_y) against a free anchor's y=; if no row
                matches (or start_time is omitted) the first free anchor on
                the day is used instead — the prepared result's slot.text
                always carries the actual anchor's rendered text so operators
                can see exactly which row was staged.
            duration_min: appointment length in minutes.
            appt_type: cpt00 select value — "EST" (established) or "NP" (new).
            provider: NOT currently applied to the calendar filter (the
                live-proven ONE-DAY contract only fills OneDay+dt and clicks
                thismon); the conf form's own prov select is NOT overridden
                at commit either — per the 2026-07-05 HAR the human never
                touches it, so it stays at the rendered default (whatever
                calAddLookup pre-selected for this slot).
            note: free-text note (maps to note00 / Note).
            incident: optional Incident radio value (case to bill). Left blank
                in propose unless explicitly supplied (value→case map UNVERIFIED).
            x, y: explicit free-slot grid coords (ONE-DAY mode: x is normally
                0). If omitted, slot selection uses start_time (see above) or
                the first free add.htm?x&y anchor on the one-day grid.
                ⚠ Date binding (learned live 2026-07-02, still true in
                ONE-DAY mode): the CELL decides the appointment's real
                date+time — FromDate/FromTime do NOT override it. Never trust
                an x/y from a prior dry-run against a DIFFERENT date to pin
                today's date; verify the real date afterwards (the cancel
                path day-binds on the mre edit form for exactly this reason).
            execute: if False (DEFAULT) → propose only, never POST. If True →
                still blocked unless BOTH BOOKING_EXECUTE_ENABLED and
                confirm_unverified are also True.
            confirm_unverified: caller's explicit acknowledgement that the
                commit contract is unverified. Required (with the module flag)
                to ever POST.
            progress_cb: optional ``callable(stage_key: str, label: str)``
                invoked at natural stage boundaries (calendar filter applied,
                patient selected, submitted, verified) so a caller (e.g.
                ``approvals._execute_book``) can surface live progress while
                this coroutine runs. Called synchronously; any exception it
                raises is caught and ignored — a progress-reporting bug must
                NEVER affect the booking flow itself.
            proof_path: optional filesystem path (FEATURE 8 — proof capture).
                When given AND execute=True actually reaches the post-submit
                verification step, a screenshot of the one-day grid
                (``self._page.screenshot(path=proof_path, full_page=False)``)
                is captured best-effort — a screenshot failure is caught and
                ignored (never affects the booking flow or its result); the
                caller learns whether it worked via ``result["proof_captured"]``
                (bool), never assumed from proof_path merely being non-None.

        Returns one of:
          {status:"prepared", action_url, fields:{…}, slot, acct, rowid, warning}
              fields is the full serialized+overridden payload (form-faithful,
              per the 2026-07-05 HAR) so operators can inspect exactly what
              would be submitted; slot carries {x, y, href, text} — text is
              the staged anchor's actual rendered content.
          {status:"execute_blocked", reason, prepared:{…}}
          {status:"date_not_on_grid", error, date, header_dates, ...}
              (fixed 2026-07-05, live-proven ONE-DAY fix: _apply_oneday_filter
              could not confirm the requested date is what's rendered — even
              after its own internal retry — so nothing was staged or
              submitted; header_dates shows what the grid actually reported)
          {status:"no_free_slots", error, date, date_filter_applied,
              alternatives:{same_day_open_slots:[], other_days:[...],
              overbook_available:False, alternatives_error?}}
              (the one-day grid for the confirmed date has no free add.htm
              anchors — day may have no clinic session or is fully booked;
              nothing was staged. alternatives.other_days is the
              find_bookable_days probe for the next `horizon` days —
              additive; the caller's own "nearest_bookable" enrichment,
              built separately in app/approvals.py, is unaffected)
          {status:"submitted_verified", response_*, submitted_fields,
              verified_cell:{x,y,r,label}, warning}
              (fixed 2026-07-05, live incident: the bk_p Submit was posted
              AND an immediate one-day grid re-read found the patient's mre?
              cell — this is the ONLY status that means "created and
              confirmed")
          {status:"submit_unconfirmed", response_*, submitted_fields,
              evidence:{response_title, response_excerpt, date,
              date_filter_applied}, warning}
              (fixed 2026-07-05, live incident: the bk_p Submit was posted
              but the post-submit grid re-read did NOT find the patient —
              treat as NOT confirmed created; re-check the calendar by hand.
              Replaces the old "submitted"/"submitted_overbooked" terminal
              statuses, which never verified anything)
          {status:"slot_conflict", error, controls, submitted_fields,
              prepared:{…}, alternatives:{same_day_open_slots:[...],
              other_days:[...], overbook_available:True, alternatives_error?}}
              (Overbook control detected — the slot is full — but
              allow_overbook was False, so nothing further was submitted
              and the slot was NOT force-overbooked. alternatives is
              gathered BEFORE returning so the dashboard can offer the
              human same-day open slots or other bookable days instead of
              only "retry with allow_overbook=true". Gathering alternatives
              is best-effort and never turns this refusal into a success.)
          {status:"callback_conflict", error, submitted_fields, prepared:{…}}
              (server's "already exists in Callback Table" bounce — see the
              conflict note above; nothing created)
          {status:"error", error, stage}
        """
        page = self._page

        def _report(stage_key: str, label: str) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(stage_key, label)
            except Exception:
                pass  # a progress-callback bug must never affect the flow

        # ---- Reject an implausible rowid BEFORE it can short-circuit -----
        # ---- resolve_patient (live incident 2026-07-05: rowid="46747", a --
        # ---- numeric SIS-style id lifted from an unrelated emr_search) ---
        # A truthy-but-bogus rowid would make the `if not rowid and acct:`
        # acct-first resolve below skip entirely, then make the strict
        # acct+rowid anchor match reject every real anchor (a real Svigg
        # rowid looks like "AAAYCzAFhAAO!k1AAC@main01"), and finally send the
        # HAR fast path to calAddLookup with an invalid rowid. Clearing it
        # here lets the existing acct-first resolve_patient stage (reliable,
        # already in place) resolve a fresh, real rowid instead.
        if rowid and not _is_plausible_svigg_rowid(rowid):
            logger.warning(
                "ignoring implausible svigg rowid %r — will resolve fresh",
                rowid,
            )
            rowid = ""

        # ---- Resolve patient identity (rowid/acct) ----------------------
        # Fixed 2026-07-05, live incident: card #263 (acct='22041163',
        # rowid='', last_name='Test Patients', first_name='1') failed here
        # with 0 matches, while card #260 for the SAME acct (just a
        # differently-split name from the chat model) succeeded. The old
        # logic searched BY NAME ONLY and let an unlucky name split veto a
        # perfectly valid, unique acct sitting right there in the params. A
        # name split must NEVER be able to veto an exact, unique acct match —
        # so when acct is present and rowid is not, we resolve ACCT-FIRST via
        # search_patient(acct=...) and only fall back to name-based
        # resolution when acct is empty. The exactly-one-match rule for
        # name-based resolution is unchanged.
        resolved_by = None
        resolve_diagnostics: dict = {}
        # Captures the resolved chart's OWN "Last, First" (when the resolve
        # stage actually found a row) so the fast-path identity check below
        # can also try the RESOLVED name's tokens, not just the caller's
        # params name — the two can differ (see the acct-match-wins warning
        # below) and either being present on the conf page is proof enough.
        resolved_patient_name = ""
        # The resolved chart's OWN DOB (when search_patient parsed one off the
        # result row — "" if unavailable). Fed to the pre-commit identity
        # guard so a name+DOB pair can be deterministically confirmed against
        # the EMR's own record before any write. Captured in BOTH resolve
        # branches (acct-first and name-based) so the guard sees it either way.
        resolved_patient_dob = ""
        if not rowid and acct:
            try:
                acct_matches = await self.search_patient(acct=acct)
            except Exception as exc:
                return {"status": "error", "stage": "resolve_patient",
                        "error": f"search_patient (acct) failed: {exc}"}
            pick = _pick_acct_match(acct_matches, acct)
            if pick["picked"] is not None:
                picked = pick["picked"]
                rowid = picked.get("rowid", "")
                acct = picked.get("acct", "") or acct
                resolved_by = "acct"
                resolved_patient_name = (picked.get("name") or "").strip()
                resolved_patient_dob = (picked.get("dob") or "").strip()
                # Name in params never vetoes the acct match, but a material
                # mismatch is worth flagging for a human to notice. Both
                # names are PHI, so the log line below carries NEITHER name —
                # only the acct (not PHI on its own) and a boolean mismatch
                # signal.
                params_name = f"{last_name.strip()},{first_name.strip()}".strip(",")
                picked_name = (picked.get("name") or "").strip()
                if params_name and picked_name and (
                    _norm_name_for_compare(params_name)
                    != _norm_name_for_compare(picked_name)
                ):
                    logger.warning(
                        "book_appointment/resolve_patient: resolved acct=%s "
                        "by ACCT MATCH but the params name differs from the "
                        "resolved chart's name (acct match still wins; "
                        "params name is not used to veto it).", acct,
                    )
            else:
                # 0 or >1 acct matches — fail honestly rather than guess.
                resolve_diagnostics = {
                    "acct_match_count": pick["match_count"],
                    "acct_search_result_count": len(acct_matches),
                }
                if not last_name:
                    match_desc = (
                        "no match" if pick["match_count"] == 0
                        else f"ambiguous — {pick['match_count']} matches"
                    )
                    return {"status": "error", "stage": "resolve_patient",
                            "error": (
                                "could not resolve patient from acct "
                                f"({match_desc}); pass explicit acct+rowid"
                            ),
                            "match_count": pick["match_count"],
                            "diagnostics": resolve_diagnostics}
                # else: fall through to name-based resolution below as a
                # last resort (acct search inconclusive but we do have a
                # name to try).

        if not rowid and last_name:
            # Bounded VARIANT LADDER (2026-07-05): a chat model can split a
            # pathological patient name incorrectly (e.g. "Test Patients"/
            # "1" for the true chart "Patients 1, Test"), so a single name
            # search is not reliable. When an acct is present, each variant
            # is tried in turn and ONLY accepted via _pick_acct_match — a
            # unique acct match — never by name similarity alone; a variant
            # matching zero or ambiguously just advances to the next one.
            # When acct is ABSENT, we run ONLY variant (a) (index 0) under
            # the pre-existing exactly-one-match rule — without an acct to
            # filter on, the looser variants (swapped/token-only) are too
            # sloppy to trust.
            variants = _resolve_name_variants(last_name, first_name)
            if not acct:
                variants = variants[:1]

            picked = None
            total_name_matches = 0
            variants_tried = 0
            for idx, (v_last, v_first) in enumerate(variants, start=1):
                variants_tried = idx
                if idx > 1:
                    # Short pause between search round-trips so we don't
                    # hammer the portal across a multi-variant ladder.
                    await asyncio.sleep(0.5)
                try:
                    matches = await self.search_patient(v_last, v_first)
                except Exception as exc:
                    return {"status": "error", "stage": "resolve_patient",
                            "error": f"search_patient failed: {exc}"}
                total_name_matches += len(matches)

                if acct:
                    pick = _pick_acct_match(matches, acct)
                    if pick["picked"] is not None:
                        picked = pick["picked"]
                        logger.info(
                            "book_appointment/resolve_patient: name variant "
                            "%d/%d matched acct uniquely", idx, len(variants),
                        )
                        break
                    logger.info(
                        "book_appointment/resolve_patient: name variant "
                        "%d/%d — acct match_count=%d, no unique pick",
                        idx, len(variants), pick["match_count"],
                    )
                else:
                    # No acct at all: exactly-one-match rule, variant (a) only.
                    if len(matches) == 1 and matches[0].get("rowid"):
                        picked = matches[0]
                        logger.info(
                            "book_appointment/resolve_patient: name variant "
                            "%d/%d — single unambiguous match (no acct "
                            "available to filter on)", idx, len(variants),
                        )
                    else:
                        logger.info(
                            "book_appointment/resolve_patient: name variant "
                            "%d/%d — %d matches, not unambiguous",
                            idx, len(variants), len(matches),
                        )
                    break  # never run further variants without an acct filter

            if not picked:
                resolve_diagnostics["name_match_count"] = total_name_matches
                resolve_diagnostics["variants_tried"] = variants_tried
                return {"status": "error", "stage": "resolve_patient",
                        "error": "could not resolve patient from name; "
                                 "pass explicit acct+rowid",
                        "match_count": total_name_matches,
                        "diagnostics": resolve_diagnostics or None}
            rowid = rowid or picked.get("rowid", "")
            acct = acct or picked.get("acct", "")
            resolved_by = resolved_by or "name"
            resolved_patient_name = (
                resolved_patient_name or (picked.get("name") or "").strip()
            )
            resolved_patient_dob = (
                resolved_patient_dob or (picked.get("dob") or "").strip()
            )

        if not rowid or not acct:
            return {"status": "error", "stage": "resolve_patient",
                    "error": "acct and rowid are required (or a resolvable "
                             "last_name)"}

        # ---- Apply the ONE-DAY calendar filter for the requested date ----
        # (fixed 2026-07-05, live-proven: replaces the old fill-dt+click-GO
        # calfilt_p pattern, which was live-proven UNRELIABLE — a cancel
        # refused with "date filter not applied" and a booking landed on
        # 2026-07-13 instead of the requested date. In ONE-DAY mode every
        # anchor's x=0, so day-column disambiguation via _grid_day_columns
        # is no longer needed at all — see that function's docstring.)
        date_filter_applied = False
        book_frame = None
        if date:
            oneday = await self._apply_oneday_filter(date)
            date_filter_applied = bool(oneday.get("applied"))
            book_frame = oneday.get("book_frame")
            if not date_filter_applied:
                proof_captured = await self._capture_proof(proof_path)
                return {"status": "date_not_on_grid",
                        "error": (f"requested date {date} could not be "
                                  "confirmed on the booking grid via the "
                                  "OneDay filter (even after a retry) — "
                                  "refusing to stage a slot without proof "
                                  "of which day is rendered"),
                        "date": date,
                        "header_dates": oneday.get("header_dates", []),
                        "date_filter_applied": False,
                        "proof_captured": proof_captured}
            _report("calendar_day", "Calendar open on the right day")
        else:
            logger.warning(
                "book_appointment called with no date — the OneDay filter "
                "cannot be targeted; navigating to the calendar's default "
                "view and using the first free anchor found there (risk of "
                "landing on an unintended day).")
            try:
                cal_url = f"{self.BASE_URL}/proxy.cgi/app/enc/cal.htm"
                await self._goto(cal_url, wait_until="load", timeout=20000)
                await page.wait_for_timeout(3000)
            except Exception as exc:
                return {"status": "error", "stage": "calendar",
                        "error": f"could not open calendar frameset: {exc}"}
            for frame in page.frames:
                if "book.htm" in frame.url:
                    book_frame = frame
                    break

        if book_frame is None:
            return {"status": "error", "stage": "calendar",
                    "error": "could not locate book.htm frame (session may have "
                             "expired or login required)"}

        # Re-read the rotating token from the CURRENT book frame URL.
        session_token = self._session_token_from_url(book_frame.url)

        # ---- Locate the free slot anchor add.htm?x&y ---------------------
        # ONE-DAY mode renders a single day-column (every free/booked anchor
        # carries x=0), so no day-column filtering is needed here — the old
        # _grid_day_columns-based restriction is retired from this path (see
        # that function's docstring; it is week-mode-only now). When a
        # start_time is given we try to match the grid's own 15-min row
        # index (_time_to_slot_y) against the free anchors' y=; when that
        # can't be matched (or no start_time was given), the first free
        # anchor is acceptable — we note the actual slot's href either way
        # so operators can see exactly which row was staged.
        try:
            if x is not None and y is not None:
                slot_x, slot_y = int(x), int(y)
                slot_href = f"add.htm?x={slot_x}&y={slot_y}"
                slot_locator = book_frame.locator(
                    f'a[href*="add.htm?x={slot_x}&y={slot_y}"]'
                )
                if await slot_locator.count() == 0:
                    return {"status": "error", "stage": "slot",
                            "error": f"requested slot x={slot_x},y={slot_y} is "
                                     "not a free add.htm anchor on the grid"}
            else:
                add_links = book_frame.locator('a[href*="add.htm?x="]')
                n_free = await add_links.count()
                if n_free == 0:
                    # ---- Bookability guidance (additive) --------------------
                    # No overbook control exists when the day has no free
                    # anchors at all (nothing to overbook against), so
                    # overbook_available is honestly False here. Probe
                    # forward for other bookable days via the SAME
                    # find_bookable_days used elsewhere — best-effort, never
                    # lets a probe failure crash or mask this refusal.
                    other_days: list = []
                    other_days_error = None
                    if date:
                        try:
                            other_days = await self.find_bookable_days(
                                date, horizon=7, need=2)
                        except Exception as exc:
                            logger.warning(
                                "book_appointment/no_free_slots: "
                                "find_bookable_days failed for %s: %s",
                                date, exc)
                            other_days = []
                            other_days_error = (
                                f"could not probe other days: {exc}")
                    proof_captured = await self._capture_proof(proof_path)
                    return {"status": "no_free_slots",
                            "error": ("no free add.htm anchors on the "
                                      "one-day grid for "
                                      f"{date or 'the default view'} — day "
                                      "may have no clinic session or is "
                                      "fully booked"),
                            "date": date,
                            "date_filter_applied": date_filter_applied,
                            "alternatives": _build_alternatives(
                                same_day_open_slots=[],
                                other_days=other_days,
                                overbook_available=False,
                                other_days_error=other_days_error),
                            "proof_captured": proof_captured,
                            }

                slot_locator = None
                slot_href = ""
                slot_x = slot_y = None
                target_y = None
                if start_time:
                    try:
                        target_y = self._time_to_slot_y(start_time)
                    except ValueError as exc:
                        logger.warning(
                            "book_appointment: could not convert start_time "
                            "%r to a grid row (%s) — falling back to the "
                            "first free anchor.", start_time, exc)
                        target_y = None
                if target_y is not None:
                    for i in range(n_free):
                        cand = add_links.nth(i)
                        cand_href = await cand.get_attribute("href") or ""
                        cm = re.search(r'add\.htm\?x=(\d+)&y=(\d+)', cand_href)
                        if not cm:
                            continue
                        if int(cm.group(2)) == target_y:
                            slot_locator = cand
                            slot_href = cand_href
                            slot_x = int(cm.group(1))
                            slot_y = target_y
                            break
                if slot_locator is None:
                    # No start_time match (or none requested) — first free
                    # anchor is acceptable; record the actual slot text.
                    slot_locator = add_links.first
                    slot_href = await slot_locator.get_attribute("href") or ""
                    cm = re.search(r'add\.htm\?x=(\d+)&y=(\d+)', slot_href)
                    slot_x = int(cm.group(1)) if cm else None
                    slot_y = int(cm.group(2)) if cm else None
        except Exception as exc:
            return {"status": "error", "stage": "slot",
                    "error": f"slot lookup failed: {exc}"}

        try:
            slot_text = (await slot_locator.first.text_content()) or ""
        except Exception:
            slot_text = ""

        # ---- Click the free slot (GET) → patient-search form (psrch) ----
        try:
            await slot_locator.first.click()
            await page.wait_for_timeout(2000)
        except Exception as exc:
            return {"status": "error", "stage": "add_form",
                    "error": f"could not open add.htm slot form: {exc}"}

        # Find the frame that now hosts the psrch search form.
        psrch_frame = None
        for frame in page.frames:
            try:
                if await frame.query_selector('form[name="psrch"]'):
                    psrch_frame = frame
                    break
            except Exception:
                continue
        if psrch_frame is None:
            return {"status": "error", "stage": "add_form",
                    "error": "patient-search form (name=psrch) did not render "
                             "after opening the slot"}

        async def _find_conf_frame():
            """Scan all frames for the rendered conf (bk_p) form. Shared by
            the direct calAddLookup fast path (below) and the normal
            search+anchor path's own post-click detection, so both treat
            "did the booking conf form render" identically."""
            for frame in page.frames:
                try:
                    if await frame.query_selector('form[name="conf"]'):
                        return frame
                except Exception:
                    continue
            return None

        async def _conf_frame_matches_identity(
            frame, want_acct: str, names_to_try: list,
        ) -> tuple:
            """Identity check for the fast path (2026-07-05 safety fix, then
            corrected same day per HAR evidence). Does the rendered conf
            (bk_p) form/page actually belong to OUR patient? A bogus/
            implausible rowid that still happened to navigate somewhere
            (e.g. a validation-error page, or — worse — a different
            patient's conf form) must never be allowed to fall through to
            Submit.

            CORRECTION (coordinator, 2026-07-05): the conf form itself has
            NO acct field and NO name field — its inputs are only
            Incident/TFORMCOUNT/cpt00/Dept00/Duration00/note00/FromDate/
            FromTime1/prov/Note/ToDate/ToTime1/off/weekday flags/Submit (HAR-
            verified bk_p POST body). The page identifies the patient purely
            by DISPLAYED NAME in the page body text (e.g. "Patients 1,
            Test"), so an acct-only check false-negatives on every real
            booking. This now passes on EITHER:
              (a) our normalized acct appears in a conf-form field value or
                  the page's own visible text (kept, in case some Svigg
                  deployment/version does render it), OR
              (b) ALL of some (last, first) name-token set's tokens are
                  present (case-insensitive) in the page's visible text —
                  see _name_tokens_present. `names_to_try` is a list of
                  (last, first) pairs (e.g. both the caller's params name
                  AND the resolve stage's own resolved chart name); any one
                  set fully matching is sufficient.

            Returns (matched: bool, methods_tried: list[str]) so the caller
            can log exactly which checks were attempted when nothing
            matched — never silently fails.

            Pure check aside from the two required frame reads (field
            values + body text), no mutation.
            """
            methods_tried = []
            want_norm = _normalize_acct(want_acct)
            try:
                field_values = await frame.eval_on_selector_all(
                    'form[name="conf"] input, form[name="conf"] select, '
                    'form[name="conf"] textarea',
                    "els => els.map(e => e.value || '')"
                )
            except Exception:
                field_values = []
            try:
                page_text = await frame.evaluate(
                    "document.body ? document.body.innerText : ''"
                )
            except Exception:
                page_text = ""

            if want_norm:
                methods_tried.append("acct_field_or_text")
                for v in (field_values or []):
                    if v and _normalize_acct(str(v)) == want_norm:
                        return True, methods_tried
                if page_text and (
                    want_acct.strip() in page_text or want_norm in page_text
                ):
                    return True, methods_tried

            for last, first in (names_to_try or []):
                if not (last or first):
                    continue
                methods_tried.append(f"name_tokens({last!r},{first!r})")
                if _name_tokens_present(page_text, last, first):
                    return True, methods_tried

            return False, methods_tried

        # ---- FAST PATH (2026-07-05, HAR-verified): if resolve_patient
        # already gave us both rowid and acct, the in-grid name/acct search
        # is provably unnecessary — a real human browser HAR
        # (websrv01.physician-to-go.net, 4.har/5.har) shows the slot's
        # identity lives in the SERVER SESSION set by the add.htm click, and
        # calAddLookup only ever needs rowid+acct on the query string. Skip
        # the flaky in-grid search+anchor-scan entirely and navigate straight
        # to calAddLookup; fall back to the search path unchanged if this
        # doesn't render the conf form (wrong page / login / anything else).
        conf_frame = None
        select_link = None
        if rowid and acct:
            fast_url = build_cal_add_lookup_url(psrch_frame.url, rowid, acct)
            try:
                await psrch_frame.goto(fast_url, wait_until="domcontentloaded")
            except Exception as exc:
                logger.warning(
                    "book_appointment/patient_select: direct calAddLookup "
                    "navigation failed (%s) — falling back to in-grid "
                    "search.", exc)
            else:
                await page.wait_for_timeout(1500)
                candidate_frame = await _find_conf_frame()
                if candidate_frame is None:
                    logger.warning(
                        "book_appointment/patient_select: direct "
                        "calAddLookup navigation did not render the conf "
                        "form — falling back to in-grid search.")
                else:
                    # SAFETY (2026-07-05, corrected same day per HAR
                    # evidence): never trust the fast path's conf frame on
                    # faith — an invalid rowid can render a validation-error
                    # page that still happens to satisfy `form[name="conf"]`,
                    # or (worse) a different patient's chart. Verify OUR
                    # identity is actually present (acct OR name tokens —
                    # the conf form has no acct/name FIELD; the page
                    # identifies the patient by DISPLAYED NAME only) before
                    # treating this as the real conf form. Try both the
                    # caller's params name and the resolve stage's own
                    # resolved chart name (they can legitimately differ —
                    # see the acct-match-wins warning above).
                    resolved_last, _, resolved_first = (
                        resolved_patient_name.partition(",")
                    )
                    names_to_try = [
                        (last_name, first_name),
                        (resolved_last.strip(), resolved_first.strip()),
                    ]
                    matched, methods_tried = await _conf_frame_matches_identity(
                        candidate_frame, acct, names_to_try,
                    )
                    if matched:
                        conf_frame = candidate_frame
                        logger.info(
                            "patient_select: direct calAddLookup navigation "
                            "(HAR-faithful) — skipping in-grid search "
                            "(resolved_by=%s, acct=%s)", resolved_by, acct,
                        )
                    else:
                        logger.warning(
                            "book_appointment/patient_select: direct "
                            "calAddLookup conf form did NOT confirm our "
                            "patient's identity (acct=%s) — tried %s — "
                            "treating fast path as failed and falling back "
                            "to in-grid search (never submitting against "
                            "an unverified identity).",
                            acct, methods_tried,
                        )

        # ---- FALLBACK: in-grid name/acct search + anchor scan -----------
        # Skipped entirely when the fast path above already found the conf
        # form (conf_frame is not None). This block is otherwise byte-
        # identical to the pre-fast-path logic — only re-indented one level.
        if conf_frame is None:
            # ---- Read-only patient name search (POST addLookup) ---------
            # Identical in kind to search_patient — a name lookup, not a
            # write. Factored into a helper so the retry logic below (added
            # after a live intermittent-failure incident, 2026-07-05 — two
            # identical approvals for acct 22041163 failed at patient_select
            # while a third, minutes later, succeeded) can re-submit the
            # exact same search without duplicating it.
            async def _submit_patient_search() -> Optional[dict]:
                """Re-run the read-only psrch submit. Returns an error dict
                on failure (caller should propagate it), or None on
                success."""
                try:
                    search_last = last_name or ""
                    if search_last:
                        ln_input = await psrch_frame.query_selector('input[name="LastName"]')
                        if ln_input:
                            await psrch_frame.fill('input[name="LastName"]', search_last)
                        if first_name:
                            fn_input = await psrch_frame.query_selector('input[name="FirstName"]')
                            if fn_input:
                                await psrch_frame.fill('input[name="FirstName"]', first_name)
                    else:
                        # Acct-only fallback (no name given). The psrch form's
                        # account-number input is named "Account" (capital A) —
                        # HAR-verified: the form POSTs to addLookup with an
                        # `Account` param, never a lowercase `acct` (that token
                        # only appears as a calAddLookup QUERY-string key on the
                        # fast path, a different context). The old lowercase
                        # `acct` selector matched no element, so the acct-only
                        # search submitted empty and fell back to a name search.
                        # Try the HAR-correct name first; keep the legacy
                        # selector as an additive fallback (zero regression).
                        acct_input = (
                            await psrch_frame.query_selector('input[name="Account"]')
                            or await psrch_frame.query_selector('input[name="acct"]')
                        )
                        if acct_input:
                            acct_field = await acct_input.get_attribute("name") or "Account"
                            await psrch_frame.fill(f'input[name="{acct_field}"]', acct)
                    # Submit the search form (read-only). Prefer a named
                    # button.
                    submit_btn = (
                        await psrch_frame.query_selector('input[name="NameSearch"]')
                        or await psrch_frame.query_selector('input[type="submit"]')
                    )
                    if submit_btn:
                        await submit_btn.click()
                    else:
                        await psrch_frame.evaluate(
                            'document.forms["psrch"] && document.forms["psrch"].submit()'
                        )
                    await page.wait_for_timeout(2500)
                except Exception as exc:
                    return {"status": "error", "stage": "patient_lookup",
                            "error": f"patient lookup (addLookup) failed: {exc}"}
                return None

            lookup_err = await _submit_patient_search()
            if lookup_err is not None:
                return lookup_err

            # ---- Locate + verify the calAddLookup select link for OUR
            # patient. Intermittent-failure fix (2026-07-05): the Patient
            # View results can render a beat after the psrch POST completes
            # (or, on a slow/cold session, the search occasionally re-lands
            # on a login-adjacent intermediate page instead of the results
            # list). Both look identical to "the anchor scan just found
            # nothing" from here, so we (1) explicitly wait for at least one
            # calAddLookup anchor to exist before scanning, and (2) if zero
            # of the anchors present match our patient, re-submit the same
            # search and re-scan up to twice more before giving up. This
            # does not weaken the match guard itself — only a link whose
            # acct (and rowid, when known) matches the intended patient is
            # ever eligible to click.

            def _norm(value: str) -> str:
                # Whitespace/zero-tolerant normalization for acct/rowid
                # compare.
                v = (value or "").strip()
                stripped = v.lstrip("0")
                return stripped if stripped else v

            norm_acct = _norm(acct)
            norm_rowid = _norm(rowid)

            max_attempts = 3  # initial scan + up to 2 retries
            results_frame = None
            select_link = None
            total_anchors_seen = 0
            candidate_accts_seen = False
            last_page_url = ""
            last_page_title = ""

            for attempt in range(max_attempts):
                # Give the results list a chance to actually exist before we
                # scan for it — this is the core fix for the timing race.
                for frame in page.frames:
                    try:
                        await frame.wait_for_selector(
                            'a[href*="calAddLookup?rowid="]', timeout=8000
                        )
                    except Exception:
                        # Not fatal by itself — this frame may simply not be
                        # the one hosting the results (or the page hasn't
                        # rendered them yet); fall through to the scan
                        # below, which tolerates zero matches per-frame.
                        continue

                attempt_total_anchors = 0
                attempt_candidate_accts = False
                for frame in page.frames:
                    try:
                        links = await frame.query_selector_all(
                            'a[href*="calAddLookup?rowid="]'
                        )
                    except Exception:
                        links = []
                    if links:
                        try:
                            last_page_url = frame.url
                            last_page_title = await frame.title()
                        except Exception:
                            pass
                    attempt_total_anchors += len(links)
                    for link in links:
                        href = await link.get_attribute("href") or ""
                        href_rowid = ""
                        href_acct = ""
                        rm = re.search(r'rowid=([^&|]+)', href)
                        am = re.search(r'acct=([^&|]+)', href)
                        if rm:
                            href_rowid = rm.group(1)
                        if am:
                            href_acct = am.group(1)
                        if href_acct:
                            attempt_candidate_accts = True
                        # HARD verification: only a link whose acct OR rowid
                        # (whichever we know) matches the intended patient
                        # is eligible. Comparison is whitespace/leading-zero
                        # tolerant on both sides. This stays at least as
                        # strict as before: acct alone never sufficed
                        # previously either (rowid was AND-ed in when
                        # known) — we now accept EITHER a normalized acct
                        # match OR a normalized rowid match, which is safe
                        # because both fields independently identify the
                        # same patient record on this results page.
                        acct_match = bool(href_acct) and _norm(href_acct) == norm_acct
                        rowid_match = (
                            bool(rowid) and bool(href_rowid)
                            and _norm(href_rowid) == norm_rowid
                        )
                        if acct_match or rowid_match:
                            results_frame = frame
                            select_link = link
                            break
                    if select_link:
                        break

                total_anchors_seen = max(total_anchors_seen, attempt_total_anchors)
                candidate_accts_seen = candidate_accts_seen or attempt_candidate_accts

                if select_link is not None:
                    break

                if attempt < max_attempts - 1:
                    logger.warning(
                        "book_appointment/patient_select: no matching "
                        "calAddLookup anchor on attempt %d/%d (acct=%s, "
                        "anchors_found=%d) — re-submitting patient search "
                        "and retrying.",
                        attempt + 1, max_attempts, acct, attempt_total_anchors,
                    )
                    await page.wait_for_timeout(1000 + attempt * 500)  # backoff
                    lookup_err = await _submit_patient_search()
                    if lookup_err is not None:
                        return lookup_err

            if select_link is None:
                logger.warning(
                    "book_appointment/patient_select: giving up after %d "
                    "attempts — acct=%s, total_calAddLookup_anchors=%d, "
                    "any_acct_candidates_present=%s, page_url=%s, page_title=%s",
                    max_attempts, acct, total_anchors_seen, candidate_accts_seen,
                    last_page_url, last_page_title,
                )
                return {"status": "error", "stage": "patient_select",
                        "error": "no calAddLookup link matched the intended "
                                 "acct/rowid in the Patient View results — "
                                 "refusing to click a non-matching patient",
                        "acct": acct,
                        "diagnostics": {
                            "attempts": max_attempts,
                            "total_calAddLookup_anchors": total_anchors_seen,
                            "any_acct_candidates_present": candidate_accts_seen,
                            "page_url": last_page_url,
                            "page_title": last_page_title,
                        }}

            # ---- Click select link (GET) → conf form (action=bk_p) ------
            try:
                await select_link.click()
                await page.wait_for_timeout(2500)
            except Exception as exc:
                return {"status": "error", "stage": "conf_form",
                        "error": f"could not open conf form via calAddLookup: {exc}"}

            conf_frame = await _find_conf_frame()
            if conf_frame is None:
                return {"status": "error", "stage": "conf_form",
                        "error": "final booking form (name=conf) did not render"}

        _report("patient_selected", "Patient chart selected")

        # Re-read the rotating token from the conf frame's own URL.
        conf_token = self._session_token_from_url(conf_frame.url) or session_token

        # Resolve the form action (absolute bk_p URL with the LIVE token).
        try:
            form_action = await conf_frame.eval_on_selector(
                'form[name="conf"]', 'f => f.action'
            )
        except Exception:
            form_action = ""
        if not form_action and conf_token:
            form_action = f"{self.BASE_URL}/proxy.cgi/{conf_token}/bk_p"

        # ---- Serialize the LIVE conf form, then override ONLY the fields --
        # ---- a human actually touches (FORM-FAITHFUL, 2026-07-05 HAR) -----
        # We do NOT construct a synthetic field dict. Instead we read every
        # input/select/textarea inside form[name="conf"] EXACTLY as rendered
        # (browser checkbox semantics: only include a checkbox's name=value
        # when it is checked), then apply _apply_overrides() so FromDate/
        # FromTime1/ToDate/ToTime1/weekday checkboxes/off/TFORMCOUNT/Incident
        # (unless explicitly passed) all stay at the server's rendered
        # defaults — exactly what the HAR's successful human commit did.
        try:
            serialized_raw = await conf_frame.eval_on_selector_all(
                'form[name="conf"] input, form[name="conf"] select, '
                'form[name="conf"] textarea',
                """els => els.map(e => ({
                    name: e.name || '',
                    type: (e.type || e.tagName || '').toLowerCase(),
                    value: e.value,
                    checked: !!e.checked
                }))"""
            )
        except Exception as exc:
            return {"status": "error", "stage": "conf_form",
                    "error": f"could not serialize conf form: {exc}"}

        serialized: dict = {}
        for el in (serialized_raw or []):
            name = el.get("name") or ""
            if not name:
                continue  # unnamed controls are never submitted by a browser
            etype = (el.get("type") or "")
            if etype in ("checkbox", "radio"):
                if el.get("checked"):
                    serialized[name] = el.get("value", "")
                # unchecked -> browser omits it entirely; leave absent.
                continue
            serialized[name] = el.get("value", "")

        # incident is passed through as None unless the caller supplied a
        # non-default value, so _apply_overrides can tell "not passed" (keep
        # rendered) apart from "explicitly cleared" (incident="").
        incident_override = incident if incident else None
        fields = _apply_overrides(
            serialized, appt_type=appt_type, duration_min=duration_min,
            note=note, incident=incident_override,
        )

        prepared = {
            "status": "prepared",
            "action_url": form_action,
            "fields": fields,
            "slot": {"x": slot_x, "y": slot_y, "href": slot_href,
                     "text": slot_text},
            "acct": acct,
            "rowid": rowid,
            "resolved_by": resolved_by,  # "acct" | "name" | None (both given)
            "date_filter_applied": date_filter_applied,
            "warning": ("form-faithful payload per the 2026-07-05 HAR — NOT "
                        "yet submitted; no appointment created"),
        }

        # ================================================================
        # COMMIT GUARD — two independent locks, fail-closed.
        # ================================================================
        if not execute:
            # DEFAULT propose path: return the prepared form, submit nothing.
            return prepared

        # execute=True requested. Still blocked unless BOTH locks are open.
        if not (BOOKING_EXECUTE_ENABLED and confirm_unverified):
            return {
                "status": "execute_blocked",
                "reason": ("booking commit is unverified (HAR pending) and "
                           "disabled by default; requires "
                           "BOOKING_EXECUTE_ENABLED=True in source AND "
                           "confirm_unverified=True"),
                "flag_enabled": BOOKING_EXECUTE_ENABLED,
                "confirm_unverified": confirm_unverified,
                "prepared": prepared,
            }

        # Lock 3 (allowlist) — enforced AT the POST site: only test accts commit
        # unless the allowlist was widened via SVIGG_BOOKING_ALLOWLIST ("*" = any).
        if not ("*" in BOOKING_ALLOWED_ACCTS or str(acct) in BOOKING_ALLOWED_ACCTS):
            return {
                "status": "execute_blocked",
                "reason": (f"acct {acct!r} is not in BOOKING_ALLOWED_ACCTS — "
                           "commit is restricted to designated test accounts"),
                "allowed": sorted(BOOKING_ALLOWED_ACCTS),
                "prepared": prepared,
            }

        # Lock 4 (IDENTITY BIND) — additive, independent of the allowlist above.
        # The allowlist ("*" in production) says WHICH accounts MAY be written
        # to; it does NOT confirm this acct is the patient the caller asked to
        # book. An acct-number mix-up/typo can resolve to a REAL, allowlisted
        # chart for the wrong person. Before the ONLY POST, deterministically
        # confirm the resolved account's OWN name (and DOB, when both sides have
        # one) matches the requested identity — fail closed on a mismatch. This
        # reuses the resolve stage's already-read EMR record (resolved_patient_*
        # from search_patient) — no new EMR read path. It NEVER relaxes any lock
        # above; it can only ADD a refusal.
        _id_ok, _id_reason = _identity_matches(
            last_name, first_name, dob,
            resolved_patient_name, resolved_patient_dob,
        )
        if not _id_ok:
            logger.warning(
                "book_appointment: IDENTITY GUARD refused a commit for acct=%s "
                "(resolved_by=%s) — %s", acct, resolved_by, _id_reason,
            )
            # Best-effort proof even on a pre-write refusal: the page still
            # shows the resolve/search state the guard was looking at.
            # _capture_proof is guarded (skips cleanly if no page).
            proof_captured = await self._capture_proof(proof_path)
            return {
                "status": "identity_mismatch",
                "stage": "identity_guard",
                "error": _id_reason,
                "acct": acct,
                "prepared": prepared,
                "proof_captured": proof_captured,
            }

        # All four locks open — the ONLY path that POSTs bk_p. Per the
        # 2026-07-05 HAR we set ONLY the fields a human actually touches
        # (cpt00, Duration00, note00/Note, Incident-if-explicit) — the slot
        # cell (x/y) already bound the real day+time; we deliberately do NOT
        # write FromDate/FromTime1/ToDate/ToTime1 or any weekday checkbox (see
        # _apply_overrides / the conflict note in this method's docstring).
        try:
            # cpt00 is a <select> — must be selected in the DOM, not just
            # filled. A silent failure here books the WRONG appt type, so
            # read back the DOM value and abort before Submit on a mismatch.
            select_failures = {}
            try:
                await conf_frame.select_option('select[name="cpt00"]', appt_type)
            except Exception as exc:
                select_failures["cpt00"] = str(exc)
            try:
                _got = await conf_frame.eval_on_selector(
                    'select[name="cpt00"]', 'e => e.value')
            except Exception as exc:
                _got = None
                select_failures.setdefault("cpt00", f"readback failed: {exc}")
            if _got != appt_type:
                try:
                    _opts = await conf_frame.eval_on_selector(
                        'select[name="cpt00"]',
                        'e => Array.from(e.options).map(o => o.value)')
                except Exception:
                    _opts = []
                return {"status": "error", "stage": "form_fill",
                        "error": (f"cpt00 select reads back {_got!r} != requested "
                                  f"{appt_type!r} — aborting before Submit (would "
                                  f"have booked the wrong appointment type)"),
                        "select_failures": select_failures,
                        "available_options": _opts,
                        "prepared": prepared}
            await conf_frame.fill('input[name="Duration00"]', str(duration_min))
            try:
                await conf_frame.fill('input[name="note00"]', note)
            except Exception:
                pass
            try:
                await conf_frame.fill('input[name="Note"]', note)
            except Exception:
                pass
            if incident:
                try:
                    await conf_frame.check(f'input[name="Incident"][value="{incident}"]')
                except Exception:
                    pass
            await conf_frame.click('input[name="Submit"]')
            await page.wait_for_timeout(2500)
            _report("submitted", "Booking submitted")
            try:
                body = await conf_frame.content()
            except Exception:
                body = ""

            def _clean(h):
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()

            # ---- Callback-Table bounce (HAR/live-verified 2026-07-07) -----
            # The server re-presents (usually) the SAME conf form with this
            # message when a residual callback entry blocks the date; nothing
            # was created. Check this BEFORE the overbook branch since a
            # callback bounce is a harder failure (no retry mirrors it).
            if _is_callback_conflict(body):
                return {
                    "status": "callback_conflict",
                    "error": ("server rejected the booking: a residual "
                              "callback-list entry already exists for this "
                              "patient/date (usually left by a prior cancel "
                              "that populated AutoReserve — new cancels no "
                              "longer seed the callback table, see "
                              "cancel_appointment). Clear the patient's "
                              "callback entry in Svigg (Call List) or book a "
                              "different date."),
                    "response_url": conf_frame.url,
                    "response_excerpt": _clean(body)[:1500],
                    "submitted_fields": fields,
                    "prepared": prepared,
                }

            # ---- Overbook recovery (HAR-verified 2026-07-05) ---------------
            # The server re-presents the SAME conf form with an Overbook
            # submit control when the slot is over-allocated. The human's
            # verified recovery resubmits the IDENTICAL fields with
            # Submit=Overbook rather than editing anything.
            if _has_overbook_control(body):
                try:
                    controls = await conf_frame.eval_on_selector_all(
                        'input[type="submit"],input[type="button"],input[type="image"],button',
                        'els => els.map(e => ({name:e.name||"", value:e.value||e.alt||e.textContent||""}))'
                    )
                except Exception:
                    controls = []
                if not allow_overbook:
                    # ---- Bookability guidance (additive, 2026-07-05) --------
                    # A full slot must never be force-overbooked just because
                    # allow_overbook defaulted False — gather real
                    # alternatives (same-day open slots + other bookable
                    # days) so the dashboard can offer the human a choice
                    # instead of a dead end. Best-effort only: a probe
                    # failure here must NEVER crash this refusal or turn it
                    # into a success — it just means fewer/no alternatives
                    # are attached.
                    # NOTE (fixed 2026-07-05, live bug): each add.htm anchor's
                    # own TEXT is always the literal word "Add" (Svigg
                    # renders no time in the anchor label — see
                    # doctorcom-booking-contract.md §5) — reading
                    # .text_content() here is exactly the bug that made every
                    # entry in this list read "Add". Read each anchor's HREF
                    # instead and derive the real clock time from its y=
                    # row via the live-proven 15-min grid mapping
                    # (pair_open_anchors_to_times / _slot_y_to_time — same
                    # y=40==10:00AM fact `_time_to_slot_y` already relies on
                    # elsewhere in this file).
                    same_day_open_slots: list = []
                    try:
                        oneday_probe = await self._apply_oneday_filter(date) \
                            if date else {"applied": False, "book_frame": None}
                        probe_frame = oneday_probe.get("book_frame")
                        if oneday_probe.get("applied") and probe_frame is not None:
                            probe_links = probe_frame.locator(
                                'a[href*="add.htm?x="]')
                            probe_n = await probe_links.count()
                            probe_hrefs = []
                            for i in range(probe_n):
                                try:
                                    href = await probe_links.nth(i).get_attribute("href")
                                except Exception:
                                    continue
                                if href:
                                    probe_hrefs.append(href)
                            same_day_open_slots = pair_open_anchors_to_times(
                                probe_hrefs)
                    except Exception as exc:
                        logger.warning(
                            "book_appointment/slot_conflict: same-day "
                            "open-slot read failed for %s: %s", date, exc)
                        same_day_open_slots = []

                    other_days: list = []
                    other_days_error = None
                    if date:
                        try:
                            other_days = await self.find_bookable_days(
                                date, horizon=7, need=2)
                        except Exception as exc:
                            logger.warning(
                                "book_appointment/slot_conflict: "
                                "find_bookable_days failed for %s: %s",
                                date, exc)
                            other_days = []
                            other_days_error = (
                                f"could not probe other days: {exc}")

                    proof_captured = await self._capture_proof(proof_path)
                    return {
                        "status": "slot_conflict",
                        "error": ("slot full — retry with allow_overbook=true "
                                  "after human confirmation"),
                        "controls": controls,
                        "response_url": conf_frame.url,
                        "response_excerpt": _clean(body)[:1200],
                        "submitted_fields": fields,
                        "prepared": prepared,
                        "alternatives": _build_alternatives(
                            same_day_open_slots=same_day_open_slots,
                            other_days=other_days,
                            overbook_available=True,
                            other_days_error=other_days_error),
                        "proof_captured": proof_captured,
                    }
                # allow_overbook=True → mirror the human's flow: resubmit the
                # SAME serialized fields with Submit=Overbook.
                clicked = None
                for sel in ('input[value="Overbook" i]', 'input[value*="Overbook" i]',
                            'input[name="Overbook" i]', 'input[name*="Overbook" i]',
                            'button:has-text("Overbook")'):
                    try:
                        await conf_frame.click(sel, timeout=4000)
                        clicked = sel
                        break
                    except Exception:
                        continue
                await page.wait_for_timeout(2500)
                try:
                    body = await conf_frame.content()
                except Exception:
                    pass
                if _is_callback_conflict(body):
                    return {
                        "status": "callback_conflict",
                        "error": ("server rejected the overbook resubmit: a "
                                  "residual callback-list entry already exists "
                                  "for this patient/date. Clear the patient's "
                                  "callback entry in Svigg (Call List) or book "
                                  "a different date."),
                        "response_url": conf_frame.url,
                        "response_excerpt": _clean(body)[:1500],
                        "submitted_fields": fields,
                        "prepared": prepared,
                    }
                if not clicked:
                    # The Overbook control was seen but the click itself
                    # failed — nothing further was submitted; this is a
                    # genuine failure, not a success needing verification.
                    proof_captured = await self._capture_proof(proof_path)
                    return {
                        "status": "overbook_click_failed",
                        "overbook_control": clicked,
                        "controls": controls,
                        "response_url": conf_frame.url,
                        "response_excerpt": _clean(body)[:1500],
                        "submitted_fields": fields,
                        "prepared": prepared,
                        "proof_captured": proof_captured,
                    }

                # ---- Post-submit verification (fixed 2026-07-05, live -----
                # ---- incident: NEVER report "submitted_overbooked" as -----
                # ---- terminal success without checking the live grid) ----
                verify = await self._verify_booking_on_grid(
                    date=date, last_name=last_name)
                _report("grid_verify", "Verified on the live grid")
                if verify.get("verified"):
                    proof_captured = await self._capture_proof(proof_path)
                    return {
                        "status": "submitted_verified",
                        "overbook_control": clicked,
                        "controls": controls,
                        "response_url": conf_frame.url,
                        "response_excerpt": _clean(body)[:1500],
                        "submitted_fields": fields,
                        "prepared": prepared,
                        "verified_cell": verify.get("cell"),
                        "proof_captured": proof_captured,
                        "warning": ("forced overbook — booking verified on "
                                    "the live grid immediately after submit"),
                    }
                proof_captured = await self._capture_proof(proof_path)
                return {
                    "status": "submit_unconfirmed",
                    "overbook_control": clicked,
                    "controls": controls,
                    "response_url": conf_frame.url,
                    "response_excerpt": _clean(body)[:1500],
                    "submitted_fields": fields,
                    "prepared": prepared,
                    "evidence": verify.get("evidence"),
                    "proof_captured": proof_captured,
                    "warning": ("forced overbook submitted, but the patient "
                                "was NOT found on the live grid afterward — "
                                "re-check the calendar by hand before "
                                "assuming anything was created"),
                }

            # ---- Post-submit verification (fixed 2026-07-05, live incident:
            # ---- NEVER report "submitted" as terminal success without ------
            # ---- checking the live grid) -----------------------------------
            verify = await self._verify_booking_on_grid(
                date=date, last_name=last_name)
            _report("grid_verify", "Verified on the live grid")
            if verify.get("verified"):
                proof_captured = await self._capture_proof(proof_path)
                return {
                    "status": "submitted_verified",
                    "response_url": conf_frame.url,
                    "response_excerpt": _clean(body)[:1500],
                    "submitted_fields": fields,
                    "prepared": prepared,
                    "verified_cell": verify.get("cell"),
                    "proof_captured": proof_captured,
                    "warning": ("booking verified on the live grid "
                                "immediately after submit"),
                }
            proof_captured = await self._capture_proof(proof_path)
            return {
                "status": "submit_unconfirmed",
                "response_url": conf_frame.url,
                "response_excerpt": _clean(body)[:1500],
                "submitted_fields": fields,
                "prepared": prepared,
                "evidence": verify.get("evidence"),
                "proof_captured": proof_captured,
                "warning": ("appointment form was submitted, but the "
                            "patient was NOT found on the live grid "
                            "afterward — re-check the calendar by hand "
                            "before assuming anything was created"),
            }
        except Exception as exc:
            return {"status": "error", "stage": "submit",
                    "error": f"bk_p submit failed: {exc}", "prepared": prepared}

    @staticmethod
    def _time_to_slot_y(t: str) -> int:
        """Convert a start time to the book-grid 15-min row index (y).

        The grid is midnight-anchored in 15-minute rows; anchored live on
        2026-07-01: y=40 == 10:00AM. Accepts "10:00AM" / "10:00 am" / "14:30".
        """
        s = t.strip().upper().replace(" ", "")
        from datetime import datetime as _dt
        parsed = None
        for fmt in ("%I:%M%p", "%H:%M"):
            try:
                parsed = _dt.strptime(s, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"unparseable time {t!r} (want e.g. '10:00AM' or '14:30')")
        minutes = parsed.hour * 60 + parsed.minute
        if minutes % 15:
            raise ValueError(f"time {t!r} is not on a 15-minute grid row")
        return minutes // 15

    async def _resolve_rowid_for_chart(
        self, acct: str, last_name: str = "", first_name: str = "",
    ) -> dict:
        """Resolve the Svigg rowid for a chart-open, keyed by ACCT.

        The cancel flow needs rowid to open plist.htm?rowid&acct, but the app
        leaves rowid empty, so it is resolved live here. This mirrors
        book_appointment's proven acct-first resolve_patient stage: search by
        acct first (uses patientEntry.htm's acct field when present), and if
        that is inconclusive, run the bounded name-variant ladder, filtering
        EVERY result through _pick_acct_match/_choose_rowid_from_matches so a
        rowid is accepted ONLY on a unique acct match with a plausible id.

        The parsed last_name is used ONLY to widen the live search net (the
        variant ladder); it NEVER selects the rowid — acct does. This is
        deliberate: the app may pass a last_name ("Patients 1") that differs
        from the Svigg record's bound name ("patient"), so name is untrusted
        for selection.

        Returns the dict from _choose_rowid_from_matches (status one of
        "ok"/"no_match"/"ambiguous"/"no_rowid") plus "error" text on a live
        search failure. Fail-closed: any non-"ok" status means the caller must
        NOT open a chart / proceed with a delete.
        """
        # 1) acct-first search (defensive acct field in search_patient()).
        try:
            matches = await self.search_patient(acct=str(acct))
        except Exception as exc:  # noqa: BLE001
            return {"rowid": "", "acct": acct, "name": "",
                    "status": "error", "match_count": 0,
                    "error": f"acct search failed: {exc}"}
        chosen = _choose_rowid_from_matches(matches, acct)
        if chosen["status"] == "ok":
            return chosen

        # 2) name-variant ladder (only widens the search; acct still selects).
        #    Skip entirely when no name is available to search with.
        if last_name:
            for idx, (v_last, v_first) in enumerate(
                    _resolve_name_variants(last_name, first_name), start=1):
                if idx > 1:
                    await asyncio.sleep(0.5)  # don't hammer the portal
                try:
                    vmatches = await self.search_patient(v_last, v_first)
                except Exception as exc:  # noqa: BLE001
                    return {"rowid": "", "acct": acct, "name": "",
                            "status": "error", "match_count": 0,
                            "error": f"name search failed: {exc}"}
                cand = _choose_rowid_from_matches(vmatches, acct)
                if cand["status"] == "ok":
                    return cand
        # Nothing resolved to a unique acct match with a real rowid.
        return chosen

    async def cancel_appointment(
        self,
        *,
        acct: str,
        date: str,
        last_name: str = "",
        first_name: str = "",
        dob: str = "",
        time: str = "",
        appointment_ref: str = "",
        reason: str = "or",
        confirm: bool = False,
        progress_cb=None,
        proof_path: Optional[str] = None,
    ) -> dict:
        """Cancel a Svigg appointment for a designated TEST patient. DESTRUCTIVE.

        HAR-FAITHFUL ENCOUNTER-KEYED CANCEL (root-cause fix 2026-07-05).
        The previous implementation drove the book.htm grid-cell mre? edit
        dialog → input[name="Delete"] → a `cancel_p` confirm form. A HAR of a
        human's WORKING manual cancel (websrv01.physician-to-go.net, captured
        2026-07-05, done twice) proved that path DID NOT PERSIST: neither
        `mre?` nor `cancel_p` appears anywhere in the working capture (0 hits
        each), and the appointment stayed on the schedule after the app
        "cancelled". The persisting delete is keyed on the appointment's
        ENCOUNTER id (enc) and runs as plain GET requests under the per-session
        /proxy.cgi/<SESSION>/ path:
          1. resched.htm?enc=<ENC>                     (reschedule/cancel dialog)
          2. resched_p.htm?TFORMCOUNT=<N>&Note1=&Delete=Delete   (Delete confirm)
          3. cancel2_p?TFORMCOUNT=<N+1>&CancelReason=<or|pr>&Yes=Yes  (COMMIT)
        where CancelReason "or"=Office Requested, "pr"=Patient Requested, and
        TFORMCOUNT is a SESSION-GLOBAL incrementing form counter that is NOT
        computable — it is read live from each rendered form (observed 3→4 for
        one appt, 13→14 for another). The <SESSION> token (9 digits) is minted
        on the patient chart's appointment-list page and read from its body
        (never fabricated). enc is resolved from the per-day SCHEDULE REPORT
        (get_schedule_day / appt_b.htm rows carry appt_e.htm?...&enc=<ENC>),
        bound by last_name + date (+ time when given).

        AutoReserve: the HAR-faithful cancel2_p commit URL carries NO
        AutoReserve param at all (the human's working cancel is exactly
        TFORMCOUNT + CancelReason + Yes), so there is nothing to neutralize on
        this path. result["auto_reserve"] == "not_applicable_har_path". (The
        old Callback-Table residue came from the cancel_p form's AutoReserve
        field, which this path never touches.)

        SAFETY (fail-closed, checked BEFORE any destructive request):
          returns {status:"execute_blocked"} unless confirm is True AND
          str(acct) is in the cancel allowlist AND last_name matches that
          acct's bound name. The appt is disambiguated to exactly one row and
          the encounter id is resolved+ambiguity-checked BEFORE the gate is
          re-affirmed. The patient chart page must show the target last_name
          before any resched link is trusted; if the session-tokened
          resched.htm?enc link, the resched TFORMCOUNT, the Delete-confirm
          page's cancel2_p/CancelReason control, or the commit TFORMCOUNT
          cannot be read, the flow FAILS CLOSED (never blind-submits a delete).

        DATE FILTER (fixed 2026-07-05, live-proven ONE-DAY mode): the grid is
        located via `get_appointment_calendar(date)`, which now applies
        `_apply_oneday_filter` (Svigg's OneDay checkbox + `thismon` click)
        instead of the old fill-dt + click-GO calfilt_p pattern — the old
        pattern was live-proven UNRELIABLE (a cancel refused with "date
        filter not applied" the night this fix was made). The
        "date filter not applied" refusal below now keys off that helper's
        confirmed `applied` flag (every row from get_appointment_calendar
        carries `date_filter_applied` reflecting it), and mre cell labels are
        matched via `_parse_mre_label` (fixes &nbsp;/\\xa0 in multi-word last
        names, e.g. "Patients 1").

        The book.htm ONE-DAY grid (get_appointment_calendar(date)) is still
        used ONLY to locate + disambiguate the appointment cell for the gate's
        name/date/ambiguity binding and for the post-cancel re-read verify; the
        ACTUAL delete no longer touches that grid's mre? cell — it goes through
        the encounter path above.

        Args:
            acct: numeric patient account (gate key — must be allowlisted).
            date: appointment date, ISO YYYY-MM-DD (passed to
                get_appointment_calendar for the ONE-DAY grid filter).
            last_name/first_name: used to locate the appt cell on the grid
                (cell text is "(LEN)&nbsp;LastName,&nbsp;FirstInit&nbsp;/Type",
                matched via _parse_mre_label) and for the identity guard on
                the mre form.
            time: optional appointment start time, e.g. "10:00AM" or "14:30",
                used to disambiguate when the patient has multiple appointments
                that day — converted to the book-grid 15-min row index.
            appointment_ref: exact Svigg cell ref (the r= value returned by
                get_appointment_calendar) — takes precedence over time.
            reason: CancelReason select value — "or" (Office Requested,
                default) or "pr" (Patient Requested).
            confirm: must be explicitly True to perform the cancel.
            progress_cb: optional ``callable(stage_key: str, label: str)``
                invoked at natural stage boundaries (day opened, appointment
                found, identity confirmed, submitted, removal confirmed) —
                see ``book_appointment``'s ``progress_cb`` for the same
                contract (called synchronously, guarded try/except, never
                affects the cancel flow).
            proof_path: optional filesystem path (FEATURE 8 — proof capture),
                same contract as ``book_appointment``'s ``proof_path``: a
                best-effort screenshot of the one-day grid taken during the
                post-cancel verification step, never affecting the cancel
                flow or its result. Surfaced via ``result["proof_captured"]``.

        Returns one of:
          {status:"not_found", ...}         appt not on that day's grid
          {status:"ambiguous", candidates:[...]}  multiple appts match — refuse
          {status:"execute_blocked", ...}   gate closed (confirm/allowlist)
          {status:"cancelled", verified, remaining_test_rows, auto_reserve, ...}
              auto_reserve: "not_applicable_har_path" — the HAR-faithful
              cancel2_p commit carries no AutoReserve param (see note above).
          {status:"error", stage, error}   fail-closed refusal (incl. no enc,
              no session-tokened resched link, or an unreadable TFORMCOUNT)
        """
        page = self._page

        def _report(stage_key: str, label: str) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(stage_key, label)
            except Exception:
                pass  # a progress-callback bug must never affect the flow

        if reason not in ("or", "pr"):
            return {"status": "error", "stage": "args",
                    "error": f"reason must be 'or' or 'pr', got {reason!r}"}
        if not last_name:
            return {"status": "error", "stage": "args",
                    "error": "last_name is required to locate the appointment "
                             "cell on the calendar grid"}
        target_y = None
        if time:
            try:
                target_y = self._time_to_slot_y(time)
            except ValueError as exc:
                return {"status": "error", "stage": "args", "error": str(exc)}

        # Accept any stray JS confirm() the portal may raise around Delete.
        def _on_dialog(d):
            asyncio.ensure_future(d.accept())
        page.on("dialog", _on_dialog)

        try:
            # ---- 1. Navigate the calendar to the target date -------------
            cal = await self.get_appointment_calendar(date)
            if cal and isinstance(cal[0], dict) and cal[0].get("error"):
                return {"status": "error", "stage": "calendar",
                        "error": cal[0]["error"]}
            # H1 guard: get_appointment_calendar silently falls back to TODAY's
            # grid if the date filter can't be applied. Refuse to locate/cancel
            # unless the requested date was actually applied.
            if cal and not any(isinstance(a, dict) and a.get("date_filter_applied")
                               for a in cal):
                return {"status": "error", "stage": "calendar_date",
                        "error": (f"date filter for {date} was not applied "
                                  "(calendar fell back to default view) — refusing "
                                  "to cancel against a possibly-wrong day")}
            _report("day_opened", "Day opened")

            def _matches(appt: dict) -> bool:
                name = str(appt.get("patient_name", "")).lower()
                if last_name.lower() not in name:
                    return False
                if first_name:
                    # Cell text is "LastName, FirstInit" — anchor the initial to
                    # the comma instead of matching it anywhere in the string.
                    im = re.search(r',\s*([a-z])', name)
                    if im:
                        return im.group(1) == first_name[0].lower()
                    return first_name[0].lower() in name.replace(last_name.lower(), "")
                return True

            before_rows = [a for a in cal if isinstance(a, dict) and _matches(a)]
            if not before_rows:
                # Disambiguation aid: a compact snapshot of who ELSE is on
                # this day's calendar (capped at 25 rows) — lets the caller
                # (approvals._execute_cancel) tell staff who IS on the
                # schedule instead of a bare "not found", without exposing
                # the full raw calendar payload.
                day_rows = [
                    {"patient_name": a.get("patient_name"),
                     "appointment_type": a.get("appointment_type"),
                     "status": a.get("status")}
                    for a in cal if isinstance(a, dict) and a.get("patient_name")
                ][:25]
                proof_captured = await self._capture_proof(proof_path)
                return {"status": "not_found", "date": date,
                        "detail": f"no calendar cell matching {last_name!r} "
                                  f"on {date}",
                        "calendar_rows": len(cal),
                        "day_rows": day_rows,
                        "proof_captured": proof_captured}

            # ---- 1b. Disambiguate to a single appt BEFORE the gate --------
            # A name can match several same-day rows; pin down exactly one via
            # appointment_ref (exact) or time (grid row) — refuse to guess.
            candidates = before_rows
            if appointment_ref:
                candidates = [a for a in candidates
                              if str(a.get("appointment_ref", "")) == str(appointment_ref)]
            elif time:
                candidates = [a for a in candidates
                              if str(a.get("cell_y", "")) == str(target_y)]
            if not candidates:
                proof_captured = await self._capture_proof(proof_path)
                return {"status": "not_found", "date": date,
                        "detail": (f"{len(before_rows)} calendar row(s) match "
                                   f"{last_name!r} but none match the requested "
                                   f"slot (time={time!r}, appointment_ref={appointment_ref!r})"),
                        "slots": [{k: a.get(k) for k in ("cell_x", "cell_y", "appointment_ref", "appointment_type", "status")} for a in before_rows],
                        "proof_captured": proof_captured}
            if len(candidates) > 1:
                return {"status": "ambiguous", "date": date,
                        "detail": ("multiple appointments match — pass time=... or "
                                   "appointment_ref=... to pick exactly one; refusing to guess"),
                        "candidates": [{k: a.get(k) for k in ("cell_x", "cell_y", "appointment_ref", "appointment_type", "status", "duration_minutes")} for a in candidates]}
            target = candidates[0]
            _report("appt_found", "Appointment found")

            # ---- 2. GATE — fail-closed, BEFORE opening/deleting anything --
            # The appt is located by NAME, so binding to the allowlisted acct
            # alone is insufficient (acct is a trust-me token here). ALSO require
            # the caller's last_name to match the acct's bound name, so an
            # allowlisted acct + a real patient's name is rejected.
            bound_name = CANCEL_ALLOWED_ACCT_NAMES.get(str(acct))
            name_ok = (bound_name is not None and bound_name in last_name.lower()) \
                or (CANCEL_ALLOW_ANY and bool(last_name))
            if not (confirm is True and name_ok):
                return {
                    "status": "execute_blocked",
                    "reason": ("cancel is DESTRUCTIVE and fail-closed: requires "
                               "confirm=True AND acct in the cancel allowlist AND "
                               "last_name matching that acct's bound name "
                               f"(confirm={confirm}, acct_allowed="
                               f"{bound_name is not None}, name_ok={name_ok})"),
                    "allowed_accts": sorted(CANCEL_ALLOWED_ACCT_NAMES),
                    "found_rows": len(before_rows),
                    "date": date,
                }

            # ---- 3. Resolve the appointment's ENCOUNTER id (enc) ----------
            # ROOT-CAUSE FIX (2026-07-05): the previous delete drove the
            # book.htm grid-cell mre? edit dialog -> input[name="Delete"] ->
            # a `cancel_p` confirm form. The 2026-07-05 HAR of a human's WORKING
            # manual cancel proved that path DOES NOT PERSIST — neither `mre?`
            # nor `cancel_p` appears anywhere in the working capture (0 hits
            # each). The persisting delete is keyed on the appointment's
            # ENCOUNTER id and runs entirely as GET requests:
            #   resched.htm?enc=<ENC>
            #   -> resched_p.htm?TFORMCOUNT=<N>&Note1=&Delete=Delete
            #   -> cancel2_p?TFORMCOUNT=<N+1>&CancelReason=<or|pr>&Yes=Yes
            # We resolve enc from the per-day SCHEDULE REPORT (appt_b.htm, which
            # get_schedule_day already scrapes and whose rows carry
            # appt_e.htm?...&enc=<ENC>), binding by patient last_name + date
            # (+ time when given). Ambiguity or no-match -> fail closed.
            reason_code = _map_cancel_reason(reason)  # validated already, re-affirm
            from datetime import date as _date_cls
            try:
                _want = _date_cls.fromisoformat(date)
                _today = _date_cls.today()
                day_offset = (_want - _today).days
            except Exception as exc:
                return {"status": "error", "stage": "enc_resolve",
                        "error": f"cannot compute day offset for {date!r}: {exc}"}

            sched = await self.get_schedule_day(day_offset)
            if sched.get("error"):
                return {"status": "error", "stage": "enc_resolve",
                        "error": (f"schedule report fetch failed: "
                                  f"{sched.get('error')}")}
            # Fail closed if the report's own date does not match the request
            # (never resolve an enc against the wrong day).
            if sched.get("date") and sched.get("date") != date:
                return {"status": "error", "stage": "enc_resolve_date",
                        "error": (f"schedule report returned date "
                                  f"{sched.get('date')!r} for offset {day_offset} "
                                  f"but {date!r} was requested — refusing to "
                                  f"resolve an encounter against the wrong day")}
            # Resolve enc candidates from the rows get_schedule_day already
            # parsed (each row carries encounter_id from its appt_e.htm?enc link
            # — reuses that live-verified extraction rather than re-parsing HTML).
            # Bind by last_name + time; the date is already pinned to this day's
            # report above. Same last-name/time/ambiguity contract as the pure
            # _extract_encs_from_schedule helper (unit-tested in
            # tests_svigg_contract.py section 12c).
            enc_candidates = []
            _seen_enc = set()
            for row in sched.get("appointments", []):
                if not isinstance(row, dict):
                    continue
                enc = str(row.get("encounter_id") or "").strip()
                if not enc or enc in _seen_enc:
                    continue
                rname = str(row.get("patient_name", "")).lower()
                if last_name.lower() not in rname:
                    continue
                if not _enc_matches_time(str(row.get("start_time", "")), time):
                    continue
                _seen_enc.add(enc)
                enc_candidates.append({"enc": enc,
                                       "time": row.get("start_time", ""),
                                       "name": row.get("patient_name", "")})
            if not enc_candidates:
                proof_captured = await self._capture_proof(proof_path)
                return {"status": "not_found", "date": date,
                        "detail": (f"no encounter id on the {date} schedule "
                                   f"report matching {last_name!r}"
                                   f"{(' at ' + time) if time else ''}"),
                        "schedule_rows": len(sched.get("appointments", [])),
                        "proof_captured": proof_captured}
            if len(enc_candidates) > 1:
                return {"status": "ambiguous", "date": date,
                        "detail": ("multiple same-day encounters match — pass "
                                   "time=... to pick exactly one; refusing to "
                                   "guess which appointment to delete"),
                        "candidates": [{"time": c["time"]} for c in enc_candidates]}
            target_enc = enc_candidates[0]["enc"]
            _report("identity_confirmed", "Encounter resolved")

            # ---- 4. Open the patient chart -> read the session-tokened
            #         resched.htm?enc link (the 9-digit <SESSION> is minted on
            #         the chart's appointment-list page; it is NOT computable,
            #         it must be read from the rendered page). Fail closed if
            #         the resched link for our enc is not present.
            #
            # Resolve the chart rowid LIVE, keyed by ACCT (the app leaves rowid
            # empty). _resolve_rowid_for_chart mirrors book_appointment's proven
            # acct-first resolve: acct search, then a name-variant ladder, with
            # EVERY result filtered to a unique acct match (never selected by
            # the parsed last_name, which can differ from the Svigg record's
            # bound name — e.g. acct 22041163 is passed as "Patients 1" but the
            # record is "patient"). Fail closed on 0/>1 matches or a bogus rowid.
            resolved = await self._resolve_rowid_for_chart(
                str(acct), last_name, first_name)
            rowid = resolved.get("rowid", "")
            if resolved.get("status") != "ok" or not rowid:
                _rstat = resolved.get("status")
                _detail = {
                    "no_match": "no patient chart matched that acct",
                    "ambiguous": (f"{resolved.get('match_count')} charts matched "
                                  "that acct — refusing to guess which"),
                    "no_rowid": "the acct match carried no usable rowid",
                    "error": resolved.get("error", "live patient search failed"),
                }.get(_rstat, f"unresolved ({_rstat})")
                return {"status": "error", "stage": "chart_open",
                        "error": (f"could not resolve a rowid for acct {acct} "
                                  f"to open the patient chart — {_detail}; "
                                  "ABORTED before any delete"),
                        "resolve_status": _rstat,
                        "acct_match_count": resolved.get("match_count")}

            # IDENTITY BIND (deterministic, additive) — the acct is a trust-me
            # token that could be a typo landing on a real chart for the WRONG
            # person. Before ANY delete, confirm the acct-resolved chart's OWN
            # name (and DOB when both sides have one) matches the requested
            # identity. This reuses the record _resolve_rowid_for_chart already
            # read (no new EMR read path) and is INDEPENDENT of the allowlist
            # name-token bind above (which "*"/CANCEL_ALLOW_ANY drops) and of
            # the schedule-report substring bind (a loose "in" check) — it can
            # only ADD a refusal, never relax a gate. Fail closed on mismatch.
            _id_ok, _id_reason = _identity_matches(
                last_name, first_name, dob,
                resolved.get("name", ""), resolved.get("dob", ""),
            )
            if not _id_ok:
                logger.warning(
                    "cancel_appointment: IDENTITY GUARD refused a delete for "
                    "acct=%s — %s", acct, _id_reason,
                )
                # Best-effort proof on a pre-delete refusal: the page shows
                # the resolve/search state (guarded — skips if no page).
                proof_captured = await self._capture_proof(proof_path)
                return {"status": "identity_mismatch", "stage": "identity_guard",
                        "error": _id_reason, "acct": acct, "date": date,
                        "proof_captured": proof_captured}

            # Navigate the encounter-module chart page that carries the resched
            # links (bare path 302s into the rotating /proxy.cgi/<SESSION>/ path;
            # the redirect is followed automatically — same cookie-authed family
            # as get_problem_list).
            plist_url = (f"{self.BASE_URL}/proxy.cgi/apps/enc/plist.htm?"
                         f"{urlencode({'acct': str(acct), 'rowid': rowid})}")
            try:
                await self._goto(plist_url, wait_until="networkidle",
                                 timeout=20000)
                await page.wait_for_timeout(1000)
                plist_html = await page.content()
            except Exception as exc:
                return {"status": "error", "stage": "chart_open",
                        "error": f"patient chart plist fetch failed: {exc}"}

            # Identity re-bind on the chart page: it MUST positively identify
            # the acct-resolved patient before we trust any resched link on it.
            # We opened plist by the ACCT-matched rowid, so the page belongs to
            # that acct; require it to SHOW at least one trusted identity token:
            #   - the acct number itself, OR
            #   - the resolved record's OWN last name (from the acct match), OR
            #   - the caller's last_name.
            # The caller's last_name alone is NOT sufficient as the sole check:
            # the app can pass a last_name ("Patients 1") that differs from the
            # Svigg record's bound name ("patient"), which would false-negative
            # a correctly-resolved chart. The acct + resolved name are the
            # reliable bindings (this mirrors why the rowid was chosen by acct).
            _hay = (plist_html or "").lower()
            _resolved_last = ""
            _rn = resolved.get("name", "")
            if _rn:
                # search_patient names render "Last, First"; take the last-name
                # side for a substring bind (it is what the chart page prints).
                _resolved_last = _rn.split(",", 1)[0].strip().lower()
            _identity_tokens = [t for t in (
                str(acct).strip().lower(), _normalize_acct(str(acct)),
                _resolved_last, last_name.lower())
                if t]
            if not any(tok and tok in _hay for tok in _identity_tokens):
                return {"status": "error", "stage": "identity_guard",
                        "error": ("patient chart page did not show the acct-"
                                  "resolved patient's acct or name — ABORTED "
                                  "before any delete")}

            resched = _extract_resched_link(plist_html, target_enc)
            if not resched:
                return {"status": "error", "stage": "resched_link",
                        "error": (f"no session-tokened resched.htm?enc={target_enc} "
                                  "link found on the patient chart page — cannot "
                                  "perform the persisting cancel; ABORTED (the old "
                                  "grid-cell delete path is intentionally removed "
                                  "because it did not persist)")}
            session = resched["session"]
            _report("identity_confirmed", "Identity confirmed")

            # ---- 5. resched.htm -> resched_p (Delete) -> cancel2_p (Yes) ---
            # Step 5a: open resched.htm?enc and read the session-global
            # TFORMCOUNT the server rendered into its form.
            try:
                await self._goto(f"{self.BASE_URL}{resched['path']}",
                                 wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(800)
                resched_html = await page.content()
            except Exception as exc:
                return {"status": "error", "stage": "resched_open",
                        "error": f"resched.htm?enc={target_enc} fetch failed: {exc}"}
            tfc_delete = _extract_tformcount(resched_html)
            if not tfc_delete:
                return {"status": "error", "stage": "resched_tformcount",
                        "error": ("could not read TFORMCOUNT from the resched.htm "
                                  "form — refusing to submit a Delete without the "
                                  "session-global form counter (fail-closed)")}

            # Step 5b: GET resched_p.htm?...&Delete=Delete (Delete-confirm page).
            try:
                del_url = _build_resched_p_delete_url(
                    self.BASE_URL, session, tfc_delete)
            except ValueError as exc:
                return {"status": "error", "stage": "resched_p_build",
                        "error": str(exc)}
            try:
                await self._goto(del_url, wait_until="networkidle",
                                 timeout=20000)
                await page.wait_for_timeout(800)
                confirm_html = await page.content()
            except Exception as exc:
                return {"status": "error", "stage": "resched_p_delete",
                        "error": f"resched_p Delete step failed: {exc}"}

            # The Delete-confirm page MUST offer the cancel2_p commit with a
            # CancelReason control; if it does not, the appointment was NOT
            # staged for deletion — fail closed rather than guess.
            if "cancel2_p" not in confirm_html and "CancelReason" not in confirm_html:
                return {"status": "error", "stage": "confirm_page",
                        "error": ("Delete-confirmation page did not present the "
                                  "cancel2_p / CancelReason commit — the delete "
                                  "was NOT staged; ABORTED (nothing committed)")}
            # Step 5c: read the NEXT session-global TFORMCOUNT the confirm page
            # rendered (observed N+1 vs the Delete step) for the cancel2_p GET.
            tfc_commit = _extract_tformcount(confirm_html)
            if not tfc_commit:
                return {"status": "error", "stage": "cancel2_tformcount",
                        "error": ("could not read TFORMCOUNT from the Delete-"
                                  "confirmation page — refusing to submit the "
                                  "cancel2_p commit without it (fail-closed)")}

            # AutoReserve: the HAR-faithful cancel2_p commit URL carries NO
            # AutoReserve param at all (the human's working cancel is exactly
            # TFORMCOUNT + CancelReason + Yes), so there is no populated
            # AutoReserve to neutralize on this path — recorded honestly.
            auto_reserve_result = "not_applicable_har_path"

            # Step 5d: GET cancel2_p?...&CancelReason=<or|pr>&Yes=Yes — COMMIT.
            try:
                commit_url = _build_cancel2_p_url(
                    self.BASE_URL, session, tfc_commit, reason_code)
            except ValueError as exc:
                return {"status": "error", "stage": "cancel2_p_build",
                        "error": str(exc)}
            try:
                await self._goto(commit_url, wait_until="networkidle",
                                 timeout=20000)
                await page.wait_for_timeout(3000)
            except Exception as exc:
                return {"status": "error", "stage": "cancel2_p_commit",
                        "error": f"cancel2_p commit failed: {exc}"}
            _report("cancel_submitted", "Cancellation submitted")

            # ---- 6. Verify by re-reading the calendar ---------------------
            # Keep the name-match counts (rows_before/remaining_test_rows) for
            # context, but verify against the SAME slot filter we cancelled, so
            # a different same-day appt for this patient can't mask success.
            after_meta: dict = {}
            cal_after = await self.get_appointment_calendar(date, meta=after_meta)
            # The re-read can hard-error or silently fall back to an unfiltered
            # grid — never count either as proof the cancel worked. But a
            # filter-CONFIRMED empty day IS proof: it is exactly the successful
            # single-appt cancel (the day's only row was just deleted, so the
            # date_filter_applied marker that rides on rows is gone BECAUSE the
            # cancel worked). Trust the authoritative flag from `meta`, which is
            # set even on an empty grid, instead of scanning now-absent rows.
            reread_ok = _cancel_reread_ok(
                cal_after, bool(after_meta.get("date_filter_applied")))
            after_rows = [a for a in cal_after
                          if isinstance(a, dict) and _matches(a)]
            candidates_after = after_rows
            if appointment_ref:
                candidates_after = [a for a in candidates_after
                                    if str(a.get("appointment_ref", "")) == str(appointment_ref)]
            elif time:
                candidates_after = [a for a in candidates_after
                                    if str(a.get("cell_y", "")) == str(target_y)]
            verified = reread_ok and len(candidates_after) < len(candidates)
            _report("removal_confirmed", "Removal confirmed")

            if not reread_ok:
                warning = ("post-cancel calendar re-read failed or lost the date "
                           "filter — cancel was submitted but could NOT be "
                           "verified; check manually")
            elif not verified:
                warning = ("cancel submitted but calendar re-read still shows "
                           "a matching row — verify manually")
            else:
                warning = ""

            proof_captured = await self._capture_proof(proof_path) \
                if verified else False
            return {
                "status": "cancelled",
                "verified": verified,
                "date": date,
                "reason": reason,
                "rows_before": len(before_rows),
                "remaining_test_rows": len(after_rows),
                "slot": {"cell_x": target.get("cell_x"),
                         "cell_y": target.get("cell_y"),
                         "appointment_ref": target.get("appointment_ref")},
                "auto_reserve": auto_reserve_result,
                "proof_captured": proof_captured,
                "warning": warning,
            }
        except Exception as exc:
            # Best-effort proof of the EMR state at the moment of failure.
            try:
                proof_captured = await self._capture_proof(proof_path)
            except Exception:
                proof_captured = False
            return {"status": "error", "stage": "cancel_flow",
                    "error": f"{exc}", "proof_captured": proof_captured}
        finally:
            try:
                page.remove_listener("dialog", _on_dialog)
            except Exception:
                pass

    async def search_and_summarize(self, last_name: str, first_name: str = "") -> list[dict]:
        """
        Search for patients and return full summaries for each match.
        Combines search_patient + get_patient_summary for convenience.
        """
        patients = await self.search_patient(last_name, first_name)

        summaries = []
        for p in patients[:10]:  # Cap at 10 to avoid portal hammering
            if p.get("rowid") and p.get("acct"):
                try:
                    summary = await self.get_patient_summary(p["rowid"], p["acct"])
                    summaries.append(summary)
                except Exception as e:
                    logger.warning(f"Failed to get summary for {p.get('name')}: {e}")
                    summaries.append(p)  # Fall back to search result

        return summaries if summaries else patients

    # ------------------------------------------------------------------
    # NEW-PATIENT CREATE
    # ------------------------------------------------------------------
    # Demographic keys accepted by create_patient(). Only last_name +
    # first_name are required; everything else is optional and rendered as an
    # honest blank (never fabricated) when absent. dob/ssn are the two fields
    # the name-search de-dupe step also consumes.
    _CREATE_DEMOGRAPHIC_KEYS = (
        "last_name", "first_name", "mi", "dob", "ssn",
        "sex", "address", "address2", "city", "state", "zip",
        "home_phone", "cell_phone", "work_phone", "email",
    )

    # ------------------------------------------------------------------
    # EXISTING-PATIENT EDIT — demographic-field mapping
    # ------------------------------------------------------------------
    # Maps update_patient()'s caller-facing field keys onto the REAL Svigg
    # edit-form input names (HAR-confirmed on the base
    # websrv01.physician-to-go.net.har edit-save POST: TFORMCOUNT, imageField.x,
    # imageField.y, LastName, FirstName, MI, Prefix, Suffix, SocSecNo, Chart,
    # StreetAddrs1, StreetAddrs2, City, State, Zip, BirthDate, Number (home),
    # Email, WorkNumber, WorkExt, CellNumber, ReferralSource, Lang1, Lang2,
    # Race, EthnicOrigin, …). For "add a phone number": cell -> CellNumber,
    # home -> Number, work -> WorkNumber. Every value here is a real form name
    # seen on the wire; nothing is invented. Any caller key NOT in this map is
    # reported back as unmapped (never silently dropped, never guessed onto a
    # field). This is the ONLY set of fields update_patient will ever touch —
    # it leaves all other pre-filled inputs at their rendered values.
    _EDIT_FIELD_MAP = {
        "last_name": "LastName",
        "first_name": "FirstName",
        "mi": "MI",
        "prefix": "Prefix",
        "suffix": "Suffix",
        "ssn": "SocSecNo",
        "chart": "Chart",
        "address1": "StreetAddrs1",
        "address": "StreetAddrs1",      # alias for address1
        "address2": "StreetAddrs2",
        "city": "City",
        "state": "State",
        "zip": "Zip",
        "dob": "BirthDate",
        "home_phone": "Number",
        "cell": "CellNumber",
        "cell_phone": "CellNumber",     # alias for cell
        "work_phone": "WorkNumber",
        "work_ext": "WorkExt",
        "email": "Email",
        "work_email": "WorkEmail",
        "referral_source": "ReferralSource",
    }

    async def create_patient(
        self,
        demographics: dict,
        *,
        dry_run: bool = True,
        confirm_unverified: bool = False,
    ) -> dict:
        """Create a NEW patient chart in Svigg/Dr.Com — DRY-RUN by default.

        SAFETY: with dry_run=True (the DEFAULT) this walks the live entry flow
        to the ADD form, DISCOVERS the add form's real input fields from the
        rendered DOM, and returns the fields it WOULD submit WITHOUT clicking
        Save. It CREATES NOTHING. The commit path (dry_run=False) is
        double-gated (CREATE_EXECUTE_ENABLED + confirm_unverified). The SAVE
        POST contract IS HAR-confirmed (base HAR: POST pentry.htm, Referer
        patientEntry_add.htm, ~53 urlencoded params, submit trigger NextTab)
        but comes from a SINGLE capture and has never been live-tested, so the
        commit path fails closed unless it can positively identify the exact
        HAR-confirmed NextTab submit control, and — critically — NEVER claims
        success from an HTTP 200: after saving it re-searches Svigg for the
        patient and returns "created_verified" ONLY on a positive re-read, else
        "created_unverified" with a loud warning.

        Flow (all read-only until the final Save on the commit path):
          login (reused; caller must have called login() OR we verify here)
            -> GET /proxy.cgi/off/maint/patientEntry_new.htm   (new-patient form)
            -> fill LastName/FirstName/MI/BirthDate/SocSecNo/Account, rtype=r
            -> POST patientEntry_new.htm  (name-search DE-DUPE — read-only)
                 * if the de-dupe surfaces an existing pentry.htm?rowid= match
                   for the same name+DOB, we STOP and report it (idempotency:
                   never double-create).
            -> GET /proxy.cgi/off/maint/patientEntry_add.htm   (the ADD form)
            -> DISCOVER add-form fields from the DOM, map demographics onto them
            -> [dry_run] STOP + return discovered fields + would-submit payload
            -> [commit]  triple-gated Save: fill demographics, click the
                 HAR-confirmed NextTab submit (POST pentry.htm), then VERIFY by
                 an independent patient re-search before ever claiming success

        Args:
            demographics: dict with at least ``last_name`` + ``first_name``.
                Optional: mi, dob (MM/DD/YYYY), ssn, sex, address, address2,
                city, state, zip, home_phone, cell_phone, work_phone, email.
                Missing optional values render as "" (honest blank) — never
                invented.
            dry_run: True (DEFAULT) => discover + propose, submit nothing.
                False => attempt the gated, unverified Save.
            confirm_unverified: caller's explicit acknowledgement that the Save
                contract is unverified. Required (with the module flag) to ever
                attempt a Save.

        Returns one of:
          {status:"prepared", add_form_url, discovered_fields, would_submit,
              mapped, unmapped_demographics, dedupe, warning}   (dry-run)
          {status:"duplicate_suspected", matches, dedupe, ...}
          {status:"execute_blocked", reason, prepared:{…}}
          {status:"created_verified", name, verified:True, match, verify,
              response_url, submit_control, prepared:{…}, warning}  (committed,
              and confirmed present by an independent re-search)
          {status:"created_unverified", name, verified:False, reason, verify,
              prepared:{…}, warning}  (Save submitted but re-search did NOT
              confirm the chart — never trusted as success)
          {status:"save_unverified", reason, submit_controls, prepared:{…}}
              (only if the add form no longer exposes the NextTab trigger)
          {status:"error", stage, error}
        """
        page = self._page

        # ---- Validate the one thing we will NOT fabricate: a name ---------
        demo = {k: (str(demographics.get(k, "")).strip()
                    if demographics.get(k) is not None else "")
                for k in self._CREATE_DEMOGRAPHIC_KEYS}
        if not demo["last_name"] or not demo["first_name"]:
            return {"status": "error", "stage": "validate",
                    "error": "last_name and first_name are required "
                             "(a blank name is never fabricated)"}

        # ---- STAGE 1: open the new-patient entry form ---------------------
        new_url = f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry_new.htm"
        try:
            await self._goto(new_url, wait_until="networkidle", timeout=20000)
        except Exception as exc:
            return {"status": "error", "stage": "new_form",
                    "error": f"could not open patientEntry_new.htm: {exc}"}

        title = await page.title()
        if "Patient Entry" not in title:
            # Most likely the session lapsed — surface it honestly so the
            # manager wrapper can mark the session expired and reconnect.
            return {"status": "error", "stage": "new_form",
                    "error": f"unexpected page title {title!r} at "
                             "patientEntry_new.htm (session may have expired)"}

        # ---- STAGE 2: name-search DE-DUPE (read-only POST) ----------------
        # The new-patient form doubles as a duplicate check: filling the name
        # (+ DOB/SSN when known) and submitting searches existing charts before
        # letting you add. This is our idempotency guard — search FIRST so we
        # never create a second chart for someone already in the system.
        dedupe = {"performed": False, "match_count": 0, "matches": []}
        try:
            # Fill the de-dupe search fields that exist on the form. Field
            # names come from the HAR: LastName, FirstName, MI, BirthDate,
            # SocSecNo, Account. rtype=r is a hidden mode flag on the form.
            field_map = {
                'input[name="LastName"]': demo["last_name"],
                'input[name="FirstName"]': demo["first_name"],
                'input[name="MI"]': demo["mi"],
                'input[name="BirthDate"]': demo["dob"],
                'input[name="SocSecNo"]': demo["ssn"],
            }
            for sel, val in field_map.items():
                if not val:
                    continue
                el = await page.query_selector(sel)
                if el:
                    await page.fill(sel, val)

            # Submit the de-dupe search. The form's own submit performs the
            # name search (rtype=r). Prefer a named search button, else submit
            # the form element directly — either way this is a READ.
            submit_btn = (
                await page.query_selector('input[name="NameSearch"]')
                or await page.query_selector('input[type="submit"]')
            )
            if submit_btn:
                await submit_btn.click()
            else:
                await page.evaluate(
                    'var f=document.forms["patientEntry_new"]||document.forms[0];'
                    'f && f.submit();'
                )
            await page.wait_for_load_state("networkidle", timeout=15000)
            dedupe["performed"] = True

            # Harvest any existing-chart matches (same link shape as
            # search_patient: pentry.htm?rowid=...&acct=...).
            existing = await page.query_selector_all(
                'a[href*="pentry.htm?rowid="]'
            )
            for link in existing:
                href = await link.get_attribute("href") or ""
                name = (await link.inner_text()).strip()
                rowid = ""
                acct = ""
                rm = re.search(r'rowid=([^&|]+)', href)
                am = re.search(r'acct=([^&|]+)', href)
                if rm:
                    rowid = rm.group(1)
                if am:
                    acct = am.group(1)
                dedupe["matches"].append(
                    {"name": name, "rowid": rowid, "acct": acct}
                )
            dedupe["match_count"] = len(dedupe["matches"])
        except Exception as exc:
            # A de-dupe failure is not fatal to a DRY-RUN discovery, but it IS
            # fatal to a commit — we will not create a chart we could not first
            # check for duplicates. Record it and enforce below.
            dedupe["error"] = str(exc)
            logger.warning("Svigg create de-dupe search failed: %s", exc)

        # Idempotency: if the de-dupe found an existing chart, STOP and report
        # it rather than adding a duplicate. (A human can decide from the card.)
        if dedupe["match_count"] > 0:
            return {
                "status": "duplicate_suspected",
                "reason": ("an existing chart matched the name/DOB in the "
                           "de-dupe search — refusing to create a duplicate; "
                           "review the matches and pass an explicit override "
                           "path if this is genuinely a new person"),
                "matches": dedupe["matches"],
                "dedupe": dedupe,
                "name": f'{demo["last_name"]}, {demo["first_name"]}',
            }

        # ---- STAGE 3: open the ADD form ----------------------------------
        add_url = f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry_add.htm"
        try:
            await self._goto(add_url, wait_until="networkidle", timeout=20000)
        except Exception as exc:
            return {"status": "error", "stage": "add_form",
                    "error": f"could not open patientEntry_add.htm: {exc}"}

        # The add form may live in the main document or a frame. Find whichever
        # context actually hosts input fields.
        add_ctx = page
        try:
            n_inputs = len(await page.query_selector_all(
                'input, select, textarea'))
        except Exception:
            n_inputs = 0
        if n_inputs == 0:
            for frame in page.frames:
                try:
                    if await frame.query_selector('input, select, textarea'):
                        add_ctx = frame
                        break
                except Exception:
                    continue

        # ---- STAGE 4: DISCOVER the add form's real fields from the DOM ----
        # We do NOT hardcode the add-form field names (they are not in the
        # HAR). We read them live so the mapping below is anchored to reality
        # and the dry-run report documents exactly what the form exposes.
        try:
            discovered = await add_ctx.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    const push = (el) => {
                        const name = el.getAttribute('name') || '';
                        if (!name || seen.has(name)) return;
                        seen.add(name);
                        let type = (el.tagName || '').toLowerCase();
                        if (type === 'input') type = el.getAttribute('type') || 'text';
                        const rec = {name, type};
                        if ((el.tagName||'').toLowerCase() === 'select') {
                            rec.options = Array.from(el.options || [])
                                .map(o => o.value).slice(0, 40);
                        }
                        out.push(rec);
                    };
                    document.querySelectorAll('input,select,textarea')
                        .forEach(push);
                    return out;
                }"""
            )
        except Exception as exc:
            return {"status": "error", "stage": "discover_fields",
                    "error": f"could not read add-form fields from DOM: {exc}"}

        if not discovered:
            return {"status": "error", "stage": "discover_fields",
                    "error": "patientEntry_add.htm exposed no input fields — "
                             "the add form did not render (session/nav issue)"}

        discovered_names = {f["name"] for f in discovered}

        # ---- STAGE 5: map demographics onto discovered fields ------------
        # Candidate field-name aliases per demographic. We match against the
        # names the form ACTUALLY exposes (discovered_names), first exact then
        # case-insensitive, and never invent a target. Anything we can't place
        # is reported under ``unmapped_demographics`` for a human to resolve.
        # First alias per demographic is the HAR-verified add-form field name
        # (base websrv01.physician-to-go.net.har: the NEW-patient Save is a
        # POST to pentry.htm with Referer patientEntry_add.htm — its param keys
        # are LastName/FirstName/MI/BirthDate/SocSecNo/StreetAddrs1/StreetAddrs2/
        # City/State/Zip/CellNumber/WorkNumber/Email/...). The remaining
        # aliases are defensive fallbacks in case the live add form differs.
        # This only affects DRY-RUN field mapping — the Save itself stays
        # unencoded and double-gated below.
        alias_map = {
            "last_name": ["LastName", "lname", "Last", "PatientLastName"],
            "first_name": ["FirstName", "fname", "First", "PatientFirstName"],
            "mi": ["MI", "MiddleInitial", "Middle"],
            "dob": ["BirthDate", "DOB", "DateOfBirth", "Birthdate"],
            "ssn": ["SocSecNo", "SSN", "SocialSecurityNo", "Social"],
            "sex": ["Gender", "Sex"],
            "address": ["StreetAddrs1", "Address", "Address1", "Addr", "Street"],
            "address2": ["StreetAddrs2", "Address2", "Addr2"],
            "city": ["City"],
            "state": ["State", "St"],
            "zip": ["Zip", "ZipCode", "PostalCode"],
            "home_phone": ["HomePhone", "Home", "Phone", "PhoneHome"],
            "cell_phone": ["CellNumber", "CellPhone", "Cell", "Mobile", "PhoneCell"],
            "work_phone": ["WorkNumber", "WorkPhone", "Work", "PhoneWork"],
            "email": ["Email", "EmailAddress", "EMail"],
        }

        def _resolve(field_names: list[str]) -> str:
            for cand in field_names:
                if cand in discovered_names:
                    return cand
            lower = {n.lower(): n for n in discovered_names}
            for cand in field_names:
                if cand.lower() in lower:
                    return lower[cand.lower()]
            return ""

        mapped: dict[str, str] = {}       # form_field_name -> value
        mapping_trace: dict[str, str] = {} # demographic_key -> form_field_name
        unmapped: list[str] = []
        for demo_key in self._CREATE_DEMOGRAPHIC_KEYS:
            val = demo.get(demo_key, "")
            if not val:
                continue  # honest blank — nothing to place
            target = _resolve(alias_map.get(demo_key, [demo_key]))
            if target:
                mapped[target] = val
                mapping_trace[demo_key] = target
            else:
                unmapped.append(demo_key)

        prepared = {
            "status": "prepared",
            "add_form_url": add_url,
            "discovered_fields": discovered,
            "would_submit": mapped,
            "mapping": mapping_trace,
            "unmapped_demographics": unmapped,
            "dedupe": dedupe,
            "name": f'{demo["last_name"]}, {demo["first_name"]}',
            "warning": ("SAVE not submitted — no chart created. Fields above "
                        "were DISCOVERED live from the add form DOM. The Save "
                        "commit IS wired (base HAR: POST pentry.htm, Referer "
                        "patientEntry_add.htm, ~53 form fields, submit trigger "
                        "NextTab, 200 OK) but only fires on the triple-gated "
                        "commit path (SVIGG_CREATE_EXECUTE=1 + dry_run=False + "
                        "confirm_unverified=True), which self-verifies by a "
                        "post-save re-search. This dry-run discovery submits "
                        "nothing and its FIRST live use must be supervised on a "
                        "test account (contract is from a single capture)."),
        }

        # ================================================================
        # COMMIT GUARD — dry-run default, then two fail-closed locks.
        # ================================================================
        if dry_run:
            return prepared

        # Commit requested. A commit without a successful de-dupe is refused:
        # we will not create a chart we could not first check for duplicates.
        if not dedupe.get("performed"):
            return {"status": "execute_blocked",
                    "reason": ("de-dupe search did not complete — refusing to "
                               "create a chart without a duplicate check"),
                    "dedupe": dedupe, "prepared": prepared}

        if not (CREATE_EXECUTE_ENABLED and confirm_unverified):
            return {
                "status": "execute_blocked",
                "reason": ("new-patient commit is unverified (Save POST not in "
                           "HAR) and disabled by default; requires "
                           "SVIGG_CREATE_EXECUTE=1 in the environment AND "
                           "confirm_unverified=True"),
                "flag_enabled": CREATE_EXECUTE_ENABLED,
                "confirm_unverified": confirm_unverified,
                "prepared": prepared,
            }

        # Both locks open. The Save wire contract is now HAR-confirmed (base
        # websrv01.physician-to-go.net.har, single 2026-07 capture): the ADD
        # form is <form method=post action=/proxy.cgi/{session}/pentry.htm
        # name="pentry">, and its Save is triggered by an
        # <input type="submit" name="NextTab"> — a POST of ~53 urlencoded
        # params whose HIDDEN TOKENS + select defaults (TFORMCOUNT, Chart,
        # Number, SigOnFile, HipaaFormDate, PrimaryOffice, Lang1, State/refState/
        # pcpState/refState2, NextTab, …) already carry their correct values in
        # the rendered add form. So — exactly like the booking flow's conf-form
        # Submit — we do NOT hand-assemble the body: we FILL only the caller's
        # demographic fields into the live DOM and CLICK NextTab, letting the
        # browser serialize the full form (all live tokens + defaults + our
        # demographics). This reproduces the captured 53-key body faithfully
        # without hardcoding a single stale token.
        #
        # FIRST-USE SAFETY: this contract comes from a SINGLE capture and has
        # NEVER been live-tested. It is triple-gated to get here; even so, we
        # NEVER assume success from an HTTP 200 — we VERIFY by an independent
        # patient re-search below, and surface any verification miss LOUDLY,
        # fail-closed (status "created_unverified").
        first_live_warning = (
            "FIRST LIVE USE MUST BE SUPERVISED ON A TEST ACCOUNT. The Svigg "
            "add-form Save contract is from a single HAR capture and has not "
            "been live-verified before. This path is triple-gated "
            "(SVIGG_CREATE_EXECUTE=1 + dry_run=False + confirm_unverified=True) "
            "and self-verifies by re-searching the patient after saving — but a "
            "human must watch the first real run and confirm the chart in Svigg."
        )

        # Positively identify the HAR-confirmed submit trigger (NextTab). We
        # NEVER guess a submit target: if the add form does not expose exactly
        # this control, fail closed rather than clicking something else.
        save_selector = 'input[type="submit"][name="NextTab"]'
        save_ctrl = None
        try:
            save_ctrl = await add_ctx.query_selector(save_selector)
        except Exception as exc:
            logger.warning("Svigg create Save-control lookup failed: %s", exc)
        if save_ctrl is None:
            # Diagnostic: what submit-like controls DID the form expose? (names
            # only — safe.) Hand them back so a human can see the drift.
            try:
                controls = await add_ctx.evaluate(
                    """() => Array.from(document.querySelectorAll(
                            'input[type=submit],input[type=image],button'))
                        .map(e => ({
                            name: e.getAttribute('name') || '',
                            type: (e.getAttribute('type')||'').toLowerCase()
                        }))"""
                )
            except Exception:
                controls = []
            return {
                "status": "save_unverified",
                "reason": ("commit locks are open but the HAR-confirmed Save "
                           "trigger input[name=\"NextTab\"] is NOT present on "
                           "the rendered add form — the form drifted from the "
                           "captured contract. Refusing to click a different "
                           "control. Re-capture the add-form Save under "
                           "supervision."),
                "submit_controls": controls,
                "prepared": prepared,
                "warning": ("NO chart created — Save trigger missing; "
                            "fail-closed. " + first_live_warning),
            }

        # Fill ONLY the caller's demographic fields into the live add form. The
        # `mapped` dict is {live_add_form_field_name: value}, already resolved
        # (STAGE 5) against the fields the DOM actually exposes and anchored to
        # the HAR add-form key names (LastName/FirstName/MI/BirthDate/SocSecNo/
        # StreetAddrs1/StreetAddrs2/City/State/Zip/CellNumber/WorkNumber/Email/…).
        # Everything else (hidden tokens, select defaults) is left untouched so
        # the browser submits the form's own live values. Fill failures are
        # recorded but not silently ignored.
        fill_failures: dict[str, str] = {}
        for fname, fval in mapped.items():
            sel = f'[name="{fname}"]'
            try:
                await add_ctx.fill(sel, fval)
            except Exception as exc:
                fill_failures[fname] = str(exc)
                logger.warning(
                    "Svigg create: could not fill add-form field %r: %s",
                    fname, exc,
                )
        # A field we intended to write but could not is a hard failure on the
        # commit path — we will not save a chart with silently-dropped data.
        if fill_failures:
            return {
                "status": "error",
                "stage": "form_fill",
                "error": ("could not fill one or more mapped add-form fields — "
                          "aborting before Save (would have created a chart "
                          "with missing data)"),
                "fill_failures": fill_failures,
                "prepared": prepared,
                "warning": "NO chart created. " + first_live_warning,
            }

        # Capture the live form action (session-scoped) as evidence only — we
        # submit by clicking NextTab, not by POSTing this URL ourselves.
        try:
            form_action = await add_ctx.eval_on_selector(
                'form[name="pentry"]', 'f => f.getAttribute("action") || ""')
        except Exception:
            form_action = ""

        # ---- THE SAVE: click the HAR-confirmed NextTab submit ------------
        try:
            await save_ctrl.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception as exc:
            # The click/POST itself failed. We CANNOT know whether the server
            # created a chart, so we DO NOT claim either way — we verify below,
            # but flag the click error honestly.
            logger.warning("Svigg create Save click/load failed: %s", exc)
            save_click_error = str(exc)
        else:
            save_click_error = ""

        try:
            resp_url = page.url
        except Exception:
            resp_url = ""
        try:
            resp_title = await page.title()
        except Exception:
            resp_title = ""

        # ================================================================
        # VERIFY-AFTER (mandatory) — never trust HTTP 200. Independently
        # re-search Svigg for the just-submitted patient and confirm a chart
        # now exists. This mirrors booking's grid-verify: the ONLY thing that
        # upgrades us to "created_verified" is a positive re-read.
        # ================================================================
        verify = {"performed": False, "matched": False, "match_count": 0,
                  "matches": []}
        try:
            found = await self.search_patient(
                last_name=demo["last_name"], first_name=demo["first_name"],
            )
            verify["performed"] = True
            verify["match_count"] = len(found)
            want_dob = demo.get("dob", "")
            for rec in found:
                got_name = (rec.get("name") or "")
                # Name must match on BOTH surname and given name (case-insens,
                # substring-tolerant to Svigg's "Last, First" rendering).
                name_ok = (
                    demo["last_name"].lower() in got_name.lower()
                    and demo["first_name"].lower() in got_name.lower()
                )
                # If we submitted a DOB and the record carries one, require it
                # to agree; if either side lacks a DOB, fall back to name-only.
                got_dob = (rec.get("dob") or "").strip()
                dob_ok = (not want_dob) or (not got_dob) or (want_dob == got_dob)
                if name_ok and dob_ok:
                    verify["matched"] = True
                    verify["matches"].append({
                        "name": got_name,
                        "acct": rec.get("acct", ""),
                        "rowid": rec.get("rowid", ""),
                    })
        except Exception as exc:
            verify["error"] = str(exc)
            logger.warning("Svigg create verify re-search failed: %s", exc)

        if verify.get("matched"):
            logger.info(
                "Svigg create_patient: chart VERIFIED via re-search "
                "(matches=%d)", len(verify["matches"]),
            )
            return {
                "status": "created_verified",
                "name": f'{demo["last_name"]}, {demo["first_name"]}',
                "verified": True,
                "match": verify["matches"][0] if verify["matches"] else {},
                "verify": verify,
                "response_url": resp_url,
                "response_title": resp_title,
                "form_action": form_action,
                "submit_control": "NextTab",
                "prepared": prepared,
                "warning": first_live_warning,
            }

        # Verification did NOT confirm a chart. NEVER report success from a 200
        # alone. Surface loudly, fail-closed: a human must check Svigg directly
        # (the save MAY or may not have landed — we cannot prove it did).
        logger.warning(
            "Svigg create_patient: Save submitted but re-search did NOT "
            "confirm the chart (match_count=%d) — reporting UNVERIFIED",
            verify.get("match_count", 0),
        )
        return {
            "status": "created_unverified",
            "name": f'{demo["last_name"]}, {demo["first_name"]}',
            "verified": False,
            "reason": ("the add-form Save was submitted (NextTab) but an "
                       "independent patient re-search did NOT find the new "
                       "chart — the create is UNCONFIRMED. Do NOT assume it "
                       "succeeded: a human must verify in Svigg and, if no "
                       "chart exists, retry or add it manually. If a chart DID "
                       "land, this may be a search-indexing lag."),
            "verify": verify,
            "save_click_error": save_click_error,
            "response_url": resp_url,
            "response_title": resp_title,
            "form_action": form_action,
            "submit_control": "NextTab",
            "prepared": prepared,
            "warning": ("CHART CREATION UNCONFIRMED — verify in Svigg before "
                        "trusting. " + first_live_warning),
        }

    # ------------------------------------------------------------------
    # EXISTING-PATIENT DEMOGRAPHIC EDIT
    # ------------------------------------------------------------------
    async def update_patient(
        self,
        last_name: str,
        first_name: str,
        updates: dict,
        *,
        dob: str = "",
        svigg_acct: str = "",
        dry_run: bool = True,
        confirm_unverified: bool = False,
        progress_cb=None,
        proof_path: Optional[str] = None,
    ) -> dict:
        """Edit an EXISTING Svigg patient's demographic fields — DRY-RUN by default.

        Unlike create_patient (deliberately dry-run-forever), this MIRRORS the
        booking flow: when the human approves the card AND the execute gate is
        on, it actually performs the edit. But every safety rail is kept.

        SAFETY (three independent locks + two guards):
          * dry_run=True (DEFAULT): walk to the pre-filled edit form, report the
            CURRENT value(s) of the target field(s) + the would-be new value(s),
            and STOP — change nothing. No imageField click, no re-read write.
          * A real write requires ALL of: dry_run=False AND the module gate
            EDIT_EXECUTE_ENABLED (env SVIGG_EDIT_EXECUTE) AND confirm_unverified
            — same fail-closed triple-gate as create_patient's, so an accidental
            call can never write.
          * IDENTITY GUARD (_identity_matches, additive): before writing, the
            resolved chart's own name (+DOB when both are present) must match the
            requested patient. A mismatch returns identity_mismatch, no write.
            Editing the wrong chart is a serious harm — this is fail-closed.
          * VERIFY-AFTER (mandatory): after saving, re-read the patient's
            demographics (get_patient_summary) and confirm the new value(s)
            actually landed. status "updated_verified" (verified=True) ONLY on a
            positive re-read; else "updated_unverified" + a loud warning. Never
            trust an HTTP 200 alone.

        The EDIT save contract IS HAR-confirmed (base websrv01.physician-to-go
        .net.har: the existing-patient edit form is <form action=.../pentry.htm>
        and its Save is an image button input[name="imageField"] type="image" —
        producing imageField.x/imageField.y — POSTed with Referer pentry.htm,
        ~54 urlencoded params). That is DISTINCT from the ADD save (NextTab,
        Referer patientEntry_add.htm). We NEVER hand-assemble the 54-field body:
        we fill ONLY the caller's target fields into the live pre-filled form and
        click imageField, so the browser serializes the full form (all existing
        pre-filled values + our changes) — every field we did NOT touch keeps its
        current value; no field is ever blanked.

        Flow (all read-only until the imageField Save on the commit path):
          resolve the patient to a UNIQUE chart (search_patient acct-first, else
            the name-variant ladder — reuses the cancel flow's
            _resolve_rowid_for_chart) -> {rowid, acct, name, dob}
            (0 or >1 ambiguous matches => honest refusal, never guess)
          -> IDENTITY GUARD on the resolved chart's own name/DOB
          -> GET the chart-open pentry.htm?rowid&acct (renders the pre-filled
             edit form), find the form context (main doc or frame) hosting
             input[name="imageField"] + the demographic inputs
          -> READ the current value of each target field (for the dry-run report
             and the verify baseline)
          -> [dry_run] STOP + return current + proposed values (change nothing)
          -> [commit] triple-gated: fill ONLY the target fields, click the
             HAR-confirmed imageField Save, then VERIFY by an independent
             get_patient_summary re-read before ever claiming success.

        Args:
            last_name/first_name: the patient the caller asked to edit — used to
                resolve the chart AND (with dob) to bind the identity guard.
            updates: {our_field: new_value}, our_field one of _EDIT_FIELD_MAP's
                keys (cell/home_phone/work_phone/email/address1/city/state/zip/…).
                cell->CellNumber, home_phone->Number, work_phone->WorkNumber,
                email->Email, etc. Empty/blank values are refused (we never
                BLANK a field — an edit must set a real value). A key not in the
                map is reported unmapped (never guessed onto a field).
            dob: OPTIONAL caller-side DOB (MM/DD/YYYY) for the identity guard.
            svigg_acct: OPTIONAL known account # — resolves the chart directly
                and unambiguously when present.
            dry_run: True (DEFAULT) => report current+proposed, write nothing.
            confirm_unverified: caller's explicit acknowledgement (part of the
                triple-gate). Required with the module flag to ever save.
            proof_path: optional screenshot path (captured on success AND
                failure, so the card can render a proof thumbnail).

        Returns one of:
          {status:"prepared", acct, name, current, proposed, mapped,
              unmapped, warning}                          (dry-run; nothing written)
          {status:"no_match"|"ambiguous"|"no_rowid", reason, match_count, …}
              (patient not uniquely resolved — refusal, no write)
          {status:"identity_mismatch", reason, resolved_name, …}
              (resolved chart does not match the requested patient — no write)
          {status:"execute_blocked", reason, flag_enabled, confirm_unverified,
              prepared}                                    (a gate is closed)
          {status:"save_unverified", reason, submit_controls, prepared}
              (commit locks open but the imageField trigger is missing — drift)
          {status:"updated_verified", acct, name, verified:True, changed, verify,
              …}   (saved AND confirmed by a demographics re-read)
          {status:"updated_unverified", acct, name, verified:False, reason,
              verify, …}   (saved but re-read did NOT confirm — never trusted)
          {status:"error", stage, error}
        Every return carries proof_captured (bool) when a proof_path was given.
        """
        def _p(stage: str, label: str) -> None:
            if progress_cb:
                try:
                    progress_cb(stage, label)
                except Exception:  # a progress callback must never break a write
                    pass

        # ---- Validate inputs we will NOT fabricate -----------------------
        last_name = (last_name or "").strip()
        first_name = (first_name or "").strip()
        if not last_name:
            return {"status": "error", "stage": "validate",
                    "error": "last_name is required to resolve the patient "
                             "(a chart is never edited without a confirmed name)"}
        if not isinstance(updates, dict) or not updates:
            return {"status": "error", "stage": "validate",
                    "error": "no field updates supplied — refusing to open an "
                             "edit form with nothing to change"}

        # Map caller fields -> real Svigg form names. Refuse blank values (an
        # edit sets a real value; we NEVER blank a field) and never guess an
        # unknown key onto a form field.
        mapped: dict[str, str] = {}        # svigg_form_field -> new value
        mapping_trace: dict[str, str] = {} # our_field -> svigg_form_field
        unmapped: list[str] = []
        blanks: list[str] = []
        phone_format_notes: dict[str, str] = {}  # our_field -> couldn't-reformat note
        for our_field, new_val in updates.items():
            sval = "" if new_val is None else str(new_val).strip()
            form_field = self._EDIT_FIELD_MAP.get(our_field)
            if form_field is None:
                unmapped.append(our_field)
                continue
            if not sval:
                blanks.append(our_field)
                continue
            # Phone fields (Number/CellNumber/WorkNumber) MUST be filled in
            # Svigg's dashed XXX-XXX-XXXX shape or the Save silently no-ops (see
            # _format_phone_for_svigg). Reformat here — the single source of
            # truth for BOTH the dry-run report (proposed/mapped) and the write
            # path's .fill(), so what the human previews is exactly what's
            # filled (no silent-only-on-write divergence). Non-phone fields
            # (email/address/name) are NEVER touched by this.
            if _is_phone_field(form_field):
                formatted, ok = _format_phone_for_svigg(sval)
                sval = formatted
                if not ok:
                    phone_format_notes[our_field] = (
                        f"{our_field}={new_val!r} has an unexpected digit count "
                        "(not 10, nor 11 with a leading 1); passing the value "
                        "through UNCHANGED — Svigg wants XXX-XXX-XXXX, so this "
                        "may not persist. Supply a 10-digit US number.")
            mapped[form_field] = sval
            mapping_trace[our_field] = form_field
        if blanks:
            return {"status": "error", "stage": "validate",
                    "error": ("refusing to write a BLANK value to "
                              f"{', '.join(blanks)} — update_patient never "
                              "blanks a field; supply a real value"),
                    "blanks": blanks}
        if not mapped:
            return {"status": "error", "stage": "validate",
                    "error": ("none of the requested fields map to a known "
                              "Svigg demographic field — refusing to edit"),
                    "unmapped": unmapped}

        # ---- STAGE 1: resolve the patient to a UNIQUE chart --------------
        # Reuse the cancel flow's acct-first resolve (search_patient acct-first,
        # then the bounded name-variant ladder), which returns a single chart
        # only on a unique acct match with a plausible rowid — else no_match /
        # ambiguous / no_rowid, all of which we REFUSE (never edit a guess).
        _p("resolve", "Resolving the patient chart")
        acct_hint = str(svigg_acct or "").strip()
        try:
            if acct_hint:
                chosen = await self._resolve_rowid_for_chart(
                    acct_hint, last_name, first_name)
            else:
                # No acct: search by name, then require a UNIQUE plausible chart.
                matches = await self.search_patient(last_name, first_name)
                # _choose_rowid_from_matches keys off acct; with no acct hint we
                # accept it ONLY when exactly one result carries a rowid.
                usable = [m for m in matches
                          if _is_plausible_svigg_rowid(str(m.get("rowid", "")))]
                if len(usable) == 1:
                    m0 = usable[0]
                    chosen = {"rowid": str(m0.get("rowid", "")),
                              "acct": str(m0.get("acct", "")),
                              "name": (m0.get("name") or "").strip(),
                              "dob": (m0.get("dob") or "").strip(),
                              "status": "ok",
                              "match_count": len(matches)}
                else:
                    chosen = {"rowid": "", "acct": "", "name": "", "dob": "",
                              "status": ("ambiguous" if len(usable) > 1
                                         else "no_match"),
                              "match_count": len(matches)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "stage": "resolve",
                    "error": f"patient resolution failed: {exc}"}

        if chosen.get("status") != "ok":
            reason = {
                "no_match": ("no Svigg chart matched the requested patient — "
                             "refusing to edit (never edit a guessed patient)"),
                "ambiguous": ("more than one Svigg chart matched — refusing to "
                              "edit an ambiguous patient; supply the account #"),
                "no_rowid": ("the matched chart had no usable Svigg id — "
                             "refusing to open it"),
            }.get(chosen.get("status"),
                  "patient could not be uniquely resolved — refusing to edit")
            return {"status": chosen.get("status", "no_match"),
                    "reason": reason,
                    "match_count": chosen.get("match_count", 0),
                    "name": f"{last_name}, {first_name}".strip(", ")}

        rowid = chosen["rowid"]
        acct = chosen["acct"] or acct_hint
        resolved_name = chosen.get("name", "")
        resolved_dob = chosen.get("dob", "")

        # ---- IDENTITY GUARD (before ANY navigation-to-write) -------------
        # Confirm the chart we resolved actually belongs to the requested
        # patient. A mismatch is a HARD refusal — editing the wrong chart is a
        # serious harm. Additive: never relaxes the gates below.
        _p("resolve", "Confirming patient identity")
        _id_ok, _id_reason = _identity_matches(
            last_name, first_name, dob, resolved_name, resolved_dob)
        if not _id_ok:
            logger.warning("Svigg update_patient: identity guard REFUSED "
                           "(no write): %s", _id_reason)
            proof_captured = await self._capture_proof(proof_path)
            return {"status": "identity_mismatch",
                    "reason": _id_reason,
                    "acct": acct,
                    "resolved_name": resolved_name,
                    "requested_name": f"{last_name}, {first_name}".strip(", "),
                    "proof_captured": proof_captured,
                    "warning": ("NO edit performed — the resolved chart does "
                                "not match the requested patient. Fail-closed.")}

        # ---- STAGE 2: open the pre-filled edit form ----------------------
        # The search result links a chart as pentry.htm?rowid=&acct= (HAR-
        # verified href shape). GETting it renders the session-scoped edit form
        # <form action=.../pentry.htm> pre-filled with the patient's CURRENT
        # values and carrying the image Save button input[name="imageField"].
        _p("open", "Opening the patient edit form")
        edit_url = (f"{self.BASE_URL}/proxy.cgi/off/maint/pentry.htm?"
                    f"rowid={rowid}&acct={acct}")
        try:
            await self._goto(edit_url, wait_until="networkidle", timeout=20000)
        except Exception as exc:
            return {"status": "error", "stage": "open_form",
                    "error": f"could not open the edit form pentry.htm: {exc}",
                    "acct": acct}

        page = self._page
        # The edit form may live in the main document or a frame. Find whichever
        # context hosts the imageField Save trigger (our reliable anchor) OR the
        # demographic inputs — mirrors create_patient's frame-hunt.
        edit_ctx = page
        found_ctx = False
        contexts = [page] + list(getattr(page, "frames", []) or [])
        for ctx in contexts:
            try:
                if await ctx.query_selector('input[name="imageField"]'):
                    edit_ctx = ctx
                    found_ctx = True
                    break
            except Exception:
                continue
        if not found_ctx:
            # Fall back to any context exposing the demographic inputs (the
            # imageField may be an <input type=image> the selector missed on a
            # variant build) — but we still require imageField for the commit.
            for ctx in contexts:
                try:
                    if await ctx.query_selector(
                            'input[name="LastName"], input[name="CellNumber"]'):
                        edit_ctx = ctx
                        break
                except Exception:
                    continue

        # ---- STAGE 3: READ the current value(s) of the target field(s) ---
        # Baseline for the dry-run report AND the post-save verify. Reads the
        # live pre-filled input values straight off the DOM (never invented).
        current: dict[str, str] = {}   # svigg_form_field -> current value
        read_errors: dict[str, str] = {}
        for form_field in mapped:
            try:
                cur = await edit_ctx.eval_on_selector(
                    f'[name="{form_field}"]',
                    'el => (el && "value" in el) ? (el.value || "") : ""')
            except Exception as exc:
                cur = ""
                read_errors[form_field] = str(exc)
            current[form_field] = cur if isinstance(cur, str) else ""

        # Human-readable current->proposed view keyed by OUR field names.
        proposed = {}
        current_by_our_field = {}
        for our_field, form_field in mapping_trace.items():
            current_by_our_field[our_field] = current.get(form_field, "")
            proposed[our_field] = mapped[form_field]

        prepared = {
            "status": "prepared",
            "acct": acct,
            "name": resolved_name or f"{last_name}, {first_name}".strip(", "),
            "edit_form_url": edit_url,
            "current": current_by_our_field,
            "proposed": proposed,
            "mapped": mapped,
            "mapping": mapping_trace,
            "unmapped": unmapped,
            "phone_format_notes": phone_format_notes,
            "read_errors": read_errors,
            "warning": ("EDIT not submitted — no chart changed. The value(s) "
                        "shown are READ live from the pre-filled edit form. The "
                        "Save commit IS wired (HAR: POST pentry.htm, Referer "
                        "pentry.htm, image button imageField) but only fires on "
                        "the triple-gated path (SVIGG_EDIT_EXECUTE=1 + "
                        "dry_run=False + confirm_unverified=True), is identity-"
                        "guarded, and self-verifies by a post-save demographics "
                        "re-read. This dry-run changes nothing."),
        }

        # ================================================================
        # COMMIT GUARD — dry-run default, then the fail-closed locks.
        # ================================================================
        if dry_run:
            prepared["proof_captured"] = await self._capture_proof(proof_path)
            return prepared

        if not (EDIT_EXECUTE_ENABLED and confirm_unverified):
            proof_captured = await self._capture_proof(proof_path)
            return {
                "status": "execute_blocked",
                "reason": ("patient-edit commit is disabled by default; "
                           "requires SVIGG_EDIT_EXECUTE=1 in the environment "
                           "AND confirm_unverified=True (the human approval "
                           "authorizes it, but the env gate must be on)"),
                "flag_enabled": EDIT_EXECUTE_ENABLED,
                "confirm_unverified": confirm_unverified,
                "prepared": prepared,
                "proof_captured": proof_captured,
            }

        first_live_warning = (
            "FIRST LIVE EDIT MUST BE SUPERVISED ON THE TEST ACCOUNT (22041163). "
            "This path is triple-gated (SVIGG_EDIT_EXECUTE=1 + dry_run=False + "
            "confirm_unverified=True), identity-guarded, and self-verifies by a "
            "post-save demographics re-read — but a human must watch the first "
            "real edit and confirm the change in Svigg."
        )

        # Positively identify the HAR-confirmed Save trigger (the image button
        # imageField). We NEVER click a different control: if the form does not
        # expose it, fail closed.
        _p("save", "Locating the Save control")
        save_ctrl = None
        try:
            save_ctrl = await edit_ctx.query_selector('input[name="imageField"]')
        except Exception as exc:
            logger.warning("Svigg update Save-control lookup failed: %s", exc)
        if save_ctrl is None:
            try:
                controls = await edit_ctx.evaluate(
                    """() => Array.from(document.querySelectorAll(
                            'input[type=submit],input[type=image],button'))
                        .map(e => ({
                            name: e.getAttribute('name') || '',
                            type: (e.getAttribute('type')||'').toLowerCase()
                        }))"""
                )
            except Exception:
                controls = []
            proof_captured = await self._capture_proof(proof_path)
            return {
                "status": "save_unverified",
                "reason": ("commit locks are open but the HAR-confirmed Save "
                           "trigger input[name=\"imageField\"] is NOT present "
                           "on the edit form — the form drifted from the "
                           "captured contract. Refusing to click a different "
                           "control."),
                "submit_controls": controls,
                "prepared": prepared,
                "proof_captured": proof_captured,
                "warning": ("NO edit performed — Save trigger missing; "
                            "fail-closed. " + first_live_warning),
            }

        # Fill ONLY the caller's target fields into the live pre-filled form.
        # Everything else (all other demographics, hidden tokens, TFORMCOUNT)
        # stays at its rendered value so the browser re-submits the FULL form
        # unchanged except for our edits — NEVER blanking an untouched field.
        _p("save", "Applying the change(s)")
        fill_failures: dict[str, str] = {}
        for form_field, new_val in mapped.items():
            sel = f'[name="{form_field}"]'
            try:
                await edit_ctx.fill(sel, new_val)
            except Exception as exc:
                fill_failures[form_field] = str(exc)
                logger.warning("Svigg update: could not fill %r: %s",
                               form_field, exc)
        if fill_failures:
            proof_captured = await self._capture_proof(proof_path)
            return {
                "status": "error",
                "stage": "form_fill",
                "error": ("could not fill one or more target fields — aborting "
                          "before Save (a partial edit is never submitted)"),
                "fill_failures": fill_failures,
                "prepared": prepared,
                "proof_captured": proof_captured,
                "warning": "NO edit performed. " + first_live_warning,
            }

        try:
            form_action = await edit_ctx.eval_on_selector(
                'form', 'f => f.getAttribute("action") || ""')
        except Exception:
            form_action = ""

        # ---- THE SAVE: click the HAR-confirmed imageField image button ---
        _p("save", "Saving in Svigg")
        try:
            await save_ctrl.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception as exc:
            logger.warning("Svigg update Save click/load failed: %s", exc)
            save_click_error = str(exc)
        else:
            save_click_error = ""

        try:
            resp_url = page.url
        except Exception:
            resp_url = ""

        # ================================================================
        # VERIFY-AFTER (mandatory) — never trust HTTP 200. Independently
        # re-read the patient's demographics and confirm the new value(s)
        # actually landed. Only a positive re-read upgrades to
        # "updated_verified".
        # ================================================================
        _p("verify", "Re-reading demographics to confirm")
        verify = {"performed": False, "confirmed": {}, "mismatched": {}}
        try:
            summary = await self.get_patient_summary(rowid, acct)
            verify["performed"] = True
            # Pull the re-read values that correspond to our edited fields.
            reread = self._extract_demographics_for_verify(summary)
            for our_field, form_field in mapping_trace.items():
                want = _norm_phone_for_compare(mapped[form_field]) \
                    if _is_phone_field(form_field) \
                    else (mapped[form_field] or "").strip().lower()
                got_raw = reread.get(our_field, "")
                got = _norm_phone_for_compare(got_raw) \
                    if _is_phone_field(form_field) \
                    else (got_raw or "").strip().lower()
                if want and got and want == got:
                    verify["confirmed"][our_field] = got_raw
                else:
                    verify["mismatched"][our_field] = {
                        "wanted": mapped[form_field], "read": got_raw}
        except Exception as exc:
            verify["error"] = str(exc)
            logger.warning("Svigg update verify re-read failed: %s", exc)

        proof_captured = await self._capture_proof(proof_path)

        # All target fields confirmed present in the re-read => verified.
        all_confirmed = (
            verify.get("performed")
            and not verify.get("mismatched")
            and len(verify.get("confirmed", {})) == len(mapping_trace)
        )
        if all_confirmed:
            logger.info("Svigg update_patient: change VERIFIED via demographics "
                        "re-read (fields=%d)", len(mapping_trace))
            return {
                "status": "updated_verified",
                "acct": acct,
                "name": resolved_name or f"{last_name}, {first_name}",
                "verified": True,
                "changed": proposed,
                "current_before": current_by_our_field,
                "verify": verify,
                "response_url": resp_url,
                "form_action": form_action,
                "submit_control": "imageField",
                "prepared": prepared,
                "proof_captured": proof_captured,
                "warning": first_live_warning,
            }

        logger.warning("Svigg update_patient: Save submitted but re-read did "
                       "NOT confirm the change(s) — reporting UNVERIFIED "
                       "(confirmed=%d/%d)",
                       len(verify.get("confirmed", {})), len(mapping_trace))
        return {
            "status": "updated_unverified",
            "acct": acct,
            "name": resolved_name or f"{last_name}, {first_name}",
            "verified": False,
            "reason": ("the edit form Save was submitted (imageField) but an "
                       "independent demographics re-read did NOT confirm all "
                       "target field(s) — the edit is UNCONFIRMED. Do NOT assume "
                       "it succeeded: a human must verify in Svigg."),
            "changed": proposed,
            "current_before": current_by_our_field,
            "verify": verify,
            "save_click_error": save_click_error,
            "response_url": resp_url,
            "form_action": form_action,
            "submit_control": "imageField",
            "prepared": prepared,
            "proof_captured": proof_captured,
            "warning": ("PATIENT EDIT UNCONFIRMED — verify in Svigg before "
                        "trusting. " + first_live_warning),
        }

    def _extract_demographics_for_verify(self, summary: dict) -> dict:
        """Map a get_patient_summary() dict onto our edit field keys for the
        post-save verify. Pure (no I/O). Returns {our_field: value_as_read},
        only for the fields the summary actually exposes (missing => "").

        get_patient_summary parses phones into summary['phones'] = {'cell':…,
        'home':…, 'work':…} and email/address at the top level (see
        _parse_summary_text). We surface just the fields update_patient can
        edit; anything not present stays "" so the caller's verify treats it as
        an honest miss, never a fabricated confirmation.
        """
        out: dict[str, str] = {}
        if not isinstance(summary, dict):
            return out
        phones = summary.get("phones") or {}
        if isinstance(phones, dict):
            if phones.get("cell"):
                out["cell"] = phones["cell"]
                out["cell_phone"] = phones["cell"]
            if phones.get("home"):
                out["home_phone"] = phones["home"]
            if phones.get("work"):
                out["work_phone"] = phones["work"]
        if summary.get("email"):
            out["email"] = summary["email"]
        if summary.get("address"):
            # Summary renders address as a single joined string — expose it
            # under address1/address for a substring-tolerant verify only.
            out["address1"] = summary["address"]
            out["address"] = summary["address"]
        return out

    # ------------------------------------------------------------------
    # Generic read-only page helpers (added 2026-07-04)
    #
    # Shared plumbing for the read-only scrape methods below. House rules:
    # honest data (never fabricate a value; unknown column semantics stay
    # raw cell strings), fail closed (login/'Sorry'/unexpected layout ->
    # explicit error dict, never a silent empty success), no PHI in logger
    # lines (counts/ids/status only).
    # ------------------------------------------------------------------

    @staticmethod
    def _login_canary_html(html: str) -> Optional[str]:
        """Return an error string if `html` is a login or 'Sorry' page, else None.

        Mirrors the get_patient_ledger 'Sorry' guard and login()'s title
        check: never parse a login/error page as data. A password input
        anywhere on the page means the proxy bounced us to the login form
        (session expired); a near-empty body containing 'sorry' is Svigg's
        generic not-found/no-session page.
        """
        if re.search(r'type\s*=\s*["\']?password', html or "", re.I):
            return ("login page served instead of data — session expired or "
                    "not logged in; re-login required")
        text = re.sub(r"<[^>]+>", " ", html or "")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 100 and "sorry" in text.lower():
            return "Svigg returned 'Sorry' — record may not exist or session expired"
        return None

    async def _login_canary_page(self) -> Optional[str]:
        """Live-page variant of _login_canary_html for _goto-based flows.

        Returns an error string if the CURRENT page looks like a login or
        'Sorry' page — or if its body cannot be read at all (indeterminate
        page state is an error, never an empty success) — else None. Note:
        on frameset pages this inspects the outer document only (framesets
        have no meaningful body), so callers that dive into frames should
        still sanity-check what they parse.
        """
        page = self._page
        try:
            if await page.query_selector('input[type="password"]'):
                return ("login page served instead of data — session expired "
                        "or not logged in; re-login required")
            body_text = await page.inner_text("body")
        except Exception as exc:  # noqa: BLE001 — indeterminate page state is an error
            return f"could not read page body to confirm the data page: {exc}"
        stripped = (body_text or "").strip()
        if len(stripped) < 100 and "sorry" in stripped.lower():
            return "Svigg returned 'Sorry' — record may not exist or session expired"
        return None

    @staticmethod
    def _generic_tables_and_text(html: str, excerpt_chars: int = 2500):
        """Extract every <table> as rows of raw cell strings + a text excerpt.

        Generic honest extraction for pages whose column semantics have not
        been mapped yet: raw cell text only, no invented field names. With
        nested tables the outer table repeats the inner tables' text —
        callers get the raw shape and must de-duplicate if they care.
        Requires bs4 (callers check ImportError per house convention).
        Returns (tables, text_excerpt).
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
        tables = []
        for tbl in soup.find_all("table"):
            rows = []
            for tr in tbl.find_all("tr"):
                cells = [
                    c.get_text(strip=True).replace("\xa0", " ").strip()
                    for c in tr.find_all(["td", "th"])
                ]
                if any(cells):
                    rows.append(cells)
            if rows:
                tables.append(rows)
        text = soup.get_text("\n")
        text = re.sub(r"[ \t\xa0]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return tables, text[:excerpt_chars]

    @staticmethod
    def _pick_report_rows(tables):
        """Pick the row-richest multi-column table; split header vs rows.

        Header detection is content-driven (first row has >=2 non-empty
        cells, none of which parse as a number/date), mirroring the
        header-location-by-content pattern of the ledger parsers — never a
        fixed row index. With a header, data rows of matching width become
        {header: cell} dicts; width-mismatched rows are kept honestly as
        {"_cells": [...]} rather than mis-zipped. Without a header, every
        row is {"_cells": [...]}. Returns (header_cells, row_dicts).
        """
        def _numlike(c):
            t = (c or "").replace(",", "").replace("$", "").strip("() ").strip()
            if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
                return True
            return bool(re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", t))

        best = []
        for rows in tables or []:
            wide = [r for r in rows if sum(1 for c in r if c) >= 2]
            if len(wide) > len(best):
                best = wide
        if not best:
            return [], []
        first = best[0]
        header_like = (
            sum(1 for c in first if c) >= 2
            and not any(_numlike(c) for c in first if c)
        )
        if not header_like:
            return [], [{"_cells": r} for r in best]
        header = first
        out = []
        for r in best[1:]:
            if len(r) == len(header):
                d = {}
                for i, cell in enumerate(r):
                    key = (header[i] or f"col{i}").strip() or f"col{i}"
                    if key in d:
                        key = f"{key}_{i}"
                    d[key] = cell
                out.append(d)
            else:
                out.append({"_cells": r})
        return header, out

    async def _fetch_apps_page(self, url: str, *, label: str, ident: str = "") -> dict:
        """Cookie-authed GET via the browser context's request API.

        Used for the /proxy.cgi/apps/... patient-module endpoints, which are
        cookie-authed (no rotating session token in the URL — same family as
        get_patient_bills_fast) and which answer HTTP 204 (No Content) when
        the module is empty.

        Why NOT self._goto here: Chromium ABORTS a navigation whose response
        is 204 (net::ERR_ABORTED), which _goto cannot distinguish from the
        transient proxy abort it retries — a plain goto would misreport an
        honest "empty module" as a navigation failure. The request API shares
        the logged-in cookie jar, follows the bare-path 302 into the rotating
        /proxy.cgi/{SESSION}/ path automatically, and returns the TRUE status
        code so 204 can be reported as {"status": "empty"} honestly. Every
        other navigation in this file still goes through _goto.

        Returns {"status": "ok", "html": ...} | {"status": "empty"} |
        {"status": "error", "error": ...}. Never raises.
        """
        if self._page is None:
            return {"status": "error",
                    "error": "browser not started — call start() and login() first"}
        try:
            resp = await self._page.context.request.get(url, timeout=20000)
        except Exception as exc:  # noqa: BLE001 — portal/network failure -> error dict
            logger.warning("Svigg %s fetch failed (%s): %s", label, ident, exc)
            return {"status": "error", "error": f"{label} fetch failed: {exc}"}
        if resp.status == 204:
            return {"status": "empty"}
        if resp.status != 200:
            logger.warning("Svigg %s returned HTTP %d (%s)", label, resp.status, ident)
            return {"status": "error", "error": f"{label} returned HTTP {resp.status}"}
        html = await resp.text()
        canary = self._login_canary_html(html)
        if canary:
            logger.warning("Svigg %s canary tripped (%s): %s", label, ident, canary)
            return {"status": "error", "error": canary}
        return {"status": "ok", "html": html}

    async def _get_patient_module(self, acct: str, rowid: str, *,
                                  path: str, label: str) -> dict:
        """Shared read for the acct+rowid patient-module family (plist / rx /
        aller / immun / pdisplay tabs).

        Patient context is established the same way get_patient_bills_fast
        does — entirely by URL query params (rowid + acct), no select-patient
        step, cookie-authed, no session token. rowid is mandatory (an
        acct-only URL on these endpoint families lands on a search/blank
        view, per get_patient_ledger's discovery note). Generic honest
        extraction: raw tables + text excerpt, nothing invented.
        """
        base = {
            "account_number": acct, "rowid": rowid,
            "tables": [], "text_excerpt": "",
            "status": "error", "source": "svigg_live",
        }
        try:
            from bs4 import BeautifulSoup  # noqa: F401 — parsing happens in the helper
        except ImportError:
            return {**base, "error": "BeautifulSoup (beautifulsoup4) not installed"}
        if not acct or not rowid:
            return {**base,
                    "error": (f"acct and rowid are both required for {label} "
                              "(rowid comes from search_patient())")}
        url = f"{self.BASE_URL}{path}?{urlencode({'acct': acct, 'rowid': rowid})}"
        try:
            fetched = await self._fetch_apps_page(url, label=label, ident=f"acct={acct}")
            if fetched["status"] == "empty":
                return {**base, "status": "empty"}
            if fetched["status"] != "ok":
                return {**base, "error": fetched.get("error", "unknown fetch error")}
            tables, excerpt = self._generic_tables_and_text(fetched["html"])
            logger.info("Svigg %s acct=%s: %d table(s) extracted",
                        label, acct, len(tables))
            return {**base, "status": "ok", "tables": tables, "text_excerpt": excerpt}
        except Exception as exc:  # noqa: BLE001 — portal failure -> error dict, never raise
            logger.warning("Svigg %s failed (acct=%s): %s", label, acct, exc)
            return {**base, "error": f"{label} failed: {exc}"}

    async def get_problem_list(self, acct: str, rowid: str) -> dict:
        """Patient problem list (apps/enc/plist.htm).

        Route captured live 2026-07-01 (HTTP 200). Table parse is
        HAR-derived 2026-07-01; NOT yet live-verified — column semantics are
        unmapped, so problems are raw rows (no invented field names).

          GET /proxy.cgi/apps/enc/plist.htm?acct={acct}&rowid={rowid}
        Bare path — the proxy 302s it into the rotating /proxy.cgi/{SESSION}/
        path; the redirect is followed automatically, so no session-token
        handling is needed (cookie-authed family, same context pattern as
        get_patient_bills_fast; rowid comes from search_patient()).

        Returns:
          {"account_number", "rowid",
           "problems": [ {header: cell, ...} | {"_cells": [...]}, ... ],
           "problems_header": [...],   # [] when no header row was detected
           "raw_tables": [ [[cell, ...], ...], ... ],
           "text_excerpt": str,
           "status": "ok" | "empty" | "error",
           "source": "svigg_live"[, "error": str]}
        Never raises on portal failure.
        """
        out = await self._get_patient_module(
            acct, rowid, path="/proxy.cgi/apps/enc/plist.htm", label="problem list")
        tables = out.pop("tables", [])
        header, rows = self._pick_report_rows(tables)
        ok = out.get("status") == "ok"
        out["problems"] = rows if ok else []
        out["problems_header"] = header if ok else []
        out["raw_tables"] = tables
        return out

    PDISPLAY_TABS = {
        "Info": "patientDisplayInfo.htm",
        "Clinical": "patientDisplayClinical.htm",
        "Scheduling": "patientDisplayScheduling.htm",
        "Ledger": "patientDisplayLedger.htm",
        "Bills": "patientDisplayBills.htm",
        "ProcedureLedger": "ProcedureLedger.htm",
    }

    async def get_patient_display(self, acct: str, rowid: str, tab: str) -> dict:
        """One tab of the pdisplay patient-display family (generic read).

        Routes captured live 2026-07-01 (HTTP 200; empty tabs answer HTTP
        204). Parse is HAR-derived 2026-07-01; NOT yet live-verified —
        generic honest extraction only: raw tables + a text excerpt, no
        invented fields.

          GET /proxy.cgi/apps/pdisplay/patientDisplay{Tab}.htm?acct=&rowid=
          (tab "ProcedureLedger" -> /proxy.cgi/apps/pdisplay/ProcedureLedger.htm)
        Valid tabs: Info, Clinical, Scheduling, Ledger, Bills, ProcedureLedger.

        Patient context = URL query params only (rowid + acct), exactly like
        get_patient_bills_fast — cookie-authed, no session token, no
        select-patient step.

        NOT the system-of-record for balances: the authoritative net balance
        remains get_patient_ledger()/_fetch_payments(). The Ledger/Bills tabs
        here are raw pdisplay views (see get_patient_bills_fast's measured
        pdisplay-vs-ledger discrepancy on acct 2086507).

        Returns {"tab", "account_number", "rowid", "tables": [[...], ...],
                 "text_excerpt": str, "status": "ok"|"empty"|"error",
                 "source": "svigg_live"[, "error"]};
        HTTP 204 -> {"status": "empty", "tab": tab, ...}.
        Never raises on portal failure.
        """
        if tab not in self.PDISPLAY_TABS:
            return {
                "tab": tab, "account_number": acct, "rowid": rowid,
                "tables": [], "text_excerpt": "", "status": "error",
                "source": "svigg_live",
                "error": (f"unknown pdisplay tab {tab!r} — valid: "
                          f"{sorted(self.PDISPLAY_TABS)}"),
            }
        out = await self._get_patient_module(
            acct, rowid,
            path=f"/proxy.cgi/apps/pdisplay/{self.PDISPLAY_TABS[tab]}",
            label=f"pdisplay {tab}")
        out["tab"] = tab
        return out

    async def get_patient_rx(self, acct: str, rowid: str) -> dict:
        """Patient medications module (apps/rx/PtntRx.htm).

        Route captured live 2026-07-01 (HTTP 200; HTTP 204 when the module is
        empty). Parse is HAR-derived 2026-07-01; NOT yet live-verified —
        generic honest extraction (raw tables + text excerpt, no invented
        fields).

          GET /proxy.cgi/apps/rx/PtntRx.htm?acct={acct}&rowid={rowid}
        Cookie-authed acct+rowid family (same context pattern as
        get_patient_bills_fast); HTTP 204 -> {"status": "empty"}.

        Returns {"account_number", "rowid", "tables", "text_excerpt",
                 "status": "ok"|"empty"|"error", "source": "svigg_live"
                 [, "error"]}. Never raises on portal failure.
        """
        return await self._get_patient_module(
            acct, rowid, path="/proxy.cgi/apps/rx/PtntRx.htm", label="patient rx")

    async def get_patient_allergies(self, acct: str, rowid: str) -> dict:
        """Patient allergies module (apps/aller/PtntAller.htm).

        Route captured live 2026-07-01 (HTTP 200; HTTP 204 when the module is
        empty). Parse is HAR-derived 2026-07-01; NOT yet live-verified —
        generic honest extraction (raw tables + text excerpt, no invented
        fields).

          GET /proxy.cgi/apps/aller/PtntAller.htm?acct={acct}&rowid={rowid}
        Cookie-authed acct+rowid family (same context pattern as
        get_patient_bills_fast); HTTP 204 -> {"status": "empty"}.

        Returns {"account_number", "rowid", "tables", "text_excerpt",
                 "status": "ok"|"empty"|"error", "source": "svigg_live"
                 [, "error"]}. Never raises on portal failure.
        """
        return await self._get_patient_module(
            acct, rowid, path="/proxy.cgi/apps/aller/PtntAller.htm",
            label="patient allergies")

    async def get_patient_immunizations(self, acct: str, rowid: str) -> dict:
        """Patient immunizations module (apps/immun/PtntImmun.htm).

        Route captured live 2026-07-01 (HTTP 200; HTTP 204 when the module is
        empty). Parse is HAR-derived 2026-07-01; NOT yet live-verified —
        generic honest extraction (raw tables + text excerpt, no invented
        fields).

          GET /proxy.cgi/apps/immun/PtntImmun.htm?acct={acct}&rowid={rowid}
        Cookie-authed acct+rowid family (same context pattern as
        get_patient_bills_fast); HTTP 204 -> {"status": "empty"}.

        Returns {"account_number", "rowid", "tables", "text_excerpt",
                 "status": "ok"|"empty"|"error", "source": "svigg_live"
                 [, "error"]}. Never raises on portal failure.
        """
        return await self._get_patient_module(
            acct, rowid, path="/proxy.cgi/apps/immun/PtntImmun.htm",
            label="patient immunizations")

    async def get_scan_review_queue(self, provider: str = "", category: str = "",
                                    scan_type: str = "") -> dict:
        """Doctor-review scan queue (apps/scan/scanList_DrReview.htm).

        Route captured live 2026-07-01 (HTTP 200). Queue-table parse is
        HAR-derived 2026-07-01; NOT yet live-verified — rows are
        header-driven dicts when a header row is detected, else
        {"_cells": [...]}; column semantics are unmapped, nothing invented.

          GET /proxy.cgi/apps/scan/scanList_DrReview.htm
              ?REVIEW_PTNT=&ReviewProvider={provider}&cat={category}
              &checkdr=&type={scan_type}
        Blank filter values = all (matches the captured request).
        Cookie-authed apps/ path — no session token.

        Returns {"queue": [...], "queue_header": [...], "raw_tables_count",
                 "filters": {...}, "status": "ok"|"error",
                 "source": "svigg_live"[, "error"]}.
        Never raises on portal failure.
        """
        filters = {"provider": provider, "category": category,
                   "scan_type": scan_type}
        base = {"queue": [], "queue_header": [], "raw_tables_count": 0,
                "filters": filters, "status": "error", "source": "svigg_live"}
        try:
            from bs4 import BeautifulSoup  # noqa: F401 — parsing happens in the helper
        except ImportError:
            return {**base, "error": "BeautifulSoup (beautifulsoup4) not installed"}
        q = urlencode({
            "REVIEW_PTNT": "", "ReviewProvider": provider,
            "cat": category, "checkdr": "", "type": scan_type,
        })
        url = f"{self.BASE_URL}/proxy.cgi/apps/scan/scanList_DrReview.htm?{q}"
        try:
            await self._goto(url, wait_until="networkidle", timeout=20000)
            canary = await self._login_canary_page()
            if canary:
                logger.warning("Svigg scan queue canary: %s", canary)
                return {**base, "error": canary}
            html = await self._page.content()
            tables, _ = self._generic_tables_and_text(html, excerpt_chars=0)
            header, rows = self._pick_report_rows(tables)
            logger.info("Svigg scan queue: %d row(s) across %d table(s)",
                        len(rows), len(tables))
            return {**base, "status": "ok", "queue": rows,
                    "queue_header": header, "raw_tables_count": len(tables)}
        except Exception as exc:  # noqa: BLE001 — portal failure -> error dict
            logger.warning("Svigg scan queue failed: %s", exc)
            return {**base, "error": f"scan review queue fetch failed: {exc}"}

    async def _get_office_widget(self, path: str, label: str) -> dict:
        """Shared read for the off/home dashboard widgets (todo_b / notes_b).

        Bare /proxy.cgi/off/home/... paths — the proxy 302s them into the
        rotating session path; Playwright follows the redirect during the
        _goto, so no token handling is needed. Canary-checked generic honest
        extraction (raw rows, semantics unmapped).
        """
        base = {"items": [], "raw_tables_count": 0, "text_excerpt": "",
                "status": "error", "source": "svigg_live"}
        try:
            from bs4 import BeautifulSoup  # noqa: F401 — parsing happens in the helper
        except ImportError:
            return {**base, "error": "BeautifulSoup (beautifulsoup4) not installed"}
        url = f"{self.BASE_URL}{path}"
        try:
            await self._goto(url, wait_until="networkidle", timeout=20000)
            canary = await self._login_canary_page()
            if canary:
                logger.warning("Svigg %s canary: %s", label, canary)
                return {**base, "error": canary}
            html = await self._page.content()
            tables, excerpt = self._generic_tables_and_text(html)
            rows = [r for tbl in tables for r in tbl]
            logger.info("Svigg %s: %d row(s) across %d table(s)",
                        label, len(rows), len(tables))
            return {**base, "status": "ok", "items": rows,
                    "raw_tables_count": len(tables), "text_excerpt": excerpt}
        except Exception as exc:  # noqa: BLE001 — portal failure -> error dict
            logger.warning("Svigg %s failed: %s", label, exc)
            return {**base, "error": f"{label} fetch failed: {exc}"}

    async def get_office_todo(self) -> dict:
        """Built-in office to-do widget (off/home/todo_b.htm).

        Route captured live 2026-07-01 (HTTP 200). Row parse is HAR-derived
        2026-07-01; NOT yet live-verified — raw rows, semantics unmapped.

          GET /proxy.cgi/off/home/todo_b.htm
        Bare path; the 302 into the rotating session path is followed
        automatically (no token handling).

        Returns {"items": [[cell, ...], ...], "raw_tables_count",
                 "text_excerpt", "status": "ok"|"error",
                 "source": "svigg_live"[, "error"]}.
        Never raises on portal failure.
        """
        return await self._get_office_widget("/proxy.cgi/off/home/todo_b.htm",
                                             "office todo")

    async def get_office_notes(self) -> dict:
        """Built-in office notes widget (off/home/notes_b.htm).

        Route captured live 2026-07-01 (HTTP 200). Row parse is HAR-derived
        2026-07-01; NOT yet live-verified — raw rows, semantics unmapped.

          GET /proxy.cgi/off/home/notes_b.htm
        Bare path; the 302 into the rotating session path is followed
        automatically (no token handling).

        Returns {"items": [[cell, ...], ...], "raw_tables_count",
                 "text_excerpt", "status": "ok"|"error",
                 "source": "svigg_live"[, "error"]}.
        Never raises on portal failure.
        """
        return await self._get_office_widget("/proxy.cgi/off/home/notes_b.htm",
                                             "office notes")

    async def _set_form_field(self, ctx, candidates, value) -> Optional[str]:
        """Set one form control, trying `candidates` names in order.

        Name resolution mirrors the _find_col convention: an exact
        [name=...] match for each candidate first, then a case-insensitive
        substring pass over the form's named controls. Control type is
        detected at runtime (select vs checkbox/radio vs text) — these report
        forms are only known from captures, so nothing is hardcoded blind.

        Returns the actual control name that was set (for honest
        fields_applied reporting) or None if no candidate matched. Raises on
        a failed set so the caller records the field as FAILED, never as
        silently applied.
        """
        el = None
        matched = None
        for cand in candidates:
            el = await ctx.query_selector(f'[name="{cand}"]')
            if el is not None:
                matched = cand
                break
        if el is None:
            named = []
            for c in await ctx.query_selector_all(
                    "input[name], select[name], textarea[name]"):
                named.append((((await c.get_attribute("name")) or ""), c))
            for cand in candidates:
                cl = cand.lower()
                for n, c in named:
                    if cl and cl in n.lower():
                        el, matched = c, n
                        break
                if el is not None:
                    break
        if el is None:
            return None
        tag = ((await el.evaluate("e => e.tagName")) or "").lower()
        if tag == "select":
            try:
                await el.select_option(value=str(value))
            except Exception:  # noqa: BLE001 — fall back to visible-label match
                await el.select_option(label=str(value))
        else:
            typ = ((await el.get_attribute("type")) or "text").lower()
            if typ in ("checkbox", "radio"):
                if value:
                    await el.check()
                else:
                    await el.uncheck()
            else:
                await el.fill(str(value))
        return matched

    async def _run_office_report(self, form_path: str, *, label: str,
                                 set_fields: Optional[list] = None) -> dict:
        """Run one off/reports/* parameter form and return the result HTML.

        Same two-step async-report pattern as _fetch_payments
        (ledgerRptcases.htm, discovered live 2026-06-29):
          1. GET the parameter form (bare /proxy.cgi/off/reports/... path;
             the 302 into the session path is followed automatically).
          2. Best-effort apply `set_fields` — a list of
             (logical_name, candidate_control_names, value) — each failure is
             recorded in fields_failed, non-fatal. Every other control keeps
             the form's own rendered defaults (browser serialization), so
             unknown field defaults are read from the form at runtime, never
             hardcoded guesses.
          3. Click the submit control inside expect_navigation.
          4. If the response is the Navigator FRAMESET (async report ticket),
             resolve <frame name="ReportBody"> to its
             /proxy.cgi/apps/event/RetrieveReport.htm?dt=..&tm=.. src and
             _goto it directly (fixing &amp; and relative paths) — exactly
             like the ledger report.
          5. If there is no frameset AND the URL never left the form, that is
             the canary: a validation bounce — explicit error, never parse
             the form itself as data.

        Returns {"status": "ok", "html", "result_url", "fields_applied",
                 "fields_failed"} or {"status": "error", "error", ...}.
        May raise on navigation failure — public wrappers catch and convert.
        """
        page = self._page
        form_url = f"{self.BASE_URL}{form_path}"
        await self._goto(form_url, wait_until="networkidle", timeout=20000)
        # The bare /off/reports/... GET 302s into the rotating
        # /proxy.cgi/{SESSION}/ path, so page.url — NOT the bare form_url —
        # is the parameter form's real address. The validation-bounce canary
        # below must compare against THIS landed URL: comparing against the
        # bare form_url can never fire once the redirect has happened, and a
        # bounced form would be parsed as report data.
        form_landed_url = page.url
        canary = await self._login_canary_page()
        if canary:
            return {"status": "error", "error": canary, "form_url": form_url}

        applied, failed = [], []
        for logical, candidates, value in (set_fields or []):
            if value in ("", None):
                continue
            try:
                matched = await self._set_form_field(page, candidates, value)
                if matched:
                    applied.append({"field": logical, "control": matched})
                else:
                    failed.append(logical)
                    logger.warning(
                        "Svigg %s report: no form control matched %r "
                        "(candidates %r) — filter NOT applied",
                        label, logical, tuple(candidates))
            except Exception as exc:  # noqa: BLE001 — record honestly, don't abort
                failed.append(logical)
                logger.warning("Svigg %s report: setting %r failed: %s",
                               label, logical, exc)

        submit = await page.query_selector(
            'input[name="Submit"], input[type="submit"]')
        if submit is None:
            return {"status": "error", "form_url": form_url,
                    "fields_applied": applied, "fields_failed": failed,
                    "error": "Execute-Report submit control not found on the form"}
        try:
            async with page.expect_navigation(wait_until="networkidle",
                                              timeout=25000):
                await submit.click()
        except Exception as exc:  # noqa: BLE001 — navigation may already be done
            logger.warning("Svigg %s report submit navigation note: %s", label, exc)

        content = await page.content()
        m = re.search(r'name="ReportBody"\s+src="([^"]+)"', content)
        if m:
            body_src = m.group(1).replace("&amp;", "&")
            body_url = (body_src if body_src.startswith("http")
                        else f"{self.BASE_URL}{body_src}")
            await self._goto(body_url, wait_until="networkidle", timeout=20000)
            content = await page.content()
            result_url = body_url
        else:
            result_url = page.url
            result_path = result_url.split("?")[0].rstrip("/")
            # Bounce detection must survive the session-path rewrite: compare
            # against the LANDED form URL (post-302) and the bare form_url,
            # plus the /proxy.cgi/-relative tail ("off/reports/<page>.htm"),
            # which stays constant even when the form re-renders under a NEW
            # rotating session token.
            form_tail = (
                form_path.split("?")[0].rstrip("/")
                .split("/proxy.cgi/")[-1].lstrip("/")
            )
            bounced = result_path in (
                form_url.split("?")[0].rstrip("/"),
                form_landed_url.split("?")[0].rstrip("/"),
            ) or result_path.endswith("/" + form_tail)
            if bounced:
                return {"status": "error", "form_url": form_url,
                        "fields_applied": applied, "fields_failed": failed,
                        "error": ("report did not navigate off the parameter "
                                  "form and produced no ReportBody frameset — "
                                  "likely a form-validation bounce; refusing "
                                  "to parse the form as data")}
        canary = await self._login_canary_page()
        if canary:
            return {"status": "error", "error": canary, "form_url": form_url,
                    "result_url": result_url,
                    "fields_applied": applied, "fields_failed": failed}
        return {"status": "ok", "html": content, "result_url": result_url,
                "fields_applied": applied, "fields_failed": failed}

    def _report_rows_result(self, res: dict, base: dict, label: str) -> dict:
        """Convert a _run_office_report result into the public rows shape."""
        if res.get("status") != "ok":
            merged = {**base, **{k: v for k, v in res.items() if k != "html"}}
            merged["status"] = "error"
            return merged
        tables, _ = self._generic_tables_and_text(res["html"], excerpt_chars=0)
        header, rows = self._pick_report_rows(tables)
        logger.info("Svigg %s report: %d row(s) parsed", label, len(rows))
        return {**base, "status": "ok", "rows": rows, "header": header,
                "row_count": len(rows), "result_url": res.get("result_url"),
                "fields_applied": res.get("fields_applied", []),
                "fields_failed": res.get("fields_failed", [])}

    async def get_open_balances_report(self, **filters) -> dict:
        """Practice-wide Open Balances report (off/reports/openbal.htm).

        Route captured live 2026-07-01 (HTTP 200): submitting the form
        produces GET /proxy.cgi/{SESSION}/openbal.post?... (the session token
        in that result URL is handled transparently by the browser
        navigation). Row parse is HAR-derived 2026-07-01; NOT yet
        live-verified.

        Flow (same async-report pattern as _fetch_payments):
          1. GET /proxy.cgi/off/reports/openbal.htm — parameter form. The
             rendered defaults (all offices, all providers) are preserved;
             field defaults are read from the form itself at runtime, never
             hardcoded.
          2. Any **filters kwargs are best-effort applied to the same-named
             form controls; unmatched/failed names are reported in
             "fields_failed", never guessed.
          3. If the result is the Navigator frameset (async report ticket),
             the ReportBody frame's apps/event/RetrieveReport.htm src is
             followed exactly like the ledger report.

        Returns {"status": "ok"|"error", "rows": [...], "header": [...],
                 "row_count", "result_url", "fields_applied",
                 "fields_failed", "source": "svigg_live"[, "error"]}.
        Rows are header-driven dicts when a header row is detected, else
        {"_cells": [...]} — column semantics unmapped, nothing invented.
        Never raises on portal failure.
        """
        base = {"rows": [], "header": [], "row_count": 0,
                "status": "error", "source": "svigg_live"}
        try:
            from bs4 import BeautifulSoup  # noqa: F401 — parsing happens in the helper
        except ImportError:
            return {**base, "error": "BeautifulSoup (beautifulsoup4) not installed"}
        try:
            res = await self._run_office_report(
                "/proxy.cgi/off/reports/openbal.htm",
                label="open balances",
                set_fields=[(k, (k,), v) for k, v in (filters or {}).items()])
            return self._report_rows_result(res, base, "open balances")
        except Exception as exc:  # noqa: BLE001 — portal failure -> error dict
            logger.warning("Svigg open balances report failed: %s", exc)
            return {**base, "error": f"open balances report failed: {exc}"}

    async def get_referrals_in_report(self, date_from: str = "",
                                      date_to: str = "") -> dict:
        """Referrals-received report (off/reports/refIn.htm).

        Route captured live 2026-07-01 (HTTP 200). Row parse is HAR-derived
        2026-07-01; NOT yet live-verified.

        Flow: GET /proxy.cgi/off/reports/refIn.htm (parameter form, defaults
        preserved) -> submit -> ReportBody/RetrieveReport pattern, exactly
        like the ledger report (_fetch_payments).

        The exact date-range control names on this form were NOT captured
        offline, so they are resolved at runtime against candidate names
        ("DtFrom"/"FromDate"/"from", "DtTo"/"ToDate"/"until") — exact match
        first, then substring, mirroring _find_col. A date that cannot be
        matched to a control is reported in "fields_failed" (the report then
        runs on the form's own defaults), never silently guessed.

        Returns {"status": "ok"|"error", "rows": [...], "header": [...],
                 "row_count", "result_url", "fields_applied",
                 "fields_failed", "source": "svigg_live"[, "error"]}.
        Never raises on portal failure.
        """
        base = {"rows": [], "header": [], "row_count": 0,
                "status": "error", "source": "svigg_live"}
        try:
            from bs4 import BeautifulSoup  # noqa: F401 — parsing happens in the helper
        except ImportError:
            return {**base, "error": "BeautifulSoup (beautifulsoup4) not installed"}
        try:
            res = await self._run_office_report(
                "/proxy.cgi/off/reports/refIn.htm",
                label="referrals in",
                set_fields=[
                    ("date_from", ("DtFrom", "FromDate", "from"), date_from),
                    ("date_to", ("DtTo", "ToDate", "until"), date_to),
                ])
            return self._report_rows_result(res, base, "referrals in")
        except Exception as exc:  # noqa: BLE001 — portal failure -> error dict
            logger.warning("Svigg referrals-in report failed: %s", exc)
            return {**base, "error": f"referrals-in report failed: {exc}"}

    async def get_payments_by_provider(self, date_from: str = "",
                                       date_to: str = "") -> dict:
        """Payments-by-provider report (off/reports/paymProv.htm).

        Route + result contract captured live 2026-07-01 (HTTP 200):
        submitting the form produces
          GET /proxy.cgi/{SESSION}/paymProv.post?AllOffices=...&AllOperators=...
              &AllProviders=...&AllTreatOffices=...&DtFrom={date_from}
              &DtTo={date_to}&...
        DtFrom/DtTo are the captured date control names; every other captured
        key (AllOffices/AllOperators/AllProviders/AllTreatOffices/...) is
        filled from the form's own rendered defaults via browser
        serialization — never hardcoded. Row parse is HAR-derived 2026-07-01;
        NOT yet live-verified.

        Flow: GET /proxy.cgi/off/reports/paymProv.htm (form) -> fill
        DtFrom/DtTo if given -> submit -> ReportBody/RetrieveReport pattern
        if the result is an async report ticket (same as _fetch_payments).

        Returns {"status": "ok"|"error", "rows": [...], "header": [...],
                 "row_count", "result_url", "fields_applied",
                 "fields_failed", "source": "svigg_live"[, "error"]}.
        Never raises on portal failure.
        """
        base = {"rows": [], "header": [], "row_count": 0,
                "status": "error", "source": "svigg_live"}
        try:
            from bs4 import BeautifulSoup  # noqa: F401 — parsing happens in the helper
        except ImportError:
            return {**base, "error": "BeautifulSoup (beautifulsoup4) not installed"}
        try:
            res = await self._run_office_report(
                "/proxy.cgi/off/reports/paymProv.htm",
                label="payments by provider",
                set_fields=[
                    ("date_from", ("DtFrom",), date_from),
                    ("date_to", ("DtTo",), date_to),
                ])
            return self._report_rows_result(res, base, "payments by provider")
        except Exception as exc:  # noqa: BLE001 — portal failure -> error dict
            logger.warning("Svigg payments-by-provider report failed: %s", exc)
            return {**base, "error": f"payments-by-provider report failed: {exc}"}

    async def _acquire_session_token(self) -> Optional[str]:
        """Get a FRESH rotating session token for the /proxy.cgi/{SESSION}/...
        booking/calendar family by loading the calendar frameset and reading
        a child frame's URL (same discovery as get_appointment_calendar).

        Tokens are NEVER cached — they rotate nearly every request — so call
        this immediately before building the URL that needs one. Returns the
        token string, or None (callers must fail closed).
        """
        page = self._page
        await self._goto(f"{self.BASE_URL}/proxy.cgi/app/enc/cal.htm",
                         wait_until="load", timeout=20000)
        await page.wait_for_timeout(3000)  # frameset never goes networkidle
        for frame in page.frames:
            token = self._session_token_from_url(frame.url)
            if token:
                return token
        return None

    async def search_open_slots(self, appt_type: str = "", office: str = "",
                                cpt: str = "", duration_min: Optional[int] = None,
                                date_from: str = "", date_to: str = "") -> dict:
        """Open-slot search — the portal's "find next open appointment" form.

        Route captured live 2026-07-01/07-03 (HTTP 200):
          GET /proxy.cgi/{SESSION}/sched.htm with the captured query contract
          (ApptType, Office, cpt1..cpt4, dur1..dur4, earl0..earl6,
           from0..from6, to0..to6, res1..res4, make, ftm, ttm, sfrom, suntil,
           Search).
        That query string is NOT reconstructed offline: the sched form is
        loaded first and its own rendered defaults preserved; only the
        caller-supplied filters are overridden (appt_type -> ApptType,
        office -> Office, cpt -> cpt1, duration_min -> dur1,
        date_from -> sfrom, date_to -> suntil) and the form's own Search
        control is submitted, so the browser serializes every remaining
        captured key from the live form. The result-page slot parse is
        HAR-derived 2026-07-01/07-03; NOT yet live-verified, and the
        result-row semantics are EXPERIMENTAL — "candidate_slots" are simply
        the parsed rows carrying a time-like token (HH:MM), not confirmed
        bookable slots.

        Session token: acquired FRESH from the calendar frameset immediately
        before the sched GET (never cached — it rotates nearly every
        request).

        Returns {"status": "ok"|"error", "candidate_slots": [...],
                 "rows": [...], "header": [...], "fields_applied",
                 "fields_failed", "result_url", "source": "svigg_live"
                 [, "error"]}.
        Never raises on portal failure; never fabricates a slot.
        """
        base = {"candidate_slots": [], "rows": [], "header": [],
                "fields_applied": [], "fields_failed": [],
                "status": "error", "source": "svigg_live"}
        try:
            from bs4 import BeautifulSoup  # noqa: F401 — parsing happens in the helper
        except ImportError:
            return {**base, "error": "BeautifulSoup (beautifulsoup4) not installed"}
        page = self._page
        try:
            token = await self._acquire_session_token()
            if not token:
                return {**base,
                        "error": ("could not acquire a session token from the "
                                  "calendar frameset — session may have "
                                  "expired or login is required")}
            sched_url = f"{self.BASE_URL}/proxy.cgi/{token}/sched.htm"
            await self._goto(sched_url, wait_until="load", timeout=20000)
            await page.wait_for_timeout(1500)  # legacy page; let controls render
            canary = await self._login_canary_page()
            if canary:
                return {**base, "error": canary}
            if await page.query_selector('[name="Search"]') is None:
                return {**base,
                        "error": ("sched.htm did not render the expected "
                                  "Search control — unexpected layout or "
                                  "expired session; refusing to parse")}

            overrides = [
                ("appt_type", ("ApptType",), appt_type),
                ("office", ("Office",), office),
                ("cpt", ("cpt1",), cpt),
                ("duration_min", ("dur1",), duration_min),
                ("date_from", ("sfrom",), date_from),
                ("date_to", ("suntil",), date_to),
            ]
            applied, failed = [], []
            for logical, cands, value in overrides:
                if value in ("", None):
                    continue
                try:
                    matched = await self._set_form_field(page, cands, value)
                    if matched:
                        applied.append({"field": logical, "control": matched})
                    else:
                        failed.append(logical)
                        logger.warning("Svigg slot search: no control matched "
                                       "%r — filter NOT applied", logical)
                except Exception as exc:  # noqa: BLE001 — record honestly, don't abort
                    failed.append(logical)
                    logger.warning("Svigg slot search: setting %r failed: %s",
                                   logical, exc)

            search_ctl = await page.query_selector(
                'input[name="Search"], button[name="Search"]')
            if search_ctl is None:
                return {**base, "fields_applied": applied,
                        "fields_failed": failed,
                        "error": ("Search control present but not clickable "
                                  "(unexpected element type)")}
            try:
                async with page.expect_navigation(wait_until="load",
                                                  timeout=25000):
                    await search_ctl.click()
            except Exception as exc:  # noqa: BLE001 — legacy forms may re-render in place
                logger.warning("Svigg slot search submit navigation note: %s", exc)
            await page.wait_for_timeout(1500)

            canary = await self._login_canary_page()
            if canary:
                return {**base, "fields_applied": applied,
                        "fields_failed": failed, "error": canary}

            # Result layout unknown offline — gather the page AND any child
            # frames (best-effort) and parse all tables generically.
            htmls = [await page.content()]
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    htmls.append(await frame.content())
                except Exception:  # noqa: BLE001 — best-effort frame read
                    pass
            tables = []
            for h in htmls:
                t, _ = self._generic_tables_and_text(h, excerpt_chars=0)
                tables.extend(t)
            header, rows = self._pick_report_rows(tables)

            time_re = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", re.I)

            def _row_cells(r):
                return r.get("_cells") if "_cells" in r else list(r.values())

            candidate_slots = [
                r for r in rows
                if any(time_re.search(c or "") for c in _row_cells(r))
            ]
            logger.info("Svigg slot search: %d row(s), %d time-bearing "
                        "candidate(s)", len(rows), len(candidate_slots))
            return {**base, "status": "ok", "candidate_slots": candidate_slots,
                    "rows": rows, "header": header,
                    "fields_applied": applied, "fields_failed": failed,
                    "result_url": page.url}
        except Exception as exc:  # noqa: BLE001 — portal failure -> error dict
            logger.warning("Svigg slot search failed: %s", exc)
            return {**base, "error": f"open-slot search failed: {exc}"}

    async def get_oneday_view(self, date_mdy: str, resource_id: str) -> dict:
        """Single-day calendar view for one resource — {resource}/oneday.htm.

        Route captured live 2026-07-01/07-03 (HTTP 200):
          GET /proxy.cgi/{SESSION}/{resource_id}/oneday.htm?dt={date_mdy}
        `date_mdy` is MM/DD/YYYY (the portal's native date format).
        `resource_id` semantics are EXPERIMENTAL — it is the path segment
        between the session token and oneday.htm in the capture, most likely
        a provider/resource id, but that mapping is unconfirmed; do NOT treat
        rows as belonging to a specific provider until verified live. Parse
        is HAR-derived 2026-07-01/07-03; NOT yet live-verified — raw rows,
        nothing invented.

        Session token: acquired FRESH from the calendar frameset immediately
        before this GET (never cached). Calendar-family page, so it is loaded
        with wait_until='load' + an explicit settle delay (framesets never go
        networkidle).

        Returns {"date", "resource_id", "tables", "rows", "text_excerpt",
                 "status": "ok"|"error", "source": "svigg_live"[, "error"]}.
        Never raises on portal failure.
        """
        base = {"date": date_mdy, "resource_id": resource_id,
                "tables": [], "rows": [], "text_excerpt": "",
                "status": "error", "source": "svigg_live"}
        try:
            from bs4 import BeautifulSoup  # noqa: F401 — parsing happens in the helper
        except ImportError:
            return {**base, "error": "BeautifulSoup (beautifulsoup4) not installed"}
        if not date_mdy or not resource_id:
            return {**base,
                    "error": "date_mdy (MM/DD/YYYY) and resource_id are both required"}
        page = self._page
        try:
            token = await self._acquire_session_token()
            if not token:
                return {**base,
                        "error": ("could not acquire a session token from the "
                                  "calendar frameset — session may have "
                                  "expired or login is required")}
            url = (f"{self.BASE_URL}/proxy.cgi/{token}/{resource_id}/"
                   f"oneday.htm?dt={date_mdy}")
            await self._goto(url, wait_until="load", timeout=20000)
            await page.wait_for_timeout(1500)
            canary = await self._login_canary_page()
            if canary:
                return {**base, "error": canary}
            htmls = [await page.content()]
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    htmls.append(await frame.content())
                except Exception:  # noqa: BLE001 — best-effort frame read
                    pass
            tables = []
            excerpt = ""
            for h in htmls:
                t, ex = self._generic_tables_and_text(h)
                tables.extend(t)
                if not excerpt:
                    excerpt = ex
            rows = [r for tbl in tables for r in tbl]
            if not tables and len(excerpt.strip()) < 40:
                return {**base,
                        "error": ("oneday.htm rendered no tables and almost "
                                  "no text — expected day-view layout not "
                                  "confirmed (wrong resource_id? expired "
                                  "session?); refusing to report an empty "
                                  "day as real data")}
            logger.info("Svigg oneday view resource=%s: %d row(s) across %d "
                        "table(s)", resource_id, len(rows), len(tables))
            return {**base, "status": "ok", "tables": tables, "rows": rows,
                    "text_excerpt": excerpt}
        except Exception as exc:  # noqa: BLE001 — portal failure -> error dict
            logger.warning("Svigg oneday view failed (resource=%s): %s",
                           resource_id, exc)
            return {**base, "error": f"oneday view fetch failed: {exc}"}

    async def prepare_appointment_update(self, ticket_number: str,
                                         room: str = "", staged: str = "",
                                         note: str = "",
                                         missed: str = "") -> dict:
        """PREPARE-ONLY builder for the front-desk check-in/staging write.

        Target (captured live 2026-07-03 — a real POST observed in the HAR):
          POST /proxy.cgi/{SESSION}/off/home/appt_u.htm
          body keys: CardPresent, Note, Room, Staged, Submit, TFORMCOUNT,
                     TicketNumber, missed, reverse, sort, todayonly

        THIS METHOD NEVER POSTS. There is deliberately NO code path in this
        class that submits this payload — not even behind the
        BOOKING_EXECUTE_ENABLED / CREATE_EXECUTE_ENABLED kill-switches —
        because appt_u semantics (which key combinations check a patient in
        vs stage vs mark missed, and what TFORMCOUNT/reverse/sort/todayonly
        must carry) need one supervised live run before any commit path is
        added. Until then this is a dry-run payload builder only; it performs
        zero network I/O (async only for call-site uniformity with the other
        scraper methods). If a commit path is ever added, it must go through
        the module-level triple-gated kill-switch pattern (see the
        BOOKING/CREATE blocks at the top of this file).

        The captured keys CardPresent, Submit, TFORMCOUNT, reverse, sort and
        todayonly had live values in the HAR that were NOT preserved offline —
        they are returned as "" and listed in "unknown_defaults"; a
        supervised run must read the real values from the rendered form.
        Nothing is guessed.

        Returns {"status": "prepared", "would_post": {...}, "target": ...,
                 "unknown_defaults": [...], "note": ...} or
        {"status": "error", "error": ...} if ticket_number is missing.
        """
        if not ticket_number:
            return {"status": "error",
                    "error": "ticket_number is required to prepare an appt_u payload"}
        payload = {
            "TicketNumber": str(ticket_number),
            "Room": room or "",
            "Staged": staged or "",
            "Note": note or "",
            "missed": missed or "",
            # Captured keys whose live default values were NOT preserved
            # offline — left explicitly blank, never guessed:
            "CardPresent": "",
            "Submit": "",
            "TFORMCOUNT": "",
            "reverse": "",
            "sort": "",
            "todayonly": "",
        }
        return {
            "status": "prepared",
            "would_post": payload,
            "target": (f"{self.BASE_URL}/proxy.cgi/{{SESSION}}/off/home/"
                       "appt_u.htm (session token must be re-read from the "
                       "live frame URL immediately before any future "
                       "supervised POST)"),
            "unknown_defaults": ["CardPresent", "Submit", "TFORMCOUNT",
                                 "reverse", "sort", "todayonly"],
            "note": ("execution blocked — appt_u semantics need one "
                     "supervised live run before any commit path is added"),
        }


# ---------------------------------------------------------------------------
# Standalone CLI for testing
# ---------------------------------------------------------------------------

async def _main():
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 svigg_scraper.py <last_name> [first_name]")
        print("       python3 svigg_scraper.py --status")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO)
    # Headless by DEFAULT — even this manual debug runner must not pop a visible
    # browser window unless a developer explicitly opts in with EMR_HEADED=1.
    # (App + MCP runtime paths are already headless=True; this closes the only
    # remaining path that could open a window a user can see.)
    scraper = SviggScraper(headless=(os.environ.get("EMR_HEADED") != "1"))

    try:
        await scraper.start()

        if sys.argv[1] == "--status":
            ok = await scraper.login()
            print(json.dumps({"logged_in": ok}))
            return

        last_name = sys.argv[1]
        first_name = sys.argv[2] if len(sys.argv) > 2 else ""

        ok = await scraper.login()
        if not ok:
            print(json.dumps({"error": "Login failed"}))
            return

        results = await scraper.search_and_summarize(last_name, first_name)
        print(json.dumps(results, indent=2, default=str))

    finally:
        await scraper.stop()


if __name__ == "__main__":
    asyncio.run(_main())

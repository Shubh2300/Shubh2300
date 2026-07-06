"""Graft-ready fixes for SviggScraper cancel/booking reliability.

Companion to MCP_CANCEL_BOOKING_FIX.md (defects D1–D5) and
SVIGG_SCHEDULING_CONTRACT.md (HAR-derived wire contract). Each method below is
written in the repo's existing idiom (result dicts, stage names, fail-closed
gates) and is meant to be merged into `python/integrations/svigg_scraper.py`
inside `class SviggScraper`. `MERGE:` comments mark the integration points.

Nothing here loosens a safety gate: every destructive step keeps
confirm=True + CANCEL_ALLOWED_ACCT_NAMES binding + an identity guard, and every
"success" claim requires a date-guarded re-read or downgrades itself honestly.
"""

import re

TFORMCOUNT_RE = re.compile(
    r'name=["\']?TFORMCOUNT["\']?\s+value=["\']?(\d+)', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Shared helpers (MERGE: add as SviggScraper methods)
# ---------------------------------------------------------------------------

async def _fresh_tformcount(self, frame) -> int | None:
    """Parse TFORMCOUNT out of the CURRENTLY rendered form in `frame`.

    The portal ignores posts carrying a stale TFORMCOUNT (it returns HTTP 200
    and does nothing — the root of the 'cancel said ok but appt still there'
    class of bugs, defect D1). Therefore: parse fresh before EVERY step, never
    compute (+2 on the grid path vs +1 on the resched path per the HARs — the
    arithmetic is not stable enough to trust).
    """
    try:
        html = await frame.content()
    except Exception:
        return None
    m = TFORMCOUNT_RE.search(html)
    return int(m.group(1)) if m else None


def _date_filter_ok(self, cal: list) -> bool:
    """True if get_appointment_calendar actually applied the date filter.

    MERGE: same predicate patch 4's H1 guard uses on the PRE-read; defect D3
    is that the POST-cancel re-read skips it, which turns a fallback-to-today
    grid into a false `verified: True`.
    """
    return bool(cal) and any(
        isinstance(a, dict) and a.get("date_filter_applied") for a in cal)


async def _click_and_wait(self, frame, selector: str, timeout_ms: int = 15000):
    """Click a control in `frame` and wait for THAT frame to navigate.

    Replaces every `click(...); wait_for_timeout(...)` + scan-all-frames pair
    (defect D1). The returned document is by construction the fresh render of
    the frame we acted in — a stale frame elsewhere in the frameset can no
    longer be mistaken for the result.
    """
    async with frame.expect_navigation(timeout=timeout_ms):
        await frame.click(selector)
    return frame


# ---------------------------------------------------------------------------
# NEW — Path B cancel, encounter-keyed (preferred).  HAR _6, proven live twice.
# ---------------------------------------------------------------------------

async def cancel_appointment_by_enc(
    self,
    *,
    enc: str,
    acct: str,
    last_name: str,
    date: str,
    reason: str = "or",
    confirm: bool = False,
) -> dict:
    """Cancel a Svigg appointment by its encounter id. DESTRUCTIVE, fail-closed.

    Wire contract (HAR _6, two complete live cancels):
        resched.htm?enc=<ENC>                      -> appointment edit form
        resched_p.htm?TFORMCOUNT=<N>&Delete=Delete -> confirm page (no delete yet)
        cancel2_p?TFORMCOUNT=<fresh>&CancelReason=<or|pr>&Yes=Yes  -> DELETES

    Why this path is primary: `enc` uniquely identifies the appointment, so
    there is no pixel-cell targeting, no name-text clicking, and no ambiguity
    with multiple same-day appointments (defect D2 does not exist here).

    `enc` sources (harvest upstream, pass in):
      - front-desk appointment list: each row's edit link is
        appt_e.htm?date=..&time=..&enc=<ENC>&prov=..
        (MERGE: expose `enc` per row in the svigg_schedule_day reader)
      - patient chart: plist.htm?rowid=<ROWID>&acct=<ACCT> -> encounter list.

    Returns:
      {status:"execute_blocked", ...}            gate closed
      {status:"error", stage, error}             fail-closed abort, nothing deleted
      {status:"cancelled", verified:true, ...}   deleted AND gone on re-read
      {status:"cancel_submitted_unverified",...} deleted-per-flow but re-read
                                                 could not honestly verify
    """
    page = self._page

    if reason not in ("or", "pr"):
        return {"status": "error", "stage": "args",
                "error": f"reason must be 'or' or 'pr', got {reason!r}"}
    if not str(enc).isdigit():
        return {"status": "error", "stage": "args",
                "error": f"enc must be a numeric encounter id, got {enc!r}"}

    # ---- GATE (identical posture to the grid path, checked FIRST) --------
    bound_name = CANCEL_ALLOWED_ACCT_NAMES.get(str(acct))          # noqa: F821  (MERGE: module global)
    name_ok = bound_name is not None and bound_name in last_name.lower()
    if not (confirm is True and name_ok):
        return {"status": "execute_blocked",
                "reason": ("cancel is DESTRUCTIVE and fail-closed: requires "
                           "confirm=True AND acct in the cancel allowlist AND "
                           f"last_name matching that acct's bound name "
                           f"(confirm={confirm}, acct_allowed="
                           f"{bound_name is not None}, name_ok={name_ok})"),
                "allowed_accts": sorted(CANCEL_ALLOWED_ACCT_NAMES)}  # noqa: F821

    try:
        # ---- 1. Open the appointment edit form by encounter id -----------
        # MERGE: reuse however the scraper currently reaches the chart/appt
        # frameset; the edit form URL is .../{SID}/resched.htm?enc=<ENC>.
        edit_frame = await self._open_resched_for_enc(enc)           # MERGE: nav helper
        if edit_frame is None:
            return {"status": "error", "stage": "resched_open",
                    "error": f"resched.htm?enc={enc} did not render"}

        # ---- 2. Identity guard on the rendered edit form ------------------
        html = (await edit_frame.content()).lower()
        if last_name.lower() not in html:
            return {"status": "error", "stage": "identity_guard",
                    "error": f"resched form for enc={enc} does not show the "
                             f"target patient name {last_name!r} — ABORTED "
                             "before Delete"}
        if await self._fresh_tformcount(edit_frame) is None:
            return {"status": "error", "stage": "resched_form",
                    "error": "no TFORMCOUNT on resched form — stale/unexpected "
                             "render, refusing to Delete"}

        # ---- 3. Delete -> confirm page (nothing deleted yet) ---------------
        # resched_p button semantics, all HAR-observed: Delete=proceed to
        # confirm, Cancel=exit(no-op), Submit=save edits(no-op for deletion).
        await self._click_and_wait(edit_frame, 'input[name="Delete"]')

        confirm_html = (await edit_frame.content()).lower()
        if "cancel2_p" not in confirm_html:
            return {"status": "error", "stage": "confirm_page",
                    "error": "confirm page (cancel2_p form) did not render "
                             "after Delete — nothing was deleted"}
        if await self._fresh_tformcount(edit_frame) is None:
            return {"status": "error", "stage": "confirm_page",
                    "error": "no TFORMCOUNT on cancel2_p confirm form — "
                             "refusing to confirm against a stale render"}

        # ---- 4. CancelReason + Yes — the ONLY request that deletes --------
        await edit_frame.select_option('select[name="CancelReason"]', reason)
        await self._click_and_wait(edit_frame, 'input[name="Yes"]')

        # ---- 5. Verify honestly (date-guarded re-read, defect D3) ---------
        cal_after = await self.get_appointment_calendar(date)
        if not self._date_filter_ok(cal_after):
            return {"status": "cancel_submitted_unverified",
                    "enc": enc, "date": date, "reason": reason,
                    "warning": ("cancel flow completed but the verification "
                                "re-read could not apply the date filter — "
                                "verify manually; NOT claiming verified")}
        still_there = [a for a in cal_after if isinstance(a, dict)
                      and last_name.lower()
                      in str(a.get("patient_name", "")).lower()]
        return {"status": "cancelled", "verified": not still_there,
                "enc": enc, "date": date, "reason": reason,
                "remaining_matching_rows": len(still_there),
                "warning": ("" if not still_there else
                            "re-read still shows a matching row (another appt "
                            "for this patient, or the delete did not persist) "
                            "— verify manually")}
    except Exception as exc:
        return {"status": "error", "stage": "cancel_by_enc", "error": f"{exc}"}


# ---------------------------------------------------------------------------
# HARDENED — Path A grid cancel (fallback when no enc is available)
# Replaces steps 3–6 of the existing cancel_appointment (defects D1, D2, D3).
# The args/gate/H1 sections of the existing method stay exactly as they are.
# ---------------------------------------------------------------------------

async def _grid_cancel_steps(self, *, grid_frame, acct, date, last_name,
                             reason, before_rows) -> dict:
    """MERGE: body replacing existing cancel_appointment steps 3-6."""
    # ---- 3. Open the appt cell via its ACTUAL mre? anchor (D2 fix) --------
    # The cell is an <a href="mre?x=..&y=..&r=..">. Target anchors, not bare
    # text; require exactly one candidate — with several (two same-day appts,
    # r=0/r=1) return them for disambiguation instead of guessing.
    anchors = grid_frame.locator(
        f'a[href*="mre?"]:has-text("{last_name}")')
    n = await anchors.count()
    if n == 0:
        return {"status": "error", "stage": "locate_cell",
                "error": f"no mre? cell anchor matching {last_name!r} on the "
                         f"{date} grid"}
    if n > 1:
        hrefs = [await anchors.nth(i).get_attribute("href") for i in range(n)]
        return {"status": "ambiguous", "stage": "locate_cell",
                "candidates": hrefs,
                "error": f"{n} appointment cells match {last_name!r} on {date} "
                         "— pass a slot time (or use cancel_appointment_by_enc) "
                         "to disambiguate; refusing to guess"}
    await self._click_and_wait(grid_frame, f'a[href*="mre?"]:has-text("{last_name}")')

    # ---- 4. Identity guard on the mre edit form (same frame, fresh render) -
    hh = (await grid_frame.content())
    if 'name="Delete"' not in hh or last_name.lower() not in hh.lower():
        return {"status": "error", "stage": "identity_guard",
                "error": f"mre edit form did not show the target patient name "
                         f"{last_name!r} — ABORTED before Delete"}
    if await self._fresh_tformcount(grid_frame) is None:
        return {"status": "error", "stage": "identity_guard",
                "error": "no TFORMCOUNT on mre form — stale render, aborting"}

    # ---- 5. Delete -> cancel_p confirm (fresh render, no frame scans) ------
    await self._click_and_wait(grid_frame, 'input[name="Delete"]')
    ch = await grid_frame.content()
    if "cancel_p" not in ch:
        return {"status": "error", "stage": "confirm_page",
                "error": "Confirm Cancellation (cancel_p) did not render after "
                         "Delete — nothing was deleted"}
    if await self._fresh_tformcount(grid_frame) is None:
        return {"status": "error", "stage": "confirm_page",
                "error": "no TFORMCOUNT on cancel_p form — refusing to confirm"}
    # REQUIRED: reason BEFORE Yes (form bounces otherwise; HAR-proven).
    await grid_frame.select_option('select[name="CancelReason"]', reason)
    await self._click_and_wait(grid_frame, 'input[name="Yes"]')

    # ---- 6. Verify with the SAME date guard as the pre-read (D3 fix) ------
    cal_after = await self.get_appointment_calendar(date)
    if not self._date_filter_ok(cal_after):
        return {"status": "cancel_submitted_unverified", "date": date,
                "reason": reason, "rows_before": len(before_rows),
                "warning": ("cancel flow completed but the verification "
                            "re-read fell back to the wrong day — verify "
                            "manually; NOT claiming verified")}
    after_rows = [a for a in cal_after if isinstance(a, dict)
                  and last_name.lower()
                  in str(a.get("patient_name", "")).lower()]
    verified = len(after_rows) < len(before_rows)
    return {"status": "cancelled", "verified": verified, "date": date,
            "reason": reason, "rows_before": len(before_rows),
            "remaining_test_rows": len(after_rows),
            "warning": ("" if verified else
                        "cancel submitted but calendar re-read still shows a "
                        "matching row — verify manually")}


# ---------------------------------------------------------------------------
# D6 — NAME-ORDER TOLERANCE (booking blocker, user-reproduced 2026-07-06):
# booking "test patient" failed while "patient test" worked, because a bare
# typed name is interpreted in ONE fixed order (last first). A staff member
# should be able to type either order. MERGE: module-level helper; call it
# from every name-search entry point (resolve_patient's name fallback, the
# app chat agent's patient lookup, and the MCP lookup_patient) so all three
# stay in lockstep.
# ---------------------------------------------------------------------------

def name_order_variants(raw: str) -> list[tuple[str, str]]:
    """Expand a free-typed patient name into (last, first) interpretations.

    Rules (in order):
      - "Last, First" comma form is authoritative -> exactly one variant.
      - Bare ONE token -> last-name-only search: (token, "").
      - Bare TWO tokens "A B" is genuinely ambiguous -> BOTH orders:
        [(A, B), (B, A)]  e.g. "test patient" -> ("test","patient") AND
        ("patient","test") — the second is what matches "Patients 1, Test".
      - THREE+ tokens -> both split points that respect multi-word last names:
        (all-but-last as last, last token as first) and
        (first token as first, rest as last), both orders deduped.

    The caller MUST keep resolution fail-closed:
      - acct-first resolution still wins over any name interpretation
        (_pick_acct_match unchanged — a unique acct match cannot be vetoed).
      - Run the search for EACH variant, collect hits, and dedupe the union
        by acct number. Exactly ONE distinct patient across all variants ->
        proceed. MORE than one distinct patient -> return
        {status:"ambiguous_name", candidates:[...]} and stop — never pick a
        winner between two different real patients on word order.
    """
    tokens = [t for t in str(raw or "").replace(",", " , ").split() if t]
    if not tokens:
        return []
    if "," in tokens:
        i = tokens.index(",")
        last = " ".join(tokens[:i]).strip()
        first = " ".join(tokens[i + 1:]).strip()
        return [(last, first)] if last else []
    if len(tokens) == 1:
        return [(tokens[0], "")]
    if len(tokens) == 2:
        a, b = tokens
        return [(a, b), (b, a)]
    joined_last_head = (" ".join(tokens[:-1]), tokens[-1])   # "Patients 1" + "Test"
    joined_last_tail = (" ".join(tokens[1:]), tokens[0])     # rest + first-token-as-first
    out, seen = [], set()
    for v in (joined_last_head, joined_last_tail,
              (joined_last_head[1], joined_last_head[0]),
              (joined_last_tail[1], joined_last_tail[0])):
        key = (v[0].lower(), v[1].lower())
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


# MERGE sketch for the name-fallback inside resolve_patient (idiom-matched):
#
#     hits_by_acct: dict[str, dict] = {}
#     for last, first in name_order_variants(raw_name):
#         for h in await self.search_patient(last, first):
#             acct_key = str(h.get("acct", "")).strip()
#             if acct_key:
#                 hits_by_acct.setdefault(acct_key, h)
#     if len(hits_by_acct) == 1:
#         resolved = next(iter(hits_by_acct.values()))   # resolved_by:"name_either_order"
#     elif len(hits_by_acct) > 1:
#         return {"status": "ambiguous_name",
#                 "candidates": [{"acct": a, "name": h.get("patient_name", "")}
#                                for a, h in hits_by_acct.items()],
#                 "error": "multiple distinct patients match this name in "
#                          "either word order — specify 'Last, First' or the "
#                          "account number; refusing to guess"}
#
# Unit cases to add to tests_name_variants.py:
#   name_order_variants("test patient")   == [("test","patient"), ("patient","test")]
#   name_order_variants("Patients 1, Test") == [("Patients 1","Test")]
#   name_order_variants("test patients 1") covers ("patients 1","test")
#   comma form NEVER expands to the swapped order (it is authoritative).


# ---------------------------------------------------------------------------
# BOOKING (defect D5) — three MERGE points inside the existing book_appointment
# ---------------------------------------------------------------------------
#
# (a) Select failures are FATAL — never POST bk_p with cpt00/prov blank.
#     Replace the current best-effort selects with:
#
#         try:
#             await conf_frame.select_option('select[name="cpt00"]', appt_type)
#         except Exception as exc:
#             return {"status": "error", "stage": "form_fill", "field": "cpt00",
#                     "error": f"could not select cpt00={appt_type!r}: {exc} — "
#                              "refusing to submit (a blank cpt00 bounces "
#                              "silently with HTTP 200; HAR-proven 6x)"}
#         # ... same pattern for prov ...
#
# (b) Detect the silent validation bounce after the POST. The bounce signature
#     (HAR _4/_5): HTTP 200 + the SAME bk_p form re-rendered with TFORMCOUNT
#     advanced by 2; the one observed success returned HTTP 302 + frameset
#     reload. After the Submit click:
#
#         post_html = await conf_frame.content()
#         if 'action="bk_p"' in post_html or "name=\"cpt00\"" in post_html:
#             return {"status": "validation_bounced",
#                     "detail": "portal re-rendered the booking form (silent "
#                               "validation failure) — booking did NOT land",
#                     "tformcount_after": await self._fresh_tformcount(conf_frame)}
#
# (c) Retire `submitted`/`submitted_overbooked` as terminals. Success is ONLY
#     `submitted_verified`: after (b) passes, re-read the grid with the same
#     _date_filter_ok guard and require the patient on the target date/slot;
#     otherwise return `submit_unconfirmed` with the page evidence. (The app's
#     07-05 scraper already adopted this contract — port it here so the MCP
#     and app copies agree.)

# atlantic-emr MCP — why booking/cancel misfires, and the fix

> Review of the actual MCP source (patch series `emr-mcp-main-1de8d3a2e`,
> 4 commits, 2026-07-01 backup) against three real session captures
> (`websrv01.physiciantogo.net_4/5/6.har`). Each defect below is keyed to the
> exact function and stage name in the code, so it can be merged into the
> current tree even where it has drifted since 07-01.
>
> Companion files: `SVIGG_SCHEDULING_CONTRACT.md` (wire ground truth) and
> `svigg_reliability_fix.py` (graft-ready corrected methods).

## Symptom being fixed

Cancels fired through the app/MCP intermittently report success while the
appointment is still on the Svigg calendar (user-observed: 1 of 3 stuck);
bookings intermittently never land. All defects below produce exactly that
signature, because the portal returns **HTTP 200 for ignored/no-op posts** —
proven in HAR _4, where six consecutive `bk_p` posts with a blank `cpt00`
bounced silently with 200 each time.

## Defects in `svigg_scraper.cancel_appointment` (post-patch-4 state)

### D1 — Stale-frame reuse: fixed sleeps + whole-frameset content scans  (root cause, high confidence)

The flow is: click → `wait_for_timeout(1800/2500/3000)` → loop over **all**
`page.frames` looking for `name="Delete"` (step 4) or the string `cancel_p` in
frame content (step 5). Two failure modes:

- A frame left over from a **previous** cancel attempt satisfies the scan. The
  code then clicks Delete/Yes inside a stale form whose `TFORMCOUNT` the portal
  silently ignores (stale counter ⇒ 200-no-op, per portal behavior). The flow
  "completes"; nothing was deleted.
- On a slow render the fresh frame isn't there yet at scan time ⇒ spurious
  `identity_guard` / `confirm_page` errors on cancels that would have worked.

Together these produce exactly "some cancels work, some silently don't."

**Fix:** never sleep-and-scan. Bind to the frame you clicked in and use
`frame.expect_navigation()` so the post-click document is, by construction,
the fresh render. Parse `TFORMCOUNT` out of that fresh document before every
step; refuse to proceed if it's absent (fail-closed).

### D2 — `text={last_name}` first-match click  (their own deferred M1/M2)

Step 3 clicks the first substring match of the last name anywhere in the grid
frame. With two same-day appointments for one patient (`mre?…r=0` vs `r=1`),
or the name appearing in a header/count row, it opens the wrong cell or a
non-link. The HAR shows the human opens a specific cell anchor
(`mre?x=<col>&y=<row>&r=<idx>`).

**Fix:** target `a[href*="mre?"]` anchors whose cell text matches, and require
exactly one candidate unless a slot time is given to disambiguate — otherwise
return `{status:"ambiguous", candidates:[…]}` instead of guessing.

### D3 — Post-cancel verification lacks the date guard  (false "verified: true")

Patch 4's H1 guard aborts if the **pre**-read grid didn't apply the date
filter — but the **post**-cancel re-read (`cal_after`) has no such check. The
date-filter fallback-to-today is a known flake. Consequences:

- Patient not on today's grid ⇒ `after_rows=0` ⇒ `verified: True` **while the
  appointment still exists on the real date.** This is the false positive that
  matches "app said cancelled, Svigg still shows it."
- Patient also on today's grid ⇒ `verified: False` on a cancel that actually
  worked (the false negative seen in live testing).

**Fix:** apply the same `date_filter_applied` guard to the re-read; if it
can't be applied, return `status:"cancel_submitted_unverified"` honestly —
never compute `verified` from a wrong-day grid.

### D4 — Only the fragile path is implemented

The grid path depends on pixel-cell anchors, text matching, and frame scans.
HAR _6 proves the office's other flow, which is strictly better for
automation — keyed on the **encounter id** (`enc`), a unique appointment
identifier, with no coordinates and no name-text clicks:

```
resched.htm?enc=<ENC> → resched_p (Delete) → cancel2_p (CancelReason + Yes)
```

`TFORMCOUNT` on `cancel2_p` is resched_p's **+1** (observed 3→4, 13→14 — note:
NOT the grid path's +2; parse it, never compute it). `enc` is harvestable from
the front-desk appointment list (each row's `appt_e.htm?…enc=<ENC>` edit link)
or the patient chart (`plist.htm?rowid&acct`).

**Fix:** add `cancel_appointment_by_enc()` (see graft file) and prefer it
whenever `enc` is known; keep the hardened grid path as fallback. Harvest and
expose `enc` per row in the schedule-day reader so callers have it.

## Defect in `book_appointment` (patch-1 state)

### D5 — Swallowed `cpt00`/`prov` select failures + unverified "submitted"  (their own deferred M3)

`cpt00` and `prov` are DOM `<select>`s. If `select_option` fails (option label
drift, wrong office context), the code continues and POSTs `bk_p` with a blank
`cpt00` — which the portal **silently bounces with HTTP 200**, re-rendering
the form (+2 `TFORMCOUNT`), proven six times in a row in HAR _4. The method
then returns `status:"submitted"` with only a "VERIFY" warning. Net effect:
"booking fired, nothing landed."

**Fix (three parts):**
1. Treat any `cpt00`/`prov` select failure as fatal for the attempt:
   `{status:"error", stage:"form_fill", field:…}` — never POST with them blank.
2. After the POST, detect the bounce: if the response re-rendered the same
   booking form (fresh `bk_p` form present, `TFORMCOUNT` advanced by 2),
   return `{status:"validation_bounced", rendered_form_state:…}`.
   Wire hint: a bounce is HTTP 200; the one observed success was HTTP 302.
3. Retire trust in `submitted`/`submitted_overbooked`: terminal success is
   only `submitted_verified` after a date-guarded grid re-read finds the
   patient on the target date (the app's scraper already adopted this on
   07-05; the MCP copy must match).

### D6 — Name-order intolerance  (user-reproduced booking blocker, 2026-07-06)

Booking "test patient" fails while "patient test" succeeds: a bare typed name
is interpreted in one fixed word order, so half of natural staff input misses
the record ("Patients 1, Test"). Fix: `name_order_variants()` in the graft
file — comma form stays authoritative, a bare two-token name searches BOTH
orders, hits are deduped by acct across variants, and two *distinct* matching
patients returns `ambiguous_name` with candidates instead of guessing.
Acct-first resolution is untouched and still wins. Wire the same helper into
all three name entry points (scraper `resolve_patient` fallback, app chat
lookup, MCP `lookup_patient`) so they can't drift apart.

## Process-level (applies regardless of the code fixes)

- **P1 — Stale module cache:** the MCP server is long-running and imports the
  scraper once; after any scraper change it executes old code until restarted.
  Restart it on every deploy (or check the scraper file mtime per tool call
  and refuse with a "stale server, restart me" error).
- **P2 — Browser death is not auto-recovered:** on a dead Chromium every
  action fails until restart. Add a liveness probe (`browser.is_connected()` +
  cheap page ping) before each write action; relaunch + re-login instead of
  failing the tool call.
- **P3 — Diagnose `execute_blocked` before blaming the wire:** this MCP build
  hard-gates cancels to the test account (`CANCEL_ALLOWED_ACCT_NAMES =
  {"22041163": "patient"}`). A cancel against any real patient returns
  `execute_blocked` — check the tool's returned `status` field when triaging
  "it didn't fire" reports; some of them are the gate working as designed.

## Merge instructions (for the session running on the machine with the repo)

1. Read `svigg_reliability_fix.py` — it contains complete replacement/new
   methods in the repo's own result-dict idiom, marked with `MERGE:` notes.
2. The local tree has commits after this 07-01 backup (the 07-05/06 app-side
   cancel rewrite). Where the app's `svigg_scraper.py` already implements a
   resched/enc cancel, reconcile rather than duplicate: the MCP and the app
   must share ONE scraper implementation (P1 makes drift between them a
   standing source of "MCP fails where the app works").
3. After merging: restart the MCP server process (P1), then live-verify on
   the test account only (book → verify on grid → cancel by enc → verify
   gone → grid path fallback once), matching `verify_all_v2.py` expectations.

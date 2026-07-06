# Svigg / WEBeDoctor / Dr.Com — Booking & Cancel Wire Contract (HAR-derived)

> Derived 2026-07-06 from a real human session capture
> (`websrv01.physiciantogo.net_4.har`, 542 entries, 83 scheduling-related).
> This is the ground-truth request sequence the portal actually accepts.
> No PHI and no credentials appear in this document.

## Why this document exists

Bookings and cancels fired through the app / MCP server have been unreliable
(user report: 3 cancel attempts, 1 stuck, 2 silently didn't). This HAR shows a
human performing the same operations successfully, so any automation that
deviates from the sequences below is the bug.

## Portal shape

- All scheduling traffic goes through `POST/GET /proxy.cgi/<SESSION_ID>/<page>`
  where `<SESSION_ID>` is a numeric id minted per calendar frame
  (e.g. `218898218`). It changes every time the calendar frameset reloads —
  never reuse one across a frameset reload.
- Every server-rendered form carries `TFORMCOUNT`. It increments by 2 per
  render within a session frame (1, 3, 5, 7 … observed). **A post with a stale
  TFORMCOUNT is silently ignored or re-renders without acting.** Always parse
  the current value out of the freshly rendered form; never compute it.

## Date navigation (calfilt)

```
GET  /proxy.cgi/app/enc/cal.htm            → frameset
GET  /proxy.cgi/<SID>/calfilt.htm          → filter form render
POST /proxy.cgi/<SID>/calfilt_p
     TFORMCOUNT=<current>, prov=<PROVIDER>, off=<office>,
     dt=MM/DD/YYYY, thismon.x=<n>, thismon.y=<n>
GET  /proxy.cgi/<SID>/calfilt.htm          → grid re-render on target date
```

Observed: the human's own navigation posts `dt=` (typed date) together with a
`thismon` image-button click. The re-rendered `calfilt.htm` after the POST is
the only trustworthy confirmation the grid is on the requested date — verify
the rendered date header before staging anything.

## Booking (bk_p)

Click the target grid cell (stages the slot server-side), then:

```
POST /proxy.cgi/<SID>/bk_p
     TFORMCOUNT=<current>, Incident=0,
     cpt00=<CPT code — REQUIRED>, Duration00=<mins>, note00=<note>,
     FromDate=<leave at rendered default>, ToDate=<rendered value>,
     Sunday=on … Saturday=on, off=<office>,
     Submit=Submit            ← normal booking
     Submit=Overbook          ← slot-full path (Duration00 must be filled)
```

Hard evidence from the capture:

1. **`cpt00` empty ⇒ silent validation bounce.** Six consecutive `bk_p` posts
   (TFORMCOUNT 6→16) went nowhere while `cpt00` was blank; the form just
   re-rendered (+2 each time). The booking only landed on the post where
   `cpt00=NP` was set. An automation that doesn't fill CPT will loop forever
   "submitting" with nothing booked.
2. **Overbook is the same form**, re-posted with `Submit=Overbook` and a
   filled `Duration00` after a normal `Submit` bounces on a full slot
   (observed: Submit → bounce → Duration00=15 + Submit=Overbook → landed).
3. `FromDate` stays at the rendered default (capture day), even when booking a
   future date — the staged grid cell binds the real slot. Do not overwrite it.
4. Success signal: after a good post the frameset reloads
   (`apptFrameset.htm`); confirm by re-reading the grid for the patient on the
   target date. Never trust the POST response alone.
5. Wire-level hint (HAR _5): a successful `bk_p` returned **HTTP 302**, while
   validation bounces returned **200** with a re-rendered form. Treat 200 as
   "probably bounced — inspect the re-rendered form for what's missing";
   treat 302 as "probably accepted — still verify by grid re-read".

## Cancel — TWO proven paths

The captures show two distinct, both-working cancel flows. Path B is the more
reliable automation target: it is keyed on the encounter id (`enc`) — a unique
appointment identifier — and involves no pixel coordinates and no text-matching.

### Path B (encounter-keyed): resched → resched_p → cancel2_p  (HAR _6, proven twice)

```
(enc source: the front-desk appointment list — each row's edit link is
 appt_e.htm?date=…&time=…&enc=<ENC>&prov=… — or the patient chart:
 plist.htm?rowid=<ROWID>&acct=<ACCT> → encounter list)

1. GET resched.htm?enc=<ENC>          → renders the appointment edit form
2. GET resched_p.htm?TFORMCOUNT=<N>&Delete=Delete
      → renders a confirm page. NOTHING DELETED YET.
3. GET cancel2_p?TFORMCOUNT=<N+1>&CancelReason=<or|pr>&Yes=Yes
      → THIS deletes the appointment.
```

Hard evidence (two complete cancels in HAR _6): TFORMCOUNT on `cancel2_p` is
resched_p's value **+1** (observed 3→4 and 13→14) — different arithmetic from
the grid path's +2. Do not compute the counter either way: **parse it fresh
from each rendered form** before posting.

Button semantics on `resched_p`, all observed live: `Delete=Delete` (proceed to
confirm), `Cancel=Cancel` (exit, no-op), `Submit=Submit` (save edits, no-op for
deletion). Only the `Delete → cancel2_p Yes` pair removes the appointment.

### Path A (grid-cell): mre → mre_p → cancel_p — the three-step contract

This capture shows the full working cancel, including two aborted attempts —
which is almost certainly the "cancels don't stick" bug:

```
1. GET /proxy.cgi/<SID>/mre?x=<col>,y=<row>,r=0
      → opens the appointment cell's edit form
2. GET /proxy.cgi/<SID>/mre_p?TFORMCOUNT=<current>,Incident=0,
      Incident_AUTOSELECTED=TRUE,cpt1=<cpt>,Duration1=<mins>,
      Note1=<note>,Staged1=0,Delete=Delete
      → presses Delete; renders a CONFIRM dialog. NOTHING IS DELETED YET.
3. GET /proxy.cgi/<SID>/cancel_p?TFORMCOUNT=<current+2>,
      CancelReason=<code e.g. "or">,Yes=Yes
      → THIS is the only request that deletes the appointment.
```

Observed in the HAR, in order:

| Attempt | Step 2 (Delete) | Step 3 (confirm)                  | Result       |
|--------:|-----------------|-----------------------------------|--------------|
| 1       | sent            | `cancel_p?…No=No`                 | **no delete**|
| 2       | sent            | `cancel_p?…No=No`                 | **no delete**|
| 3       | sent            | `cancel_p?…CancelReason=or,Yes=Yes` | deleted ✔  |

### Failure modes that produce "the appointment is still in Svigg"

- Stopping after step 2 (`Delete=Delete` renders a dialog; it commits nothing).
- Posting the confirm with `No=No`, or without `CancelReason`, or without
  `Yes=Yes`.
- Reusing a stale `TFORMCOUNT` on step 3 (must be the value from the dialog
  render, which is step-2's + 2).
- Reusing a `<SESSION_ID>` after the frameset reloaded.

### Required post-conditions (fail-closed)

- After step 3, re-read the day grid (fresh `calfilt` on the same date) and
  assert the patient's row is gone. Report `cancelled_verified` only then;
  otherwise report honestly (`cancel_unconfirmed` + evidence), never success.

## MCP-server reliability (applies on top of the wire contract)

Two known process-level causes of "the MCP fires but nothing lands", from the
project's own docs — both must be handled or the wire fixes above won't help:

1. **Stale process.** The MCP server is long-running and imports the scraper
   once. After any scraper change it keeps executing the old code until
   restarted. Fix: restart it after every deploy, or have it check the scraper
   file mtime/hash on each tool call and re-exec itself when stale.
2. **Browser death is not auto-recovered.** If the shared headless Chromium
   dies, every subsequent action fails ("Target page … has been closed") until
   restart. Fix: a liveness probe before each action (`browser.is_connected()`
   / a cheap page ping) that relaunches the browser and re-logs-in instead of
   failing the tool call.

## What is needed to patch the actual code

The source (`mcp/atlantic_emr_server.py`,
`python/integrations/svigg_scraper.py`, `emr_session_manager.py`, tests) is
not in this repository. Push the code — **code only, never `.env`, tokens,
`service_account.json`, or any `app/data/` contents** — and the fixes above
can be applied and unit-tested directly against this contract.

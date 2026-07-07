# Local EMR Bridge

The bridge is the **only** component in the Back Office platform that talks to a
real EMR. It runs **on the office machine**, bound to **localhost**, inside the
office network. It is **never exposed publicly**.

It fronts the vendored, deterministic Playwright EMR clients
(`bridge/integrations/`) behind the Action Registry contracts
(`packages/action-registry/`) and a strict two-phase, verified, audited flow.

## What it does

- Exposes POST endpoints per EMR system (`/sis/*`, `/svigg/*`) for each action.
- Validates every request with Pydantic v2.
- For writes (risk_level >= 2) requires an `approval_token` before dispatch.
- Dispatches to an adapter layer that wraps the vendored clients **where a real
  implementation exists**.
- Where no real implementation exists, or required EMR credentials are absent at
  runtime, returns a structured **blocked** response — never a fake success:

  ```json
  {"status": "blocked",
   "reason": "Missing real SIS/Svigg selectors or credentials",
   "needed_from_user": ["env:SIS_USERNAME", "env:SIS_PASSWORD"]}
  ```

- Captures a **post-action verification screenshot** (and trace) on every write,
  saved under `bridge/evidence/` (gitignored).

## Layout

```
bridge/
  main.py                 FastAPI app (localhost bind, /healthz, /registry)
  models.py               BridgeResponse + request models (Pydantic v2)
  evidence.py             screenshot + Playwright trace capture -> ids
  routers/sis.py          POST endpoints for SIS
  routers/svigg.py        POST endpoints for Svigg
  routers/_deps.py        approval-token gate for writes
  adapters/sis_adapter.py     wraps vendored SISClient (read-only)
  adapters/svigg_adapter.py   wraps vendored SviggScraper (reads + gated writes)
  adapters/_common.py         gates: implemented? credentials? patient match?
  integrations/               VENDORED clients (do not edit lightly)
  evidence/                   screenshots + traces (gitignored; PHI-bearing)
  requirements.txt
```

## Standard response

Every endpoint returns `bridge.models.BridgeResponse`:

```
status:  success | failed | blocked | needs_human_review
verified: bool                 # True only after a confirmed post-action re-read
system, action
patient_match: {match_level: strong|weak|none, matched_by: []}
data, source, as_of
screenshot_id, trace_id
requires_human_review, warnings: [], failure_reason
```

## Patient verification (non-negotiable)

- **Strong** (writes allowed): `emr_id`, or `dob + exact name`, or `dob + phone`.
- **Weak** (name/phone/email only): the write **halts** and returns
  `needs_human_review`. Reads still run but report `match_level: weak`.

## Implemented vs blocked (verified against the vendored code)

| Action | SIS | Svigg |
|---|---|---|
| find_patient | implemented | implemented |
| get_patient_demographics | implemented | implemented |
| get_upcoming_appointments | implemented | implemented |
| retrieve_notes | implemented | blocked (no notes in Svigg) |
| get_referral_status | blocked | blocked |
| book_appointment | blocked (SIS read-only) | implemented (gated, verify) |
| cancel_appointment | blocked | implemented (gated, verify) |
| create_new_patient | blocked | implemented (dry-run; commit gated + unverified) |
| update_unsigned_note | blocked | blocked |
| append_signed_note_addendum | blocked | blocked |

Blocked actions return the structured block above with the missing real input.

## Running (office machine only)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# credentials live ONLY in .env on the office machine (never in git/logs):
#   SIS_USERNAME, SIS_PASSWORD            (SIS Complete)
#   WEBEDOCTOR_USER, WEBEDOCTOR_PASS      (Svigg / WEBeDoctor)
# write kill-switches (fail-closed; leave unset until trusted):
#   SVIGG_BOOKING_EXECUTE, SVIGG_CREATE_EXECUTE
#   SVIGG_BOOKING_ALLOWLIST, SVIGG_CANCEL_ALLOWLIST   (test acct only)

# localhost-only bind:
python -m bridge.main
# or: uvicorn bridge.main:app --host 127.0.0.1 --port 8901
```

## Security posture

- **Localhost bind only** (`127.0.0.1`). Never `0.0.0.0`; never port-forwarded.
  Office network only.
- **No PHI in git or logs.** `evidence/` (screenshots/traces) is gitignored and
  stays on the office machine.
- **Credentials only in `.env`** on the office machine — never in code, never in
  Postgres (the DB stores only credential *reference* env-var names).
- **No mock data, ever.** Missing selectors/credentials -> `blocked`. Weak match
  -> `needs_human_review`. Unverified write -> honest non-success + evidence.
- **Writes are two-phase:** the plan is approved on screen; the bridge checks the
  approval token, executes deterministically, re-reads to verify, and captures a
  screenshot before reporting success.

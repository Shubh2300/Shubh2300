# Original spec: "Verified EMR Action Bridge"

This is the owner's original build-environment prompt, preserved verbatim
(lightly reformatted for readability only — no content changed, added, or
removed). It's the founding spec for the whole platform. **See `DECISIONS.md`
for where the actual build diverged from this** (e.g. Python bridge instead
of TypeScript, Temporal from day one instead of "structure for later,"
single-machine deployment) — this file is the original ask, not the current
plan.

---

## Build Environment

We are building this app using Cursor and Claude Code. This is a real
production-style surgical center workflow automation system. This is not a
demo app. This is not a fake SaaS dashboard. This is not a chatbot. This is
not a Lovable/Base44-style prototype. The goal is to build a real medical
office / surgical center operations platform that can retrieve information
from SIS Complete and Svigg/Doctor.com, then eventually take approved
actions inside those systems.

We are building this using:
- Cursor as the main development environment
- Claude Code as the coding agent
- GitHub as the source of truth
- Next.js for the web dashboard
- Backend API using NestJS or FastAPI
- Postgres for database/state/audit logs
- Temporal for reliable workflow execution
- TypeScript + Playwright for the local EMR bridge
- OpenAI mini API for cheap structured action parsing and response drafting
- Local office machine/server for running the EMR bridge

**Core architecture name: Verified EMR Action Bridge**

The system should work like this: Staff enters a task → OpenAI mini parses
it into a structured approved action → system validates it against the
Action Registry → staff reviews and approves → Temporal starts the workflow
→ Local Playwright Bridge executes exact SIS/Svigg steps → bridge verifies
the result/action inside the EMR → screenshots/traces/audit logs are saved
→ task is marked completed only if verification passes → if anything is
unclear, the workflow stops and creates human review.

## Hard rule

The AI never directly controls SIS or Svigg. The AI only proposes an action
from the approved Action Registry. The bridge executes deterministic
Playwright scripts.

No arbitrary browser control. No random clicking. No "figure it out." No
fake workflows. No mock EMR data. No simulated patients. No placeholder
patient results. No generic browser agent. No HAR-only automation. No
direct writeback without approval. No action marked complete unless the EMR
confirms it.

If real credentials, selectors, URLs, or recordings are missing, create the
code structure and mark that action as `BLOCKED_PENDING_REAL_SELECTOR_OR_CREDENTIALS`.
Do not invent selectors. Do not invent EMR screens. Do not invent fake data.
Do not pretend an integration works until real workflow recordings/selectors
are provided.

## Approved action categories

**Level 1: Read actions**
- find_patient
- get_patient_demographics
- get_upcoming_appointments
- get_referral_status
- get_documents
- retrieve_notes
- get_recent_activity

**Level 2: Scheduling actions**
- book_appointment
- cancel_appointment
- reschedule_appointment
- confirm_appointment
- mark_no_show
- add_appointment_note

**Level 3: Patient creation/update actions**
- create_new_patient
- update_patient_demographics
- add_insurance_info
- add_referring_provider
- upload_patient_document
- link_referral_to_patient

**Level 4: Clinical note actions**
- retrieve_note
- create_note_draft
- update_unsigned_note
- append_signed_note_addendum
- route_note_for_provider_review

Do not create a generic `edit_note` action. Signed notes must never be
freely edited. Signed notes can only use `append_signed_note_addendum`.
Provider approval is required before any signed-note addendum. Unsigned
notes may use `update_unsigned_note` but only after staff/provider approval.

## Every action contract must define

- action name
- target system: SIS or Svigg
- risk level
- required inputs
- preconditions
- exact execution steps
- selectors or screen anchors needed
- patient verification rules
- success condition
- failure modes
- retry behavior
- approval requirements
- post-action verification
- output schema
- audit proof required

### Example action contract: `book_appointment`

**Inputs:** patient first name, patient last name, DOB, provider, appointment
type, date, time, location, reason.

**Preconditions:** patient match is strong; appointment type is valid;
provider schedule is visible; slot is available; staff approved exact
booking details.

**Execution:** login to EMR → search patient → verify patient identity →
open scheduler → verify provider/location/date → select appointment slot →
select appointment type → confirm booking → reopen patient schedule →
verify appointment exists.

**Success:** appointment appears in patient chart and scheduler.

**Failure:** patient not found; multiple patient matches; DOB mismatch;
slot unavailable; provider unavailable; appointment type missing;
confirmation failed; post-booking verification failed.

**Output:** status, verified true/false, appointment details, source screen,
timestamp, screenshot_id, trace_id, requires_human_review, warnings.

## Patient matching rules

**Strong match:** EMR patient ID; DOB + exact name; DOB + phone.

**Weak match:** name only; phone only; email only.

Weak matches must stop and require human review. Never create a patient,
book an appointment, cancel an appointment, or edit a note based on a weak
patient match.

## Patient creation rule

Before `create_new_patient`: search by name + DOB; search by phone; search
by email if available; check for possible duplicate; show duplicate risk to
staff; require staff approval. If possible duplicate exists, stop and
require human review.

## Scheduling rule

Booking, canceling, and rescheduling must be two-phase.

**Phase 1: Plan** — find patient → verify patient → check
appointment/schedule → prepare proposed action → show exact action to
staff.

**Phase 2: Commit** — staff approves → bridge performs action → bridge
verifies inside EMR → screenshot/trace saved → audit log written.

## Clinical note rule

`retrieve_note` is allowed as read-only. `create_note_draft` is allowed.
`update_unsigned_note` requires staff/provider approval.
`append_signed_note_addendum` requires provider approval.

For every note modification, store: original note snapshot, proposed
change, reason for change, approver, timestamp, final note result,
screenshot_id, trace_id.

## The EMR bridge must run locally on an office machine/server

Build a local service: `emr-bridge/`. Use Node.js, TypeScript, Playwright,
Fastify or Express, Playwright traces, screenshots, structured JSON
responses. The bridge should expose only deterministic endpoints.

### Initial endpoints

**SIS:** `POST /sis/find_patient`, `/sis/get_patient_demographics`,
`/sis/get_referral_status`, `/sis/get_upcoming_appointments`,
`/sis/retrieve_notes`, `/sis/book_appointment`, `/sis/cancel_appointment`,
`/sis/create_new_patient`, `/sis/update_unsigned_note`,
`/sis/append_signed_note_addendum`.

**Svigg:** same list of endpoints under `/svigg/*`.

### Each endpoint must

1. Validate input.
2. Check approval status.
3. Start or continue a Temporal workflow.
4. Execute deterministic Playwright steps.
5. Verify correct screen.
6. Verify correct patient.
7. Perform the read/write action.
8. Verify the final result inside the EMR.
9. Save screenshot.
10. Save Playwright trace.
11. Return structured JSON.
12. Write audit log.
13. Fail safely if anything is ambiguous.

### Required output format

```json
{
  "status": "success | failed | blocked | needs_human_review",
  "verified": true,
  "system": "SIS | Svigg",
  "action": "action_name",
  "patient_match": {
    "match_level": "strong | weak | none",
    "matched_by": ["dob", "exact_name"]
  },
  "data": {},
  "source": "system > screen > section",
  "as_of": "timestamp",
  "screenshot_id": "string",
  "trace_id": "string",
  "requires_human_review": false,
  "warnings": [],
  "failure_reason": null
}
```

If failed:

```json
{
  "status": "failed",
  "verified": false,
  "requires_human_review": true,
  "failure_reason": "clear reason",
  "screenshot_id": "string",
  "trace_id": "string"
}
```

## Backend app responsibilities

Receive staff prompts; send prompt to OpenAI mini structured parser;
convert prompt into ActionIntent JSON; validate ActionIntent against Action
Registry; display approval screen; require staff approval; create
ActionRun; start Temporal workflow; call Local EMR Bridge; receive
verified result; update task status; write audit log; show result to
staff.

## OpenAI mini responsibilities only

Parse staff prompt; select approved action; identify missing fields; assign
risk level; explain why action was selected; draft response only after
verified data is returned.

**OpenAI mini must not:** control the browser; click inside EMR; invent
actions; invent patient data; claim a task is done; bypass approval; write
clinical advice; edit notes directly; send anything patient-facing without
approval.

Use Structured Outputs / strict JSON schema for OpenAI mini.

### ActionIntent schema

```json
{
  "action_name": "string",
  "target_system": "sis | svigg | both | unknown",
  "risk_level": 1,
  "requires_approval": true,
  "patient_identifiers": {
    "first_name": "string",
    "last_name": "string",
    "dob": "date",
    "phone": "string | null",
    "email": "string | null",
    "emr_id": "string | null"
  },
  "action_inputs": {},
  "missing_fields": [],
  "reason": "why this action was selected"
}
```

## Database tables needed

organizations, users, roles, patients, patient_external_ids, tasks,
action_registry, action_runs, action_steps, approvals, workflow_runs,
workflow_steps, audit_logs, emr_systems, emr_credentials_reference,
screenshots, traces, note_snapshots, appointment_actions,
patient_creation_requests, human_review_queue.

## Audit log must record

Staff prompt, parsed ActionIntent, staff approver, timestamp, target
system, action requested, patient identifiers used, patient match result,
pre-action state, post-action state, screenshot_id, trace_id, final
result, failure reason if failed.

## Security requirements

Use dedicated EMR accounts; read-only first where possible; separate
read/write permissions; no credentials in code; encrypted secrets;
role-based approvals; audit logs should not be editable; no PHI sent to
any AI provider unless proper BAA/compliance setup exists; do not store
raw credentials in database; do not expose bridge publicly without strict
access controls; use local bridge inside office environment.

## Build order

**Phase 1: Foundation** — app structure, backend API, Postgres schema,
Action Registry, approval system, audit log, OpenAI mini structured
parser, Temporal workflow skeleton, local bridge service skeleton.

**Phase 2: Read-only SIS actions** — find_patient, get_patient_demographics,
get_referral_status, get_upcoming_appointments, retrieve_notes.

**Phase 3: Read-only Svigg actions** — same five, Svigg side.

**Phase 4: Scheduling write actions** — book_appointment,
cancel_appointment, reschedule_appointment.

**Phase 5: Patient creation/update actions** — create_new_patient,
update_patient_demographics, add_insurance_info, add_referring_provider.

**Phase 6: Notes** — create_note_draft, update_unsigned_note,
append_signed_note_addendum.

## First real target

Build the foundation and the first blocked real endpoint:
`POST /sis/find_patient`. Do not fake the response. If selectors/credentials
are missing, return:

```json
{
  "status": "blocked",
  "reason": "Missing real SIS selectors/credentials",
  "needed_from_user": [
    "SIS login URL",
    "Playwright codegen recording for find_patient",
    "screenshots of patient search screen",
    "success/failure notes"
  ]
}
```

For each real EMR action, the owner provides: screen recording, Playwright
codegen output, targeted HAR if needed, screenshots, written success/failure
notes. Use those to build deterministic Playwright scripts. Do not build any
free-roaming AI browser control. Do not build mock data. Do not create fake
patients. Do not claim completion unless the EMR verifies the action.

## Final product goal

A real surgical-center workflow platform where staff can type tasks,
approve the proposed action, and the system reliably executes verified
actions inside SIS/Svigg with screenshots, traces, audit logs, and safe
failure handling.

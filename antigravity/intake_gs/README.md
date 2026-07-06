# intake_gs — Atlantic Pain & Wellness Intake Script

Drop-in upgrade for the existing `Code.js`/`Config.js` Google Apps Script intake.
Uses the **same Script Property names**; all new properties are optional.

---

## Deployment

1. Open the Apps Script project attached to your log spreadsheet.
2. **Delete** (or disable the trigger on) the old `Code.js` — only ONE intake
   script may have an active time trigger.
3. Paste `Code.gs` into a new script file in the editor (replace old code).
4. Enable **Drive API (v3)** via Services → Drive API.
5. Set the Script Properties below.
6. Run **`runUnitTests()`** — all assertions must log PASS.
7. Run **`dryRun()`** — verify parsing/classification with no side effects.
8. Re-enable the time-based trigger pointing to **`processInbox`**.

---

## Script Properties

| Property | Required | Default | Notes |
|---|---|---|---|
| `LOG_SHEET_ID` | ✅ | — | Spreadsheet ID for the Runs/OcrCache/Head Injury tabs |
| `PATIENTS_ROOT_FOLDER_ID` | ✅ | — | Drive folder where patient sub-folders are created |
| `QUARANTINE_FOLDER_ID` | ✅ | — | Drive folder for low-confidence PDF quarantine |
| `LOG_DOC_ID` | ✅ | — | Google Doc ID for the run bulletin |
| `GEMINI_API_KEY` | optional* | — | Google AI Studio API key used as fallback if OpenAI is unavailable |
| `OPENAI_API_KEY` | optional | — | OpenAI API key. When set, the script tries OpenAI first and falls back to Gemini if OpenAI is unavailable. |
| `OPENAI_MODEL` | optional | `gpt-4o-mini` | OpenAI model for classification, summaries, and contact extraction. |
| `INBOX_QUERY` | ✅ | `in:inbox` | Gmail search query (e.g. add `from:referrals@...`) |
| `PROCESSED_LABEL` | optional | `patient-pdfs-processed` | Applied on success |
| `REVIEW_LABEL` | optional | `patient-pdfs-needs-review` | Applied when manual review is needed |
| `FEE_SLIP_TEMPLATE_ID` | optional | `''` | Google Doc template ID; skip if blank |
| `WC_FORM_TEMPLATE_ID` | optional | `''` | Google Doc template ID; skip if blank |
| `INTAKE_FORM_TEMPLATE_ID` | optional | `''` | Google Doc template ID; skip if blank |
| `IGNORED_LABEL` | optional | `patient-pdfs-ignored` | Applied to bulk/vendor/non-referral emails |
| `RECORDS_LABEL` | optional | `patient-records-billing` | Applied to non-referral emails that are still patient records/billing/insurance requests |
| `SKIP_VENDOR_INVOICES` | optional | `true` | Set `false` to force every email through Gemini |
| `DRY_RUN` | optional | `false` | Legacy property; use `dryRun()` function instead |
| `STAFF_NAME_BLOCKLIST` | optional | `''` | Comma-separated full names of clinic staff; matched names are routed to review rather than filed as patients |
| `FOLDER_NAME_STYLE` | optional | `paren-dob` | `paren-dob` → `First Last (DOB MM-DD-YYYY)` · `underscore` → `First_Last_DOB_YYYY-MM-DD` |
| `GEMINI_MODEL` | optional | `gemini-2.5-flash-lite` | Gemini fallback model |
| `MAX_THREADS` | optional | `15` | Threads fetched per run |
| `TIME_BUDGET_MS` | optional | `300000` | Stop loop after this many ms; untouched threads run next trigger |
| `NOTIFY_ON_REFERRAL` | optional | `1` (ON) | Set `0` to disable the instant new-referral/booking/follow-up alert emails |
| `NOTIFY_EMAIL` | optional | `''` | Recipient for the instant alert emails; blank sends to the effective user (self) |
| `PARTNER_BILLING_DOMAINS` | optional | `mdmanage.com,srm-inc.com` | Comma-separated sender domains (subdomains match too) routed straight to the records/billing lane, no AI classification |
| `IGNORE_SENDERS` | optional | `invoice+statements@mail.anthropic.com,failed-payments@mail.anthropic.com,noreply-apps-scripts-notifications@google.com,alerts@tdbank.com,noreply@messaging.squareup.com` | Comma-separated sender strings; any match on the From header is ignored deterministically (Gate 1, before AI) |
| `BOOKING_LABEL` | optional | `Intake/Booking Requests` | Applied to scheduling/rescheduling requests (patient or representative asking to book an appointment) |
| `CLINIC_ADDRESSES` | optional | `mainlinesurgery@gmail.com,mainlinepain@gmail.com,mainsurgical@gmail.com` | Comma-separated list of the clinic's own sending addresses; used to tell staff's own outbound replies on a handled thread apart from a genuine inbound follow-up |

* At least one AI key is required: `OPENAI_API_KEY` or `GEMINI_API_KEY`.

---

## Merge tags in Doc templates

Supported in any of the three form templates:

| Tag | Replaced with |
|---|---|
| `{{patient_first_name}}` | Capitalised first name |
| `{{patient_last_name}}` | Capitalised last name |
| `{{patient_full_name}}` | `First Last` |
| `{{dob}}` | `MM-DD-YYYY` |
| `{{date_today}}` | Today MM-DD-YYYY |
| `{{practice_name}}` | Atlantic Pain & Wellness Institute |
| `{{practice_phone}}` | (blank; set in source template) |

---

## Resilience design

- **Claim-first ledger**: a `claimed` row is written to the `Runs` sheet *before*
  any Drive/Gemini work. If the script dies mid-run the row stays `claimed`; on the
  next run it is detected, moved to `stale-claim`, and the thread gets `REVIEW_LABEL`
  for human triage instead of silent re-processing.
- **Terminal-status dedup**: every ledger status EXCEPT `claimed` (in-flight) and
  `retry` (deliberately re-opened, see `retryReviewQueue()` below) is treated as
  terminal — `done`, `ignored`, `records`, `report-filed`, `review`, `error`,
  `stale-claim` all mean "fully handled, do not re-process". `processThread_` checks
  this via `isTerminalLedgerStatus_()` before doing any work, so a records request,
  filed report, or reviewed/errored item is never silently re-queued or re-drafted
  on a later run.
- **Time-budget guard**: the loop checks elapsed ms before each thread; threads beyond
  the budget are left untouched (Gmail query re-matches them next trigger).
- **Fail-safe AI path**: a Gemini API failure always routes to review — never silently
  ignores or silently processes.
- **Low-confidence quarantine**: name with no labeled DOB → PDFs quarantined,
  no patient folder, no forms, REVIEW label. A name alone never creates records.

### Instant alerts ("call to book" / "call to schedule" / follow-up)

In addition to the daily digest (`sendQuarantineDigest`), three lanes send an
immediate internal email to `NOTIFY_EMAIL` (never to the patient/attorney) so
staff act right away instead of waiting for the digest:
- A successfully processed referral (`fullProcess_` reaching `done`) — "call
  the patient now to schedule their first appointment." Body: patient name,
  DOB, case type (plus a HEAD INJURY flag when detected), phone, referring
  source, Drive folder link.
- A booking/scheduling request (`routeBookingRequest_`) — "call to schedule
  this appointment now." Body: patient, requested by, phone, folder link.
- A follow-up on an already-labeled thread (`routeThreadFollowup_`) — "open
  the thread and respond." Body: prior lane, subject, sender.

Every alert subject always starts with `[Intake Alert] `. Any missing field
renders `—`, never a guess. Controlled by `NOTIFY_ON_REFERRAL` (default ON)
and `NOTIFY_EMAIL` (default: effective user / self). A failure to send any
alert is logged and added to the bulletin but never fails the run or the
filing that already succeeded. `isIntakeAlertSubject_()` is a loop guard so
the alerts themselves — if they land in the scanned inbox — are ignored
rather than treated as a new referral or a follow-up.

### Manual retry: `retryReviewQueue()`

After fixing an AI-quota problem (e.g. adding `OPENAI_API_KEY`), run
**`retryReviewQueue()`** once from the Apps Script editor. It flips every ledger row
with status `review`, `error`, or `stale-claim` to `retry` and logs how many rows
were re-opened. It does not call any AI or Gmail API itself — the next
`processInbox`/`sweepBacklog` run re-attempts those messages normally (the claim-first
ledger reuses the existing row rather than appending a duplicate). Retried items may
produce duplicate Gmail drafts if they had partially processed before being parked;
that is an accepted tradeoff for a deliberate manual retry.

### Routing lanes

Every thread takes exactly one of these paths before returning:

| Condition | Action | Ledger status |
|---|---|---|
| Message arrived on an already-labeled thread (records/booking/referral/report), sender is NOT the clinic itself | Logged to **Follow-Ups** tab; instant alert; attachments filed to existing folder when findable | `followup` |
| Message arrived on an already-labeled thread, sender IS the clinic itself (own reply) | No label changes; silently noted | `ignored` |
| Sender matches `IGNORE_SENDERS` | `IGNORED_LABEL`, no Drive write | `ignored` |
| RingCentral fax/voicemail notification (`notify@ringcentral.com`) | `REVIEW_LABEL`; logged to **Fax Queue** tab, no Drive write (see below) | `fax-queued` |
| Bulk/marketing headers | `IGNORED_LABEL`, no Drive write | `ignored` |
| Vendor invoice (heuristic) | `IGNORED_LABEL`, no Drive write | `ignored` |
| Staff blocklist name match | `REVIEW_LABEL`, no Drive write | `review` |
| Sender domain matches `PARTNER_BILLING_DOMAINS` | Routed to records/billing lane (same as records/billing request below) | `records` |
| Deterministic booking-request text (`isBookingRequest_`), patient identifiable | `BOOKING_LABEL`; logged to **Booking Requests** tab; instant "call to schedule" alert | `booking` |
| Deterministic records/billing request text, patient identifiable | `RECORDS_LABEL`; PDFs filed to existing folder (if any); logged to **Records & Billing Requests** tab (Status `NEW`); surfaced in daily digest | `records` |
| Deterministic inbound-report text, no patient identity parsed | PDFs quarantined, `REVIEW_LABEL` | `review` |
| Deterministic inbound-report text, patient identifiable | Filed to existing folder, or quarantined + review if no folder found | `report-filed` / `review` |
| AI category classification fails (both providers unavailable/unparseable) | `REVIEW_LABEL`, no Drive write | `review` |
| AI category `BOOKING_REQUEST` | Same as deterministic booking lane above | `booking` |
| AI category `RECORDS_REQUEST` | Same as deterministic records/billing lane above | `records` |
| AI category `INBOUND_REPORT` | Same as deterministic inbound-report lane above | `report-filed` / `review` |
| AI category `PATIENT_OTHER` (mentions a patient, fits no other bucket) | `REVIEW_LABEL`, no Drive write | `review` |
| AI category `NOT_PATIENT` | `IGNORED_LABEL`, no Drive write | `ignored` |
| AI category `NEW_REFERRAL`, no parseable name | PDFs quarantined, `REVIEW_LABEL` | `review` |
| AI category `NEW_REFERRAL`, name only (no labeled DOB) | PDFs quarantined, `REVIEW_LABEL`, no folder | `review` |
| AI category `NEW_REFERRAL`, high confidence (name + DOB) | Full process: folder created, PDFs filed, intake forms generated | `done` |
| Inbound patient report labels 2+ distinct patient DOBs (multi-patient document) | Not filed; `REVIEW_LABEL` | `review` |

Every patient-related thread now ends with a visible outcome — a Gmail label,
an action-queue tab row, and an instant alert where relevant — closing the gap
where a thread could receive multiple inbound nudges and end with no label and
no outcome (the "Damon Holden" case that drove this change).

### Fax Queue

RingCentral fax notification emails (`notify@ringcentral.com`, subject `New Fax
Message from <number> on <date>`) never carry the actual fax document as an
attachment — the PDF lives in the RingCentral portal. Rather than silently
ignoring these (the original 2-week-audit gap) or misreading them as a patient
referral, Gate 1 detects them deterministically (`parseFaxNotification_`),
appends a row to the **Fax Queue** tab (`Date`, `From Number`, `Pages`,
`Subject`, `MessageId`, `Status: NEW`) in `LOG_SHEET_ID`, and applies
`REVIEW_LABEL` so a human retrieves the document from the RingCentral portal
and files it manually. RingCentral voicemail notifications (`New Voice Message
from <number> on <date>`) are queued to the same tab with `Pages: 0`. Both use
ledger status `fax-queued`, which `isTerminalLedgerStatus_` treats as terminal
(no auto-retry).

**Records/billing lane notes:**
- `isRecordsOrBillingRequest_` is a pure heuristic function; it triggers on keywords such as: *records request, medical records, LOP, lien, subpoena, billing, itemized statement, balance due, insurance, claim number, EOB, pre-authorization, verification of benefits*.
- A records request **never creates a new patient folder** — `findExistingPatientFolder_` only searches; it returns null rather than creating.
- When no existing folder is found the email stays in Gmail under `RECORDS_LABEL` for staff triage; nothing is written to Drive.
- Records/billing items appear in the daily `sendQuarantineDigest` email alongside review/error items.

### Booking Requests lane

Closes the gap where a scheduling request (e.g. `Harun Omar Sahin - REQ FOR
APPOINTMENT`) had no dedicated lane and landed wherever the classifier guessed
— sometimes Records & Billing. `isBookingRequest_` is a pure heuristic that
matches appointment/schedule/reschedule/"REQ FOR APPOINTMENT" language, but
**always defers to the records/billing lane** when the same text also matches
`isRecordsOrBillingRequest_`'s keyword set (records/billing keeps priority).
`routeBookingRequest_`:
- Finds the patient's existing folder (`findExistingPatientFolder_` /
  `findPatientFolderByLastName_`) and files any OCRable attachments into it —
  never creates a new folder for a booking request.
- Extracts a phone number from the body with a simple digit-pattern match
  (`''` if none found).
- Appends a row to the **Booking Requests** tab (`Date`, `Patient`, `DOB`,
  `Requester`, `Phone`, `Subject`, `Folder`, `Status: NEW`, `MessageId`).
- Applies `BOOKING_LABEL` (default `Intake/Booking Requests`) and removes
  `REVIEW_LABEL` if present.
- Sends the instant "call to schedule this appointment now" alert to
  `NOTIFY_EMAIL` (same guarded, never-throws pattern as the referral alert) —
  gated by `NOTIFY_ON_REFERRAL`.
- Ledger status `booking`.

Booking requests are detected both deterministically (Gate 1, before AI) and
via the AI category router's `BOOKING_REQUEST` category, so a rate-limited AI
provider never strands an obvious scheduling request without a lane.

### Follow-Ups tab (thread follow-up awareness)

The Damon Holden fix: a reply landing in an **already-labeled** patient thread
(records, booking, referral, or report lane) is never re-classified in
isolation and never silently dropped. Immediately after the ledger dedup
check and the Sway gate, `processThread_` reads the thread's current Gmail
labels and checks them against `RECORDS_LABEL` / `BOOKING_LABEL` /
`PROCESSED_LABEL` / `REPORTS_LABEL`:
- If the sender is **not** one of `CLINIC_ADDRESSES` (a genuine inbound
  follow-up) and the subject is not the system's own alert
  (`isIntakeAlertSubject_`), the message is routed to `routeThreadFollowup_`:
  it appends a row to the **Follow-Ups** tab (`Date`, `Subject`, `From`,
  `Prior Lane`, `Status: NEW`, `MessageId`), files OCRable attachments to the
  patient's existing folder when findable by parsed name (skipped silently
  otherwise — never creates a folder), sets ledger status `followup`, and
  sends an instant alert (`[Intake Alert] Follow-up (<lane>): <subject>`)
  telling staff to open the thread and respond.
- If the sender **is** one of `CLINIC_ADDRESSES` (staff's own reply on a
  thread they already handled), the ledger row is claimed and marked
  `ignored` with detail `own reply on handled thread` — no label changes, no
  alert; this is expected traffic, not a gap.

This check runs before every other Gate 1/2/3 filter, so a follow-up on a
handled thread can never be mis-routed to Ignored or silently skipped by the
bulk/sales gates.

### AI category router (`classifyEmailCategory_`)

Replaces the old binary "is this a referral?" classification at the final
Gate-3 branch of `processThread_`. Uses OpenAI (`OPENAI_API_KEY`) as the
primary provider and falls back to Gemini using the same request/retry/
backoff structure as `classifyReferralWithGemini_` (which remains in the file
for `dryRun()` and any code that still references it directly). The model
returns one of six categories — `NEW_REFERRAL`, `BOOKING_REQUEST`,
`RECORDS_REQUEST`, `INBOUND_REPORT`, `PATIENT_OTHER`, `NOT_PATIENT` — plus an
optional best-guess `patientFirst`/`patientLast` and a one-line `reason`.
Parsing is defensive: code fences are stripped, `JSON.parse` failures are
caught, and any failure (both providers down, unparseable response, no AI key
configured) returns `null`, which fails safe to the same `REVIEW_LABEL` path
the old Gemini-failure branch used — never silently ignored, never guessed.
`PATIENT_OTHER` (mentions a specific patient but fits none of the other
buckets) always goes to `REVIEW_LABEL` rather than `IGNORED_LABEL`, so a human
sees it.

---

## Migration from Code.js

- Old deployments used `Referral/New`, `Referral/Processed`, `Referral/Error`,
  `Referral/Ignored` Gmail labels. This script uses `patient-pdfs-processed`,
  `patient-pdfs-needs-review`, `patient-pdfs-ignored` by default.  
  Either update `PROCESSED_LABEL` / `REVIEW_LABEL` / `IGNORED_LABEL` to match your
  existing labels, or rename the Gmail labels.
- The second intake script in the repo (`Code.js`) must have its trigger disabled —
  two active triggers on overlapping queries cause double-processing.

---

## Scheduled functions

| Function | Trigger type | Purpose |
|---|---|---|
| `processInbox` | Time-based (every 30 min recommended) | Main intake loop |
| `sweepBacklog` | Time-based (daily) | Wider-window catch-up scan; idempotent via the claim-first ledger |
| `sendQuarantineDigest` | Time-based (daily) | Email owner a count of review/error items |
| `retryReviewQueue` | Manual only | Re-open `review`/`error`/`stale-claim` ledger rows (status → `retry`) for re-attempt on the next run |
| `dryRun` | Manual only | Test parsing without side effects |
| `runUnitTests` | Manual only | Verify pure functions after edits |

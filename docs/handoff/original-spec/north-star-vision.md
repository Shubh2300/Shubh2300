# Original spec: "North Star Vision"

The owner's second founding prompt, preserved verbatim (lightly reformatted
for readability only). Written after `verified-emr-action-bridge.md`, this
one frames the product vision, the module breakdown, and the MVP scope.
**See `DECISIONS.md` and `docs/handoff/roadmap.md` for where the actual
build stands relative to this** — this file is the original ask.

---

## Roles

Cursor is the main development environment where the app will be built,
edited, reviewed, and organized. Claude Code is the AI coding agent that
will help inspect the codebase, plan the architecture, create files, write
code, refactor modules, run tests, and turn the vision into working
software. GitHub should be treated as the source of truth for version
control.

The goal is to use Cursor + Claude Code to build a real production-style
medical office workflow automation platform that feels as functional,
coordinated, and operationally serious as TriFetch.

This should not feel like a Lovable/Base44 demo or a basic chatbot app. It
should feel like a real clinic operations command center with workflow
state, human approvals, audit logs, AI-drafted responses, referral triage,
and future EMR/browser/desktop overlay integration.

The goal is not to build a simple chatbot, dashboard, or fake SaaS demo.
The goal is to build a real, modular healthcare workflow operating system
that can eventually function inside a doctor's office.

The app must be broken into tiny working pieces, like puzzle pieces. Each
piece should work independently, be testable, and later connect into a
larger system.

## North Star Vision

Build an AI-powered medical office workflow system that can intake
messages, emails, referrals, documents, and patient requests; classify
them; understand what action is needed; verify the real workflow state;
draft or complete the correct next step; route risky items to staff; and
maintain a full audit trail.

The system should eventually coordinate across: patient inbox/messages,
email inbox, referral intake, fax/document intake, EMR/EHR records,
scheduling, prior authorizations, insurance/eligibility checks, staff task
routing, AI-drafted patient responses, human approval workflows, audit
logs, future browser/EMR automation, and a future desktop "glass overlay"
style assistant.

## Core Product Principle

**The AI is not allowed to make promises unless the backend has verified
the action or status.**

Example: if a patient asks, "Did my referral come in?" — the AI must not
simply reply yes. The system must first check the referral queue, document
store, patient record, or connected source. Only after verification can it
draft or send a response. The AI should behave like an operations
coordinator, not a generic assistant.

## Build Philosophy

1. Break everything into small modules.
2. Build one working module at a time.
3. Don't fake/demo data ever, always use real provable data.
4. No real PHI should be used in development.
5. Every workflow must have state tracking.
6. Every AI action must be logged.
7. Every risky action must require human approval.
8. Every module should have clear inputs, outputs, and failure states.
9. Avoid building a giant agent. Build many controlled agents/functions.
10. Prioritize reliability over visual polish.

## Initial App Modules

### 1. Patient Inbox Module

**Purpose:** Receive patient messages and classify what they need.

**Functions:** import/create mock patient messages; classify intent;
detect urgency; detect missing information; summarize message; suggest
next action; draft staff-facing note; draft patient-facing response; mark
as needs approval, safe draft, urgent escalation, or incomplete.

**Intent categories:** scheduling request; reschedule/cancel; referral
status; prior authorization status; insurance question; prescription/refill
question; medical/clinical symptom; billing question; document request;
general admin question; complaint/escalation; unknown.

### 2. Referral Intake Module

**Purpose:** Process inbound referral documents or referral messages.

**Functions:** upload/mock referral document; extract patient name, DOB,
phone, referring provider, diagnosis/reason, insurance, urgency; detect
missing fields; classify referral type; assign referral status; create a
task for staff; draft fax/email response to referring office if information
is missing; link referral to patient record.

**Referral statuses:** new; needs review; missing information; eligibility
needed; prior auth needed; ready to schedule; scheduled; rejected;
completed.

### 3. AI Draft Response Module

**Purpose:** Generate responses that are tied to verified workflow state.

**Functions:** accept message + patient context + workflow status; generate
response draft; include internal reasoning summary for staff; include
confidence score; include action checklist; require approval before
sending; never claim an action happened unless verified in system state.

**Response modes:** staff-only summary; patient-facing draft;
referring-provider draft; internal task note.

### 4. Human Approval Module

**Purpose:** Prevent unsafe automation.

**Functions:** queue AI drafts for review; allow approve/edit/reject;
capture who approved; capture timestamp; capture original AI draft and
final version; log approval decision; prevent auto-send for clinical,
urgent, ambiguous, or high-risk messages.

**Risk levels:**
- Level 0: summarize only
- Level 1: safe admin draft
- Level 2: send after approval
- Level 3: staff action required
- Level 4: EHR writeback requires approval
- Level 5: clinical advice blocked
- Level 6: urgent escalation

### 5. Task Routing Module

**Purpose:** Turn messages/referrals into actionable office tasks.

**Functions:** create task; assign department/person; set priority; set due
date; link to patient/message/referral; track status; show task board;
escalate overdue items.

**Task types:** call patient; verify insurance; check referral; request
missing documents; submit prior auth; review clinical concern; schedule
appointment; send message; update chart; staff follow-up.

### 6. Audit Log Module

**Purpose:** Create a full legal/operational record of every action.

**Functions:** log every AI classification; log every draft; log every
approval; log every status change; log every task creation; log every
external action; store actor (AI, staff user, system); store before/after
values; store timestamp.

### 7. Workflow State Engine

**Purpose:** Track where every item is in the process.

**Core idea:** each workflow should have a current state, allowed next
states, required checks, and final outcome.

**Example referral workflow:** new referral → data extracted → missing
info check → eligibility check required → prior auth check required →
ready to schedule → patient contacted → appointment scheduled → completed.

Every workflow state should be explicit.

### 8. Rules Engine

**Purpose:** Let the office define how work should be handled.

**Initial rules:** which message types require approval; which message
types are urgent; which referral types need staff review; which
appointment types need insurance verification; which phrases trigger
escalation; which messages can be drafted but not sent; which staff role
owns each task type.

### 9. Patient Record Module

**Purpose:** Maintain a lightweight internal patient profile for workflow
tracking.

**Fields:** patient name, DOB, phone, email, insurance, assigned provider,
referral status, appointment status, prior auth status, open tasks,
message history, documents, notes.

This is not meant to replace the EMR. It is a workflow coordination layer.
(Later refined into `.claude/skills/patient-card-completeness-engine/`.)

### 10. Integration Layer

**Purpose:** Prepare the app for real office integrations ASAP.

**Systems:** Gmail; RingCentral; fax inbox (inside RingCentral); Google
Drive; EMRs (SIS & Svigg); payer portals (SIS and Svigg); scheduling
calendar (Svigg); browser automation; a desktop overlay that doesn't look
like AI made it.

For now, create clean interfaces/adapters so we can plug these in later.

## Architecture Requirement

Design the system so each external integration is an adapter. Example:
`EmailAdapter`, `FaxAdapter`, `EHRAdapter`, `SMSAdapter`, `VoiceAdapter`,
`DocumentStorageAdapter`, `EligibilityAdapter`, `PriorAuthAdapter`. The app
should work with real adapters.

## Technical Direction

Use a serious production-style architecture.

**Preferred stack:** Frontend Next.js; Backend FastAPI or NestJS; Database
Postgres; ORM Prisma or SQLAlchemy; Workflow engine — start simple, but
structure for Temporal/Inngest later; AI provider abstracted behind an AI
service interface; Auth role-based users; Logging structured audit logs;
Testing unit tests for workflow logic.

Do not hardcode everything into frontend components. Do not build one giant
function. Do not hide business logic in UI. Create reusable services.

**Suggested database tables:** organizations, users, roles, patients,
messages, message_threads, referrals, documents, document_extractions,
tasks, workflow_runs, workflow_steps, ai_drafts, approvals, rules,
escalations, audit_logs, integrations, external_actions.

## First Build Goal

Build the smallest real version of the product:

**MVP 1: AI Inbox + Referral Triage + Human Approval + Audit Log**

MVP 1 must include: dashboard; patient inbox; referral queue; message
classification; referral status tracking; AI response draft generation;
human approval queue; task creation; audit log; mock patient data; mock
referral data; mock AI output if no API key is configured; real AI service
interface if API key is configured.

**The MVP should prove this loop:** inbound item → classify → extract
important info → determine next action → draft response → create task →
require approval → log everything → update workflow state.

## User Experience

The office staff should open the dashboard and immediately see: new
messages; new referrals; urgent items; AI draft responses waiting for
approval; tasks assigned to staff; workflow status for each
patient/referral; audit trail of what happened.

The UI should feel like an operations command center for a doctor's
office.

## Design Style

Clean, serious, medical, modern. No childish SaaS look. No gimmicks. No
fake chatbot-first interface. The AI should be embedded into workflows.
(Later expanded into `docs/handoff/research/design-principles.md`.)

## Important Safety Rules

Never auto-send clinical advice. Never auto-send urgent symptom responses.
Never say something is completed unless system state confirms it. Always
route uncertain cases to staff. Always log AI-generated content. Always
separate draft from approved final response. Always show staff what
evidence/context was used.

## Development Instructions

First, inspect the current codebase. Then create a clear implementation
plan. Then build in small commits/modules. After each module, verify it
works. Prefer functional progress over perfect design. Use reasonable
defaults. Use real patient data that we will find together and sort
together — never use fake data.

## Output wanted first

1. A concise technical plan
2. Recommended app structure
3. Database schema proposal
4. Module build order
5. First implementation step
6. Then begin coding

## The end goal

A real medical office AI operations platform that starts with
inbox/referral triage and grows into a full TriFetch-style workflow
automation system with verified actions, human approval, audit logs, and
future EMR/browser/desktop overlay integration.

---

**Note on the original attachment:** this prompt originally ended with
instructions to break up the three legacy codebases (`n8n-office`,
`ai-phone-intake`, `antigravity`) into reusable "puzzle piece" functions.
That framing was superseded on 2026-07-08 — see the amendment note at the
top of `DECISIONS.md`. The legacy code is reference-only now; nothing from
it gets copied into the 2.0 build.

# Patient Card Completeness Engine — Design Notes

Reference detail backing `SKILL.md`. This file is the "what/why" data model
reference; `SKILL.md` is the rules. Captured from the original brainstorm
so nothing gets lost — not yet implemented as of this writing.

## The idea in one paragraph

Every patient has a card. Every card has required fields. Every field has a
source, status, timestamp, and verification proof. The app constantly shows
what is complete, missing, stale, conflicting, or blocked — and turns any gap
into a task automatically.

## Full field list by section

**Identity**
- name
- DOB / DOS (Date of Service)
- phone
- email
- address
- emergency contact
- preferred language

**Insurance / eligibility**
- primary insurance
- secondary insurance
- policy ID
- group number
- eligibility verified
- payer portal checked
- clearinghouse checked
- auth required yes/no
- last verified timestamp

**Chart readiness**
- demographics complete
- insurance uploaded
- referral received
- notes received
- surgical packet complete
- consent forms complete
- labs/imaging received
- provider notes reviewed

**Scheduling**
- consult scheduled
- surgery scheduled
- pre-op scheduled
- post-op scheduled
- recall scheduled
- cancellation/no-show status

**Communication**
- SMS history
- call history
- email history
- voicemail history
- last patient contact
- last staff contact
- unresolved messages

**Pre-op / post-op / recalls**
- pre-op instructions sent
- pre-op confirmed
- surgery confirmation sent
- post-op instructions sent
- post-op call completed
- recall created
- recall completed

**Open work**
- missing info
- staff task needed
- payer task needed
- provider review needed
- patient response needed
- blocked reason

## Field status object shape

Every field-level status is an object like this, not a bare value:

```json
{
  "value": "Verified",
  "status": "complete",
  "source": "Payer Portal",
  "verified": true,
  "verified_at": "2026-07-07T15:20:00Z",
  "evidence_id": "trace_123",
  "stale_after_days": 7,
  "requires_human_review": false
}
```

## Schema naming — how it evolved

First pass:
```
patients
patient_cards
patient_card_sections
patient_card_fields
patient_card_events
patient_card_requirements
patient_card_sources
patient_card_conflicts
patient_card_audit_logs
```

Better names (current):
```
patients
patient_completion_cards
completion_requirements
completion_field_statuses
verification_events
source_evidence
patient_conflicts
```

**Key table: `completion_field_statuses`**

| Column | Notes |
|---|---|
| id | |
| patient_id | |
| requirement_key | e.g. `insurance_primary`, `pre_op_instructions_sent` |
| section | one of the 12 sections in SKILL.md |
| value | the actual data (or null if missing) |
| status | one of: complete / missing / stale / conflicting / blocked / needs_human_review / not_applicable |
| source_system | one of: SIS / Svigg / Gmail / Google Drive / RingCentral / Payer Portal / Clearinghouse / Staff Manual Entry / Patient Form / Document Extraction |
| verified | bool |
| verified_at | timestamp |
| evidence_id | pointer into `source_evidence` (screenshot/trace/document) |
| stale_after_days | how long this value stays trusted before flipping to stale |
| requires_human_review | bool |
| missing_reason | free text, only when status = missing |
| conflict_reason | free text, only when status = conflicting |
| updated_by | user id or system/agent name |
| created_at / updated_at | |

## Worked example — what the app should always be able to answer

**Question the app should always ask:** what does this patient card need to become complete?

```
Patient: John Smith

Insurance: stale
Referral: complete
Pre-op instructions: missing
Post-op appointment: not scheduled
Chart notes: needs provider review
Recall: not applicable
```

Which the system turns directly into tasks:

```
Verify insurance
Request missing referral note
Send pre-op instructions
Schedule post-op
Route chart to provider
```

## Source system → data mapping

- **SIS/Svigg** → appointments, notes, demographics, chart status
- **Gmail** → referrals, office communication, attachments
- **Google Drive** → documents, surgical packets, templates
- **RingCentral** → calls, SMS, voicemails
- **Payer portal / clearinghouse** → eligibility, insurance verification, authorization status

This mapping is why `source_system` on `completion_field_statuses` matters:
each field's freshness/trust depends on which of these actually last touched
it, not on a single blended "last updated" timestamp.

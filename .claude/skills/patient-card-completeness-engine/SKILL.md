---
name: patient-card-completeness-engine
description: Use when designing or modifying the Patient Card Completeness Engine, completion grid, patient readiness logic, verification fields, source-of-truth mapping, insurance verification status, pre-op/post-op/recall completion, or missing-info workflows for the surgical center app.
---

# Patient Card Completeness Engine Skill

## Purpose

Build and maintain the Patient Card Completeness Engine.

This engine tracks whether each patient is operationally complete across EMR, insurance, communication, documents, scheduling, pre-op, post-op, and recall workflows.

This is not fake data.
This is not a CRM note.
This is not a chatbot memory.

This is an internal operational grid that tells the office exactly what we have, what we do not have, what is stale, what conflicts, and what still needs staff action.

## Core concept

Every patient has a Patient Completion Card.

Every Patient Completion Card is made of sections.

Every section has requirements.

Every requirement has a status.

Every status must include source, verification state, timestamp, and evidence where possible.

See `design-notes.md` in this directory for the full field-level data model (all section fields, the status-object shape, the database schema evolution, and a worked example) — this file states the rules; that file has the reference detail.

## Required sections

1. Identity
2. Insurance / eligibility
3. Referral / intake
4. Chart readiness
5. Documents
6. Scheduling
7. Communication
8. Pre-op
9. Post-op
10. Recalls
11. Open tasks
12. Conflicts / warnings

## Status values

Use only these status values:

- complete
- missing
- stale
- conflicting
- blocked
- needs_human_review
- not_applicable

Do not create vague statuses like "good" or "done."

## Field structure

Every patient card field must support:

- requirement_key
- section
- value
- status
- source_system
- verified
- verified_at
- evidence_id
- stale_after_days
- requires_human_review
- missing_reason
- conflict_reason
- updated_by
- created_at
- updated_at

## Source systems

Expected source systems:

- SIS
- Svigg
- Gmail
- Google Drive
- RingCentral
- Payer Portal
- Clearinghouse
- Staff Manual Entry
- Patient Form
- Document Extraction

## Rules

Do not mark a field complete unless there is a verified value.

Do not guess missing values.

If sources disagree, mark the field conflicting.

If a field has not been verified recently enough, mark it stale.

If a weak patient match exists, mark needs_human_review.

If an external system is unavailable, mark blocked.

Do not let AI overwrite verified data without human approval.

Do not let AI create completion statuses without source metadata.

Do not use mock patient data.

Do not invent patient records.

## Completion logic

A patient card is complete only when all required requirements for that patient type are:

- complete
- or not_applicable

If any requirement is:

- missing
- stale
- conflicting
- blocked
- needs_human_review

then the card is incomplete.

## Patient type templates

Support different required fields by patient/workflow type:

- new consult
- surgery candidate
- scheduled surgery
- pre-op patient
- post-op patient
- recall patient
- referral-only patient

Each template should define required fields.

Example — Scheduled surgery patient requires:
- demographics complete
- insurance verified
- authorization status checked
- surgery appointment scheduled
- pre-op instructions sent
- consent packet complete
- required documents uploaded
- post-op appointment scheduled

## Database tables to use

Recommended tables:

- patients
- patient_completion_cards
- completion_requirements
- completion_field_statuses
- verification_events
- source_evidence
- patient_conflicts
- completion_templates
- completion_template_requirements
- patient_tasks
- audit_logs

`completion_field_statuses` is the key table — see `design-notes.md` for its full column list and how it maps to this skill's Field Structure section above.

## Required behavior

When building code for this system:

1. Create explicit requirement keys.
2. Store field-level status.
3. Store source and timestamp.
4. Store evidence ID where possible.
5. Create tasks automatically for missing/stale/conflicting fields.
6. Do not mark card complete until all required fields pass.
7. Keep audit logs for every status change.
8. Separate staff-entered values from externally verified values.
9. Never assume an EMR field is current without verification.
10. Never use fake data.

## UI requirements

The UI should show:

- patient name
- patient type
- overall readiness status
- completion percentage
- missing items
- stale items
- conflicts
- blocked items
- next best action
- source system for every completed item
- last verified timestamp
- staff owner/task

The card should feel like an operational command center, not a profile page.

## Output expectations

When asked to build or modify this area, produce:

1. Database schema change
2. API routes
3. service logic
4. status rules
5. UI components
6. audit behavior
7. tests
8. blocked items if real integrations are missing

Do not produce vague architecture only.

## Note on this skill's own testing

This is a domain/reference skill (data model + rules for one subsystem), not a discipline-enforcing skill like TDD — it was written directly from a complete spec rather than through the full RED-GREEN-REFACTOR pressure-testing cycle described in `writing-skills`. If this skill turns out to be ambiguous or gets misapplied in practice, tighten it then, with a real observed failure to fix rather than a hypothetical one.

# Clinical OS Backend Blueprint

The app should become a clinical operating system: separate specialized cores that work together through shared contracts, like hardware blocks on a chip.

## Core Idea

```text
External systems
RingCentral, SIS, Svigg, Drive, email, staff actions
        |
Integration Hub
        |
Clinical Event Bus
        |
PracticeOS Kernel
        |
Patient Graph + Workflow Engine + Manager Neural Core
        |
Tasks, timelines, digests, revenue queues, audit logs, dashboards
```

## The iPhone Analogy

- Apple Neural Engine -> Manager Neural Core
  - Looks across every subsystem.
  - Finds discrepancies, missing info, duplicate identities, stale tasks, unmatched faxes, and revenue risk.
  - Creates repair recommendations and eventually repair tasks.

- Secure Enclave -> Security and Audit Core
  - RBAC, audit logs, encrypted secrets, PHI boundaries, BAA/vendor registry.

- High-performance storage -> Patient Graph
  - Canonical patient/case/document/task relationships.

- Media processors -> Document Intelligence and Communications Core
  - Faxes, PDFs, calls, SMS, reminders, summaries, post-op outreach.

- Performance controller -> Workflow Engine
  - Assigns work, enforces due dates, escalates stuck items, balances staff load.

- GPU/display stack -> Analytics and Command Center
  - Fast daily views, revenue reporting, manager briefing, bottleneck dashboards.

## Current Backend Modules

The live endpoint `/api/clinical-os/modules` describes the backend modules and their status.

High-value modules:

1. PracticeOS Kernel
   - Central coordinator for patient state, tasks, timeline, and audit.

2. Manager Neural Core
   - Endpoint: `/api/manager/review`
   - Scans patient data, tasks, follow-ups, faxes, and billing risk.
   - Returns severity-ranked repair findings.

3. Patient Graph
   - Canonical patient identity, case, payer, attorney, procedure, and document model.

4. Clinical Event Bus
   - Planned durable log of every source event and staff action.

5. Workflow Engine
   - Converts events into tasks, owners, due dates, and review queues.

6. Integration Hub
   - RingCentral, SIS, Svigg/WebeDoctor, Google Drive, future EHR/payer APIs.

7. Document Intelligence
   - Fax/PDF intake, OCR/text extraction, classification, patient match, timeline filing.

8. Revenue Core
   - AR, EOB, denial, appeal, underpayment, reconciliation intelligence.

9. Communications Core
   - Calls, texts, reminders, post-op follow-ups, attorney outreach drafts.

10. Security and Audit Core
    - RBAC, audit log, PHI guardrails, connector secrets, vendor registry.

11. AI Gateway
    - One approved path for summaries, classification, drafting, and staff copilots.

12. Deployment and Tenant Core
    - What makes it sellable: tenant settings, backups, updates, support diagnostics.

## Shared Contracts

These contracts are the backend equivalent of Apple silicon interconnects.

Event:
```json
{
  "event_id": "evt_...",
  "tenant_id": "practice_...",
  "event_type": "fax.received",
  "entity_ref": {"patient_id": "pat_..."},
  "source_system": "ringcentral",
  "payload_hash": "...",
  "occurred_at": "2026-06-12T09:00:00"
}
```

Task:
```json
{
  "task_id": "task_...",
  "tenant_id": "practice_...",
  "patient_id": "pat_...",
  "owner_role": "billing",
  "status": "pending",
  "due_date": "2026-06-13",
  "source_event_id": "evt_..."
}
```

Document:
```json
{
  "document_id": "doc_...",
  "tenant_id": "practice_...",
  "patient_id": "pat_...",
  "document_type": "EOB",
  "storage_ref": "secure://...",
  "match_confidence": "high",
  "review_status": "filed"
}
```

Audit:
```json
{
  "audit_id": "audit_...",
  "tenant_id": "practice_...",
  "actor_id": "staff_...",
  "action": "fax.attach_timeline",
  "entity_ref": {"document_id": "doc_..."},
  "timestamp": "2026-06-12T09:05:00",
  "risk_level": "medium"
}
```

## Manager Neural Core Jobs

V1 jobs now exposed through `/api/manager/review`:

- Missing DOB, contact info, insurance, attorney/LOP, or DOS.
- Duplicate-looking patient records.
- Revenue-risk patients with high open balances or sparse context.
- Overdue or blocked team tasks.
- Overdue post-op follow-up calls.
- Unmatched or low-confidence faxes.

Next jobs:

- Auto-create repair tasks with manager approval.
- Suppress already-reviewed issues.
- Learn per-practice thresholds.
- Detect contradictions across SIS, Svigg, RingCentral, Drive, and staff notes.
- Score practice health every morning.

## Commercialization Gates

Before selling this to other practices:

- Move from loose JSON to SQLite/Postgres with migrations.
- Add tenant isolation.
- Add role-based access control.
- Encrypt secrets and protect connector credentials.
- Add immutable audit logs for PHI-touching actions.
- Add backups and restore testing.
- Add connector health checks and retry/dead-letter handling.
- Add support diagnostics that exclude PHI by default.
- Create a vendor/BAA registry for cloud, OCR, AI, messaging, and hosting.
- Keep low-confidence patient matching human-reviewed.

## Compliance Anchors

- HHS says the HIPAA Security Rule requires administrative, physical, and technical safeguards for ePHI: https://www.hhs.gov/hipaa/for-professionals/security/laws-regulations/index.html
- HHS cloud guidance says cloud services handling ePHI need HIPAA-compliant business associate agreements: https://www.hhs.gov/hipaa/for-professionals/special-topics/health-information-technology/cloud-computing/index.html
- HHS risk analysis guidance treats risk analysis as the first step for protecting ePHI: https://www.hhs.gov/hipaa/for-professionals/security/guidance/guidance-risk-analysis/index.html
- ONC information-blocking policy matters if the product starts exchanging EHI with certified health IT or patients: https://healthit.gov/information-blocking/

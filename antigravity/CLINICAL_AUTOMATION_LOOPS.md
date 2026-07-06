# Clinical Automation Loop Map

The app should behave like a set of small clinical feedback loops, not one giant bot. Each loop watches a source, writes normalized state locally, creates a staff task when judgment is needed, and records an audit event.

## Default n8n Pattern

```text
Cron/Webhook trigger
-> Fetch source data
-> Normalize into local SQLite/JSON
-> Rules classify urgency and owner
-> Create/update dashboard task
-> Human review when confidence is low
-> Audit log
```

## Loops to Wire

1. Morning Command Center
   - Trigger: weekday morning or dashboard button.
   - Sources: SIS, Svigg/WebeDoctor, local AR reports, reconciliation endpoint.
   - Output: automation readiness, AR import, discrepancy scan, manager blockers.

2. Fax Digest
   - Trigger: RingCentral inbound fax polling.
   - Sources: RingCentral message store, PDF/TIFF attachments, patient database.
   - Output: classified fax, patient match, review task, timeline event.

3. EOB Payment Reconciliation
   - Trigger: new EOB fax or AR import.
   - Sources: fax summaries, SIS ledger, billing ledger.
   - Output: unposted payment task, write-off review, reconciliation evidence.

4. Denial and Appeal
   - Trigger: denial fax, denied AR status, denial language in notes.
   - Sources: fax digest, billing notes, carrier fields, attorney fields.
   - Output: appeal checklist, owner assignment, due date, packet status.

5. Prior Authorization
   - Trigger: new referral or upcoming DOS without auth evidence.
   - Sources: patient worklist, SIS schedule, authorization documents.
   - Output: missing-auth task, payer follow-up, pre-DOS escalation.

6. Post-Op Follow-Up
   - Trigger: completed procedure or recovery schedule.
   - Sources: followups.json, team tasks, RingCentral calls.
   - Output: nurse callback queue, outcome note, escalation task.

7. Attorney and Case Status
   - Trigger: aged balance, no update after follow-up window, legal correspondence.
   - Sources: billing ledger, attorney metadata, faxes/emails, case notes.
   - Output: attorney status draft, follow-up timer, timeline update.

8. Compliance Watchdog
   - Trigger: every automation run and every staff review action.
   - Sources: audit logs, task changes, fax review actions, automation status.
   - Output: PHI/vendor blockers, low-confidence filing warnings, audit trail.

## HIPAA Guardrails

- Do not send PHI to n8n Cloud, external AI APIs, or OCR vendors unless the path is approved for HIPAA and covered by a BAA.
- Prefer local execution for OCR, rules, matching, and SQLite writes.
- Keep low-confidence patient matches in staff review.
- Keep source documents under the approved scratch/data folder, not git.
- Never delete source RingCentral/SIS/Svigg records in v1.

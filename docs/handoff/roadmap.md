# Roadmap — where this stands, what's next

## The project, one sentence

Build the TriFetch-style automation platform in-house: staff types a task,
AI proposes a specific action, a human approves it, the system executes it
against SIS/Svigg with proof, and every step is logged — for a surgical
center that treats **head-injury and workers'-comp patients**.

## Domain specifics that shape every decision here

- Clinic: Atlantic Pain & Wellness Institute (Dr. Gupta) — pain management +
  surgical center, includes a **Head Injury Institute**.
- Heavy **workers' comp (WC)** caseload: claim numbers, adjusters, employers,
  attorney/LOP (letter of protection) tracking, carrier-specific rules.
  Existing prior art: `antigravity`'s WC referral routing, `ai-phone-intake`'s
  `wc_outreach.py` lifecycle classifier, the MDManage AR roster
  (WC/NF/Major-Med/Lien billing categories, ~$5.36M book).
  This is not a generic medical office — WC/head-injury cases have their own
  documents (WC Form, Head Injury Evaluation), their own stakeholders
  (adjusters, attorneys), and their own stalled-case failure modes.
- Two EMRs: SIS Complete (surgical center side) and Svigg/WEBeDoctor
  (pain management side) — a patient's full picture requires checking both.

## Current state (all committed, `back-office/` monorepo)

- **Foundation (Phase 1): done.** Postgres schema (20+ tables, hash-chained
  append-only audit log), Action Registry (24 action contracts, 8 marked
  `IMPLEMENTED_VENDORED`, 16 honestly `BLOCKED_PENDING_REAL_SELECTOR_OR_CREDENTIALS`),
  local EMR Bridge re-vendored from the real, production-tested SIS/Svigg
  clients (not a stale copy — see commit history), FastAPI backend
  (parser → registry validation → approval queue → Temporal handoff), a
  Temporal worker enforcing the write-approval-token gate, Next.js dashboard
  skeleton (not styled/finished — web work is paused, see below).
- **`update_patient_demographics` is deliberately blocked** — confirmed
  broken server-side in production (200 OK, correct fields, doesn't persist).
  Do not re-enable without new evidence.
- **Web/UI work is paused** on the owner's explicit instruction until the
  whole plan is locked. Don't resume without being told to.
- **Skills installed:** `patient-card-completeness-engine` (the per-patient
  completion-grid design — see below, this is the next feature's data
  model), engineering-practice skills (`superpowers`), `ui-ux-pro-max`
  (design reference, ready for when web work resumes).
- **Toolbox repos added to scope:** `n8n-office-mirror` (real production
  Back Office code), `ai-phone-intake` (real production phone/SMS/fax code,
  full history), `koko-intake` (early-stage kiosk, P0/P1 only).

## Next feature, as the owner specified it directly

> "I need to make sure the system I make is able to request the pre-op,
> post-op, follow-up, and recalls."

This is not a new idea — it's already modeled as a section of the
`patient-card-completeness-engine` skill's card (`Pre-op / post-op / recalls`:
pre-op instructions sent, pre-op confirmed, surgery confirmation sent,
post-op instructions sent, post-op call completed, recall created, recall
completed). Building this feature means making those fields real:

1. **Trigger logic**: something (a scheduled check, or a status change on
   the patient card) decides "this patient needs a pre-op call/text now."
2. **Action Registry entries**: this is patient-facing outbound contact —
   should follow the **outbound-call compliance-gate pattern** found in
   `ai-phone-intake` (see `research/reliability-audit.md`'s "positive
   finding"): kill switch → explicit human compliance confirmation →
   do-not-call/consent check → execute → verify. Not a plain approve-and-send.
3. **Vapi integration**: the phone system is already live at the office —
   per the owner, infrastructure gets built first, the Vapi API key gets
   added later. Don't block the design on having the key now.
4. **Ties into `completion_field_statuses`**: every send should write back
   to the patient card (`pre_op_instructions_sent: complete, source: ...,
   verified_at: ...`), not live as a disconnected task.

## Known open items (not yet decided)

- **Where does the message-reliability fix live** (the reconciliation-job
  gap from `research/reliability-audit.md`)? Patched into the old
  `ai-phone-intake` app, or built fresh as a Temporal workflow in the new
  platform? Leaning toward the latter (owner already chose Temporal
  "from day one" and wants it built so it doesn't crash) but not confirmed.
- **Repo split**: `back-office/` still lives as a folder inside
  `shubh2300/shubh2300` rather than its own repo (owner approved creating
  a separate `back-office` repo earlier but it was never created — GitHub
  App can't create repos on this account, needs the owner to do it manually
  via github.com/new).
- **Approval roles**: v1 is "any staff member approves anything" — role-based
  approval (front desk vs. manager vs. provider) is deferred, schema already
  supports it (`roles.can_approve_risk_levels`).

## Read next

`research/reliability-audit.md` and `research/atlantic-hub-fork.md` for the
engineering lessons already learned from the real, live codebases — both are
load-bearing input for how the pre-op/post-op/recall feature (and anything
else touching Vapi/RingCentral) should be built.

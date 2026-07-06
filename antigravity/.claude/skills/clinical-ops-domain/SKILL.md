---
name: clinical-ops-domain
description: Healthcare operations domain expertise for THIS app — Atlantic Pain & Wellness Institute, a pain-management and surgical center (Dr. Gupta). Use this whenever a task touches the clinical workflow or patient data: intake, insurance verification, attorney LOPs, scheduling, pre-op/post-op, recovery callbacks, billing, denials/appeals, or any patient/attorney communication. It defines the real case pipeline, the MVA/WC/PI case types, the carriers and legal concepts, the next-action logic, and the non-negotiable safety rules for patient data (never fabricate, label AI summaries as draft, PHI handling). Consult it before building clinical features or writing clinical copy so the work fits how the clinic actually runs. IMPORTANT: for patient- or attorney-facing messages, this skill governs tone and content — do NOT use the marketing copywriting/cold-email/email skills for clinical communications.
---

# Clinical Ops Domain — Atlantic Pain & Wellness

The app supports a **pain-management and surgical center**. Most patients arrive through **referrals** and **accident/injury cases**, not retail marketing. Tone everywhere is clinical, warm, and precise — never salesy.

## The case pipeline (memorize this order)

```
phone intake → insurance verification → attorney LOP → scheduling →
pre-op → surgery → post-op recovery calls → billing → case resolution
```

Almost every feature maps to a stage here. When something is "stuck," it's stuck *at a stage* — name the stage and the owner.

## Case types

| Type | Meaning | Notes |
|---|---|---|
| **MVA** | Motor vehicle accident | Often involves an attorney + auto carrier; LOP common |
| **WC** | Workers' compensation | Employer/comp carrier; authorization-heavy; `.badge-wc` |
| **PI** | Personal injury | Attorney-driven; payment via settlement/LOP |
| (intake) | New referral not yet triaged | `.badge-new` |

Status badges in the UI: `new`, `wc`, `pending`, `success` — keep these meanings consistent with the design system.

## Key concepts & entities

- **Referring doctor** — source of most patients; referral emails are parsed into intake (see `Code.js` / Gemini extraction).
- **Attorney / LOP (Letter of Protection)** — for MVA/PI/WC, an attorney's LOP lets the clinic treat now and get paid from the settlement. Missing/expired LOP is a common blocker.
- **Insurance carriers** — State Farm, Allstate, Geico, NJ manufacturers, etc. Each has its own authorization quirks; WC and auto auth differ from commercial health insurance.
- **Denial / appeal** — surgeries and procedures get denied; denials need an owner, a reason, and an appeal path. The Billing Clearance Agent reasons about blockers.
- **Recovery callbacks** — post-op patients get scheduled nurse callbacks; the Call Workload Balancer distributes these across nurses.
- **Monday Clinical Meeting** — the weekly coordination surface for upcoming patients, missing collateral, task ownership.

## Next-action logic (what the dashboard computes)

A patient's "next action" is derived from state, roughly in priority order: missing documents → denial follow-up → intake issue → auth/scheduling → legal (LOP) status → closeout review. When you add data or features, preserve this ordering so the surfaced action is the most urgent one.

## Safety rules — non-negotiable

These reflect both clinical reality and this project's posture (local app, real-clinic intent):

1. **Never fabricate patient data.** Only reference data explicitly present. If a field is unknown, say "unknown / not on file," never invent a name, DOB, carrier, or claim number. (Koko's system prompt enforces this too.)
2. **Label AI-generated clinical content as DRAFT requiring clinician review.** Injury summaries, clinical reconstructions, and head-injury reports are drafts for a human to verify — never present them as finalized medical record.
3. **PHI handling.** This app runs locally. Before sending any PHI to an external model (Gemini/OpenAI) or service, confirm a BAA/HIPAA posture exists. Prefer keeping patient-like data local; treat `scratch/` and logs as potentially sensitive (they're gitignored for a reason).
4. **Clinical comms ≠ marketing comms.** Patient welcome messages, scheduling notes, and attorney coordination are clinical/administrative. Do **not** apply marketing funnel framing, CTAs, growth copy, or the marketing skills (cold-email, copywriting, emails) to them. Plain, respectful, accurate.
5. **Auditability.** If a feature resolves denials, missing docs, or callbacks beyond demo mode, it should record who did what and when.

## When writing clinical copy

- Address the patient/attorney directly and plainly. State purpose, next step, and a real contact (the practice phone number — placeholder support/records emails were intentionally removed).
- No emoji, no marketing superlatives, no urgency-manufacturing.
- For attorneys: factual case-coordination tone; don't over-share clinical detail beyond what's needed.

## Pointers

- Backend routes and data shapes: `server.py` (`/api/patients`, `/api/monday-meeting`, `/api/tasks`, `/api/koko`, …).
- Referral parsing + Google Sheets/Docs sync: `Code.js` (Apps Script).
- Koko's persona and guardrails: `gpt_client.py`.
- UI for all of this: see **clinic-design-system** and **frontend-code-quality**.

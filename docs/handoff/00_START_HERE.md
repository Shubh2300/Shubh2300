# Start here

You're picking up a session building a real, in-house automation platform for
a surgical center — think "TriFetch, built ourselves." The owner is an intern
at the clinic with limited CS background; guide decisions, don't just execute
requests blindly, and always check what already exists before building new.

## Read in this order

1. **`/PROJECT_OVERVIEW.md`** (repo root) — the full system map: three
   pre-existing codebases (`n8n-office`, `ai-phone-intake`, `antigravity`)
   analyzed in depth, what each one does, and the canonical "which piece do
   we use for which function" picks.
2. **`/DECISIONS.md`** (repo root) — 15 locked build decisions (stack,
   deployment target, approval model, Temporal, repo layout, timeline) plus
   standing safety guardrails. Don't re-litigate these without new
   information that changes the calculus.
3. **`docs/handoff/roadmap.md`** — current build state and the very next
   feature to build (pre-op/post-op/follow-up/recall requests), as specified
   directly by the owner.
4. **`docs/handoff/research/`** — two files of load-bearing engineering
   findings from the real, live codebases (not background reading):
   - `reliability-audit.md` — a real, confirmed gap: the live phone system
     can silently lose a call if the app is down when it ends, with zero
     mechanism to notice. Also documents a good pattern worth copying
     (the outbound-call compliance gate).
   - `atlantic-hub-fork.md` — a previous, unfinished attempt at almost this
     exact platform, found buried in a fork branch. What's real vs. what's
     a disconnected local prototype, and what to salvage.
5. **`.claude/skills/patient-card-completeness-engine/`** — the data model
   for tracking per-patient completeness (identity, insurance, chart
   readiness, scheduling, pre-op/post-op/recalls, open work). This is the
   skill that governs the next feature.

## Standing rules already agreed with the owner

- **No web/UI work** until the owner says the plan is locked. The Next.js
  dashboard exists as an unstyled skeleton — do not resume styling/building
  it without being asked.
- **"This looks newer, replace the old code" requires a gate**: read-only
  inspect → diff report → identify what's real vs. stale → check for
  secrets → get the owner's explicit approval → only then modify. Don't
  silently overwrite significant existing work.
- **Never fabricate data.** No mock patients, no invented EMR selectors, no
  claiming an action succeeded without EMR verification + a screenshot.
- **`update_patient_demographics` stays blocked** — confirmed broken
  server-side in the real production system, not a missing-selector issue.
- The owner has a **designated test patient** for all EMR write development —
  ask for its identifiers before building/testing any write action; never
  test writes against real patients.
- The owner explicitly said: **treat all four legacy codebases
  (`n8n-office`, `ai-phone-intake`, `antigravity`, `koko-intake`) as a
  toolbox** — mine them for proven pieces, don't rebuild from scratch, but
  the new platform's architecture is not bound to any of their structures.

## Repos in scope

- `shubh2300/shubh2300` — this repo. `back-office/` folder holds the new
  platform monorepo (not yet split into its own repo — owner approved this
  but hasn't created it on GitHub yet).
- `shubh2300/n8n-office-mirror` — real, current production Back Office code
  (mirrored from `mainlinesurgery-a11y/n8n-office`, branch
  `backoffice-autopilot-live-20260705`).
- `shubh2300/ai-phone-intake` — real, current production phone/SMS/fax code
  (full git history, branch `fork/aiops-pipeline` has the most recent work).
- `shubh2300/koko-intake` — early-stage patient-intake kiosk (P0/P1 only,
  voice + PDF output not yet built).

If any of these aren't in scope for your session, they can be added with the
`add_repo` tool (same GitHub owner as this repo — cross-owner adds aren't
supported in one session, so start fresh with the right owner if you ever
need `mainlinesurgery-a11y/*` directly).

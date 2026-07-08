# Start here

You're working on an in-house automation platform for a surgical center —
similar to TriFetch, but built ourselves. The owner is an intern at the
clinic with limited CS background; guide decisions, don't just execute
requests blindly, and always check what already exists before building new.

## The vision — read this before anything else

This is meant to become an **all-knowing platform**: one unified store of
patient data that cross-references every input — EMRs, email, RingCentral,
faxes, documents — instead of a dashboard bolted onto disconnected systems.
Anything repetitive in the back office (billing follow-up, insurance
checks, etc.) should eventually be handled by an agent, not a person.

**Target operating model:** agents work autonomously — investigating,
gathering evidence, monitoring for problems — but only ONE human is in the
loop, and their job is narrow: approve or deny the specific actions agents
propose. Autonomous work (looking something up, checking for a gap,
confirming a problem is real) does not need approval; only the resulting
ACTION does.

Worked example the owner gave: if a patient in the unified dashboard is
missing an EOB (Explanation of Benefits), the agent should actively search
for it across every connected source on its own — no approval needed just
to look. Only once it has exhausted the search and is confident the EOB is
genuinely missing does it escalate to the dashboard, in a clean,
purpose-built card for that specific issue type (not a generic wall of
text) — showing what's wrong, what was already checked, and a recommended
action, with quick-action buttons to act immediately.

**Zero tolerance for junk approval cards.** Named failure mode: an
irrelevant message (the owner's example — a July 4th holiday email) must
never generate an approval card. Anything not genuinely patient-related
should be auto-classified as spam/trash/not-important and filtered out
before it ever reaches a human, with high confidence, not a guess. This has
to be both accurate (never silently drop something real) AND aggressive
(never surface something fake) — a cluttered, unprofessional-looking system
is a real failure here, not a cosmetic one.

**Resources are not a constraint.** Google APIs are already available.
Budget for tools, certifications, hardware, or software is effectively
unlimited — if a design would be better with something not currently
available, say so explicitly rather than silently scoping down to fit an
assumed budget.

**Design bar:** this must not read as "AI slop" — dense and honest, not
decorative. See `PROJECT_OVERVIEW.md` and prior session discussion for the
specific do/don't list (worth formalizing into its own doc if it hasn't
been yet).

## Read in this order

0. **`docs/handoff/original-spec/`** — the owner's two original founding
   prompts, preserved verbatim: `verified-emr-action-bridge.md` (the
   architecture spec: action contracts, EMR bridge design, DB tables) and
   `north-star-vision.md` (the product vision: 10 modules, risk levels,
   MVP scope). Read these for the actual original ask in full fidelity —
   everything below is how it got interpreted and where it diverged.
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
4. **`docs/handoff/phase1-build-log.md`** — how the first build pass actually
   went: a real schema mismatch caught between parallel agents, more bugs a
   reconciliation agent found beyond the original diagnosis, an unsupervised
   concurrent-edit collision caught by verifying disk state, and the
   verification pattern (compile + real tests + typecheck at every commit,
   never trust a completion report alone) worth repeating regardless of
   whether Phase 1's code itself survives into 2.0.
5. **`docs/handoff/research/`** — engineering findings from the real, live
   codebases and one design discussion (not background reading):
   - `reliability-audit.md` — a real, confirmed gap: the live phone system
     can silently lose a call if the app is down when it ends, with zero
     mechanism to notice. Also documents a good pattern worth reimplementing
     fresh (the outbound-call compliance gate).
   - `atlantic-hub-fork.md` — a previous, unfinished attempt at almost this
     exact platform, found buried in a fork branch. What's real vs. what's
     a disconnected local prototype, and which patterns are worth rebuilding
     (never the code itself).
   - `design-principles.md` — what separates "AI slop" from a clean,
     professional tool, prompted by a TriFetch screenshot. Read before web
     work resumes.
6. **`.claude/skills/patient-card-completeness-engine/`** — the data model
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
- **This is a full rewrite (2.0), not an assembly of old parts.** The four
  legacy codebases (`n8n-office`, `ai-phone-intake`, `antigravity`,
  `koko-intake`) are version 1.0 — reference material only. Tear each one
  down to understand what actually works and what fails, the way you'd
  strip a car to the frame before rebuilding it with modern parts. Nothing
  gets copied, ported, or vendored in as-is — every piece gets built fresh
  with better tools. (Note: `back-office/bridge/integrations/` currently
  contains SIS/Svigg client code vendored in verbatim under the old framing,
  before this rule was set — whether that gets rewritten too is an open
  question for the owner, not yet decided either way.)

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

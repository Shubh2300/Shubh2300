# Back Office — Decision Record
*Locked in with Shubh on 2026-07-07. These answers drive the build. Change them here, not in chat history.*

| # | Question | Decision |
|---|---|---|
| 1 | Clinic authorization | **Leadership (Dr. Gupta / office) fully aware and approved** — reads and writes. |
| 2 | BAA status | **BAA in place** per Shubh. (Action: confirm which provider(s) it covers before PHI flows to any LLM.) |
| 3 | API keys in backup | **Do not rotate — reuse existing keys.** Guardrail: keys live ONLY in `.env` on the office machine, never in git, never in code, never in logs. |
| 4 | ai-phone-intake backup / patient DB | Use its **code as a toolbox**; old patient DBs not needed — **the platform builds its own database**. |
| 5 | Foundation | **New platform per the Verified EMR Action Bridge spec.** All three prior projects are a **parts library ("toolbox"), not the base** — pull proven pieces instead of rebuilding, but the architecture is new. |
| 6 | Bridge language | **Python.** Criterion was "strongest, most reliable EMR connection" — the live-verified SIS/Svigg clients and the existing MCP server are Python. New dashboard is still Next.js/TypeScript. |
| 7 | Temporal | **Yes — Temporal from day one.** Runs on the office machine via Docker; workflows must survive crashes/restarts. |
| 8 | Repo layout | **New monorepo `back-office`** (apps/web, apps/api, bridge/, packages/, db/). Built on this branch first; transplants cleanly once the `back-office` GitHub repo exists. |
| 9 | Deployment | **Everything on the office machine** — Postgres, Temporal, API, web dashboard, and the EMR bridge on one box (docker-compose), inside the office network. |
| 10 | Biggest pain | All four hurt, but the standout gap: **insurance-process status tracking and payment tracking simply don't happen today** — they become a first-class module, alongside referrals/phones/scheduling. |
| 11 | Approvals | **v1: any staff member can approve anything** (single shared queue). Role-based approvals structured in the schema now, enforced later. |
| 12 | Test patients | **Shubh has a test patient** — all EMR write development runs only against it (allowlist-enforced, like the existing `22041163` pattern). Need its identifiers in both EMRs. |
| 13 | Action verification | **Build auto-verify before going live** — after every write the bridge re-reads the EMR and only marks complete if the change is really there, **plus screenshot proof of success captured by the headless browser on every action.** |
| 14 | Emma / prior projects | Separate projects; **mix the good pieces from both into the new platform**. Toolbox, not the whole toolbox. |
| 15 | Timeline | **ASAP — build and test along the way, fix bugs as they appear, iterate fast.** |

## Standing guardrails (non-negotiable, from the spec + prior house rules)
- AI never controls the EMR directly; it only proposes actions from the Action Registry. Deterministic Playwright only.
- Two-phase everything: plan → human approval → commit → verify → screenshot + trace + audit log.
- No mock EMR data, no fake patients, no fabricated fields. Missing real selectors/credentials ⇒ `BLOCKED_PENDING_REAL_INPUT`, never invented.
- Weak patient matches stop and go to human review. Signed notes: addendum only, provider-approved.
- No PHI to any AI provider outside the BAA-covered path. No PHI in git, logs, or this repo — ever.

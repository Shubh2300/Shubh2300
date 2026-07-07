# Back Office — Verified EMR Action Bridge

A staff-facing surgical-center workflow platform for Atlantic Pain & Wellness.
Staff describe what they need in plain language; the platform parses it into a
structured, reviewable action, a human approves it, and only then does a durable
Temporal workflow drive the EMR through a **verified** bridge that re-reads the
change and captures a screenshot before anything is marked done.

> Leadership-approved reads and writes (DECISIONS.md #1). No mock data, no fake
> patients, no fabricated results — ever. Missing real selectors/credentials
> surface as `blocked`, never invented.

---

## Architecture

```
  staff (web app)
        │  free-text task
        ▼
  ┌───────────────┐   POST /intents/parse   ┌──────────────────┐
  │  API (FastAPI)│ ───────────────────────▶│ AI parser (OpenAI│
  │  apps/api     │◀─────────────────────── │ structured output)│
  └──────┬────────┘   ActionIntent (proposal)└──────────────────┘
         │  validate against Action Registry
         │  POST /approvals  → pending (human reviews the plan)
         │  POST /approvals/{id}/approve  (records who/when)
         ▼
  ┌───────────────┐  start ExecuteActionWorkflow
  │ Temporal      │──────────────────────────────┐
  └───────────────┘                              ▼
                                        ┌──────────────────┐
                                        │ worker/          │
                                        │  validate_intent │
                                        │  call_bridge ────┼──▶ EMR bridge (HOST)
                                        │  verify_result   │    POST /sis/*  /svigg/*
                                        │  record_outcome  │    → standard envelope
                                        └────────┬─────────┘      (status, verified,
                                                 │                 screenshot_id, …)
                                                 ▼
                                     action_runs + hash-chained audit_logs
                                     (needs_human_review → human_review_queue)
```

Two-phase everything: **plan → human approval → commit → verify → screenshot +
trace + audit log**. A run completes as `verified` only when the bridge reports
`status=success` AND `verified=true`. `blocked` / `needs_human_review` /
ambiguity route to the human review queue — never a silent success.

### Components (this repo owns apps/api + worker; siblings own the rest)

| Path | What it is |
|------|-----------|
| `apps/api/` | Staff-facing FastAPI backend (tasks, parse, approvals, action runs, audit, patients). |
| `worker/` | Temporal worker running `ExecuteActionWorkflow` and its four activities. |
| `apps/web/` | Next.js dashboard (sibling). Talks to the API at `NEXT_PUBLIC_API_URL`. |
| `bridge/` | Local Python FastAPI EMR bridge (sibling). Runs on the **host**, not a container. |
| `db/schema.sql` | Postgres schema, 20 tables (sibling). |
| `packages/action-registry/` | `actions.json` + `action_intent.schema.json` (sibling). |

---

## The EMR bridge runs on the HOST (important)

The bridge holds a **persistent, logged-in real browser session** to SIS and
Svigg. That session cannot live in an ephemeral container, so the bridge is
**not** in `docker-compose.yml`. Start it directly on the office machine,
listening on `:8600`. The API and worker containers reach it via
`host.docker.internal:8600` (wired through `extra_hosts` + `BRIDGE_URL`).

---

## Running on the office machine

```bash
cp .env.example .env          # fill in real values on the office machine only
# 1) start the EMR bridge on the HOST (separate process, port 8600)
# 2) bring up the stack:
docker compose up -d --build
```

Services:

| Service | Port | Notes |
|---------|------|-------|
| postgres | 5432 | Initialized from `db/schema.sql` on first boot. |
| temporal | 7233 | `temporalio/auto-setup` (creates its DBs in the same Postgres). |
| temporal-ui | 8080 | Workflow inspection. |
| api | 8000 | FastAPI. `GET /health` for liveness. |
| worker | — | Temporal worker (no exposed port). |
| web | 3000 | Next.js dashboard. |
| **bridge** | **8600** | **On the host, not compose.** |

Local API dev (outside docker), from `apps/api/`:

```bash
pip install -r requirements.txt
python3 -m pytest tests/          # pure-logic unit tests
uvicorn main:app --reload
```

---

## Environment variables (names only — see `.env.example`)

`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL`,
`TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE`, `BRIDGE_URL`,
`OPENAI_API_KEY`, `OPENAI_PARSER_MODEL`, `ACTION_REGISTRY_PATH`,
`ACTION_INTENT_SCHEMA_PATH`, `AUDIT_PEPPER`, `ORGANIZATION_ID`, `CORS_ORIGINS`,
`NEXT_PUBLIC_API_URL`.

Secrets live ONLY in `.env` on the office machine (DECISIONS.md #3) — never in
git, never in code, never in logs. If `OPENAI_API_KEY` is absent, the parser
endpoint returns a structured `parser_unavailable` error rather than any canned
output.

---

## Phase roadmap

- **Phase 1 — Foundation** ✅ API skeleton, Temporal worker, verified-bridge
  workflow, hash-chained audit, approval flow, action registry validation.
- **Phase 2 — SIS reads** — wire read actions (`find_patient`,
  `get_patient_demographics`, appointments, referrals, documents, notes).
- **Phase 3 — Svigg reads** — schedule/ledger reads via the browser-RPA bridge.
- **Phase 4 — Scheduling writes** — book / cancel / reschedule / confirm /
  no-show, each with post-action re-read verification + screenshot proof, run
  only against the allowlisted test patient first.
- **Phase 5 — Patient writes** — create patient, demographics, insurance,
  referring provider, document upload.
- **Phase 6 — Notes** — draft / update unsigned / signed-note addendum
  (provider-approved), route for provider review.

---

## Safety guardrails (non-negotiable)

- AI never controls the EMR directly — it only proposes actions from the Action
  Registry; the deterministic bridge executes.
- No PHI in git, logs, or this repo. Audit log stores only identifiers *used to
  match* and hashes; application logs carry ids/counts only.
- Weak patient matches halt and go to human review. Signed notes: addendum only.
- Writes are never retried on unknown outcomes — they route to human review.

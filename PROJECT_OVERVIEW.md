# Back Office — Project Overview & System Map
*Written 2026-07-07 from a full review of all three codebases. This is the "state of the union" before any new code is written.*

## The one-paragraph summary

You don't have "a rough copy." You have **three substantial, overlapping systems** built for the same clinic (Atlantic Pain & Wellness / Dr. Gupta), each solving a slice of the office-automation problem, each with real working integrations and real safety engineering. The job ahead is **not** to build from scratch — it is to pick a foundation, extract the best "puzzle pieces" from all three, kill the duplicates, and close specific known gaps. Roughly 60–70% of the "Verified EMR Action Bridge" spec already exists in working Python code.

---

## The three codebases

### 1. `n8n-office/` (in this repo) — the closest thing to the Back Office vision
- **Back Office app** (`app/`, FastAPI, :8787): staff chat assistant with a strict READ/WRITE tool split. Reads hit the EMRs live; **writes never touch the EMR directly** — they enqueue into `app/approvals.py`, a human approves on screen, and only then does the scraper execute. This *is* the two-phase plan/commit pattern from the spec.
- **EMR integrations** (`python/integrations/`):
  - `sis_client.py` — SIS Complete accessed via its real REST API *through* a logged-in Playwright page (Auth0 cookies ride along). ~25 read endpoints, live-verified (`journal/sis-reads.md`).
  - `svigg_scraper.py` — Svigg/WEBeDoctor pure browser RPA (no API exists). Search, ledger, schedule reads, book, cancel, create-patient — every write triple-gated (env kill-switch + explicit confirm flag + test-account allowlist).
  - `audit_log.py` — SHA-256 hash-chained, append-only, tamper-evident audit log (HIPAA §164.312(b)).
- **Known open defects** (documented at repo root, fix code written but **not merged**):
  - `SVIGG_SCHEDULING_CONTRACT.md` — HAR-derived ground truth of Svigg's wire protocol.
  - `MCP_CANCEL_BOOKING_FIX.md` — six defects D1–D6. Already fixed in the live scraper: D3 (post-cancel date guard), D5a. Still outstanding: **D1** (stale-frame scan on cancel), **D4** (no encounter-id cancel path), **D5b/c** (no booking bounce detection — "submitted" isn't verified), **D6** (name-order intolerance in booking's patient resolve).
  - `svigg_reliability_fix.py` — graft-ready code for all six, waiting to be merged into `python/integrations/svigg_scraper.py`.
  - **P1**: app + MCP each import the scraper separately → a code change requires restarting all processes or they run stale logic. **P2**: dead headless browser is not auto-recovered.
- **n8n workflows** (`n8n-workflows/`, `python/flows/`): scaffolds only, never activated; their write paths are deliberate `NotImplementedError` stubs. Effectively dead weight / reference material.
- **MCP server** (`mcp/atlantic_emr_server.py`): exposes the same EMR stack to Claude sessions. Its README says "read-only, 7 tools" but code now includes book/cancel/create-patient — **README is stale**.

### 2. `ai-phone-intake` (uploaded zip — ⚠️ NOT on GitHub anywhere)
The largest and most production-hardened system. FastAPI monolith (`app/main.py` ~10k lines, `app/database.py` ~18k lines), ~1,000+ tests, deployed to **Azure** with a deploy gate that requires the owner's cryptographically signed commit.
- **Vapi voice agent "Emma"**: inbound phone intake webhook + mid-call tool webhooks + `emma_brain.py` Claude escalation tier (built after a documented incident analysis: 44% transfer rate, mis-transcribed callbacks, a Spanish-caller incident).
- **RingCentral**: SMS/fax/call-log/contacts sync, webhook subscriptions, FCC STOP-keyword handling.
- **`outbound_drafts.py`** — the cleanest channel-agnostic draft → approve → send state machine in any of the three codebases. Nothing patient-facing ever auto-sends.
- **`identity_graph.py`** — EMPI-lite patient matching: immutable mention store, weighted pairwise scoring, shadow-report loop to tune weights before trusting auto-link. The most sophisticated patient matcher you own.
- **Billing intelligence** (`billing_intelligence.py` ~13k lines): denial categorization, collectibility scoring, deadline risk, CPT extraction; plus MDManage AR roster parsing (~$5.36M book).
- **Records-release consent gate** (`release_terms.py`): only a signed release authorizes third-party disclosure; overrides are logged, never silent.
- **SIScomplete access via Chrome extension session capture**: rides a staff member's real authenticated browser session instead of storing a password. Third distinct EMR-access pattern.
- Most connectors are code-complete but **feature-flagged off** pending credentials/rollout.

### 3. `antigravity/` (in this repo) — referral/email intake + ops dashboard
- **Gmail referral intake** (Google Apps Script, two generations): v2 (`intake_gs/Code.gs`, 3.5k lines) has ledger-backed idempotency, PDF OCR, dual-provider (OpenAI→Gemini) classification, patient-folder dedup in Drive, quarantine of low-confidence docs, and real unit tests — the best-tested piece across all repos.
- **Dashboard** (`server.py` :8000, stdlib-only + one 9,400-line `dashboard.html`): in daily office use. Manager Core review scan (missing info, duplicate patients, revenue risk), fax digest + classification (RingCentral), task/follow-up creation.
- **`referral_bot.py`**: shadow mode only — classifies and reconciles against the schedule but deliberately never sends or books (waiting on an "executor seam").
- **RPA**: WEBeDoctor + SIS agents (incl. SMS-2FA human-in-the-loop login), schedule scraper.
- **House rules** (enforced via `.claude/` skills): honest-data (never fabricate a field; `—`/`[FILL IN]`), PHI never in the repo, secrets only in `.env`.

---

## Cross-cutting reality check

**You own three of everything.** Three patient matchers, three approval flows, three audit logs, three RingCentral clients, three EMR-access patterns (Playwright-with-API, raw HTML scraping, Chrome-extension session capture), multiple LLM classifiers. The "puzzle pieces" plan requires picking ONE canonical piece per function and deprecating the rest.

**Best-of-breed picks (initial recommendation):**
| Function | Canonical piece | From |
|---|---|---|
| Approval/draft state machine | `outbound_drafts.py` (comms) + `app/approvals.py` (EMR writes) | ai-phone-intake / n8n-office |
| Patient matching | `identity_graph.py` | ai-phone-intake |
| Audit log | hash-chained `audit_log.py` | n8n-office |
| SIS reads | `sis_client.py` | n8n-office |
| Svigg reads/writes | `svigg_scraper.py` + merged `svigg_reliability_fix.py` | n8n-office |
| Referral email intake | `intake_gs/Code.gs` v2 pipeline | antigravity |
| Inbox triage | `request_triage.py` / `email_request_triage.py` (deterministic) + LLM classifiers | n8n-office / antigravity |
| Records consent gate | `release_terms.py` | ai-phone-intake |
| Voice | Vapi + `emma_brain.py` | ai-phone-intake |

**Spec vs reality — the big divergences:**
1. The mega-prompt calls for **Next.js + NestJS + Postgres + Temporal + TypeScript Playwright bridge**. Everything working today is **Python + Playwright(Python) + SQLite + vanilla HTML**. A TS rewrite discards live-verified EMR code.
2. The spec demands **post-action verification inside the EMR**. Today, Svigg booking explicitly cannot auto-verify ("submitted" + manual-verify warning is by design); cancel verification exists but has the D1/D4 gaps.
3. **Temporal** solves reliable long-running workflow execution — but nothing today runs Temporal, and the approval queue + scheduler cover much of the need at current scale.
4. The formal **Action Registry with per-action contracts** doesn't exist as data; its behavior is hardcoded in the tools/gates. Making it explicit is real, valuable, and cheap.

**Immediate risks (independent of any build plan):**
1. 🔴 `ai-phone-intake` has **no GitHub remote** — the zip + one Mac are the only copies of a production Azure app.
2. 🔴 The backup folder in Downloads contains **live secrets** (per its own README). Keys should be rotated and moved to a proper secrets store.
3. 🟠 `svigg_reliability_fix.py` unmerged — the "cancel said ok but appointment still there" class of bug remains possible.
4. 🟠 PHI databases (aiops.db, app.db, patient_database.json) exist only on one Mac with no encrypted backup.
5. 🟡 Stale MCP README (claims read-only; isn't). P1 stale-module-cache and P2 browser-death issues are known and open.

---

## Where GitHub stands today
- Account `Shubh2300`, repo `shubh2300/shubh2300`, one real branch (`claude/intelligent-goldberg-3z3e6m`) = n8n-office + antigravity import (secret-scanned, PHI-free). No default `main` branch, no README at root until now, no other repos.
- Cursor and Claude Code both operate off this same repo — "GitHub as source of truth" starts with getting **all** source (especially ai-phone-intake) into it, structured deliberately (monorepo vs. multi-repo is an open decision).

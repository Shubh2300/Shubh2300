# Antigravity — Guide for ANY AI Agent (Codex, Gemini, Claude, …)

This project is worked on by multiple AIs in rotation. Read this before
touching anything. Claude-specific extras live in CLAUDE.md (same map, plus
Claude subagent config) — this file is the tool-agnostic version.

## What this is

Clinical operations dashboard for Atlantic Pain & Wellness Institute (pain
management + surgical center). Single-page vanilla JS frontend
(`dashboard.html`) + Python-stdlib API (`server.py`, port 8000, watchdog
auto-restarts it — reload code with `lsof -ti :8000 | xargs kill; sleep 3`).

## Non-negotiable house rules

1. **Honest data.** Missing data renders "—" or "[FILL IN]" — NEVER an
   invented insurer, attorney, DOB, phone, dollar amount, status, or success
   message. Demos must be labeled demos and never write to real data files.
   APIs never claim success for work that didn't happen. (A June 2026 audit
   removed ~60 fabrications; do not reintroduce the pattern.)
2. **PHI stays out of the repo.** All patient data lives in
   `~/.gemini/antigravity/scratch/` (patient_database.json, patients_data.js,
   followups.json, team_tasks.json, staff.json, billing_ledger.json,
   calls.json). NEVER embed patient data in dashboard.html or any tracked
   file. The frontend loads data from `/patients_data.js` (served from
   scratch); regenerate that file with `extract_patients_data.py`.
3. **Secrets only in `.env`** (not included in backups). `service_account.json`
   stays untracked.
4. **Token/context economy.** Do not read dashboard.html (~11k lines) or any
   log fully — grep for anchors, read small ranges. Verify with
   `bash scripts/verify.sh` (all py syntax + all inline JS + 4 API smokes)
   instead of re-reading files.

## Map

- `server.py` — all API routes as `elif path == "..."` chains in do_GET/do_POST.
  Key GETs: /api/patients, /api/tasks, /api/staff, /api/followups,
  /api/billing (incl. live cptStats), /api/reconciliation (discrepancy scan),
  /api/monday-meeting, /patients_data.js.
- `dashboard.html` — grep anchors: `renderCompletedPatients`, `matchesFilters`,
  `renderRoleTaskCard`, `loadFollowups`, `inspectPatient`,
  `updateCPTAnalytics`, `runReconciliationScan`, `loadStaffRoster`.
- Apps Script intake: `Code.js` / `Config.js` / `EmailTemplates.js`.
- Pipelines: `ar_export_agent.py` → `import_ar_reports.py` → `morning_sync.sh`;
  `ringcentral_sync.py` / `ringcentral_fax_sync.py`; portal RPA in
  `sis_agent.py` / `webedoctor_agent.py` / `phone_intake_sync.py`.
- Automation architecture: `CLINICAL_AUTOMATION_LOOPS.md`.
- Koko AI assistant: `gpt_client.py` (Gemini key or any OpenAI-compatible
  `GPT_ENDPOINT`, e.g. local freellmapi on :3001).

## Design language (June 2026 redesign)

"Clinical OS": light theme, white bordered boxes for every section, dense
tables, Apple-quality typography (system font stack), ONE restrained accent
(clinical green), semantic status chips (tinted bg + dark text + 1px border).
No glassmorphism, no backdrop-filter, no glow, no neon, no dark theme. Design
tokens are CSS variables in dashboard.html's `:root` — change tokens, not
scattered literals.

## When you finish a change

Run `bash scripts/verify.sh`; it must end VERIFY OK. Keep diffs scoped; do not
reformat untouched code.

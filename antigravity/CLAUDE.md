# Antigravity — Claude Operating Guide

Clinical ops dashboard for Atlantic Pain & Wellness Institute (pain mgmt +
surgical center). Local-only today; commercial ambitions. Patient data = PHI.

## Operating protocol (token discipline — follow strictly)

The expensive model (Fable/Opus) plans, decomposes, and reviews. Cheap models
type. This is policy, not preference:

1. **Delegate all code edits** to the `implementer` subagent (Sonnet) via the
   Agent tool. Give it exact files, anchors, and desired behavior. Mechanical
   chores (log rotation, running scripts, bulk exact-string replace, smoke
   tests) → the `mechanic` subagent (Haiku). The planning model hand-edits only
   trivial one-liners or after a delegated attempt failed once.
2. **Never use the Workflow tool** unless the user explicitly asks for an
   audit/sweep (one past sweep cost ~415k subagent tokens).
3. **Never read dashboard.html or any log file whole.** Use `grep -n` anchors
   + Read with offset/limit. Never grep without `| head`. webedoctor_rpa.log
   can be huge — `tail` only.
4. **Verification is scripted, not re-read:** `bash scripts/verify.sh` checks
   all Python syntax, all inline JS, patients_data.js, and 4 API smokes.
   Review the script's output, not the files.
5. Use this file's map below instead of re-exploring the codebase.

## File map

| File | What it is |
|---|---|
| `server.py` | Python-stdlib API on :8000. Routes = `elif path == "..."` chains in `do_GET` / `do_POST`. Watchdog auto-restarts; reload code via `lsof -ti :8000 \| xargs kill; sleep 3` |
| `dashboard.html` | Single-page vanilla JS frontend served at `/`. Patient data loads from `/patients_data.js` (served out of scratch) into `window.EXCEL_PATIENTS` / `window.PATIENT_DETAILS`. **Do NOT re-embed data here.** |
| `extract_patients_data.py` | Regenerates `scratch/patients_data.js` from `patient_database.json`. Run after bulk data imports. |
| `Code.js` / `Config.js` / `EmailTemplates.js` | Google Apps Script referral-email intake (Gmail→Gemini→Drive/Sheets). Lives in Apps Script; repo copies are source of truth for edits. |
| `ar_export_agent.py` → `import_ar_reports.py` → `morning_sync.sh` | AR-report pipeline: RPA-export from SIS/Svigg portals, header-autodetect import into patient billing, then reconciliation scan. |
| `ringcentral_sync.py` / `ringcentral_fax_sync.py` | RingCentral call-log + fax polling (JWT creds in `.env`; see RINGCENTRAL_SETUP.md). |
| `sis_agent.py`, `sis_capture.py`, `webedoctor_agent.py`, `phone_intake_sync.py` | Portal RPA/scrapers (Playwright). SIS session state: `scratch/sis_browser_state.json`. |
| `gpt_client.py` | Koko AI client (Gemini or any OpenAI-compatible `GPT_ENDPOINT`, e.g. local freellmapi on :3001 via `start_freellmapi.sh`). |
| `scripts/verify.sh` | The one verification command. |
| `.claude/agents/` | `implementer` (sonnet) + `mechanic` (haiku) subagents. |

**Data (PHI — all OUTSIDE the repo in `~/.gemini/antigravity/scratch/`):**
`patient_database.json` (~3.9k patients), `patients_data.js` (frontend copy),
`team_tasks.json`, `followups.json`, `staff.json`, `billing_ledger.json`
(Sheets-tab dump), `calls.json`, `ar_reports/`. Timestamped `*.backup-*.json`
backups sit alongside. Never write patient data inside the repo.

## API endpoints (server.py)

GET: `/api/health`, `/api/patients` (q/page/limit), `/api/tasks` (role/all),
`/api/staff`, `/api/followups` (assignee), `/api/billing` (incl. live
`cptStats`), `/api/reconciliation` (discrepancy scan), `/api/monday-meeting`,
`/api/upcoming-schedule`, `/api/patient-resource`, `/api/notes`, `/api/logs`,
`/api/archives`, `/api/koko/status`, `/api/agents/health`, `/api/sis/status`,
`/api/webedoctor/crawl`, `/patients_data.js`.
POST: `/api/staff`, `/api/followups/{generate,add,complete,update}`,
`/api/tasks/{add,update}`, `/api/notes`, `/api/koko`, `/api/update-status`,
`/api/patients/{update,delete}`, `/api/sync-patient`, `/api/sync-note`,
`/api/monday-meeting/log`, `/api/run-agent`, `/api/sis/2fa`, more in do_POST.

## dashboard.html anchors (grep these, don't scroll)

- Main table render: `for (let i = 0; i < activeCount; i++)` (active rows),
  `visibleHistorical.forEach` (seen rows), `matchesFilters` (filter logic),
  `populateAttorneyFilter` (lawyer dropdown).
- Completed Records tab: `renderCompletedPatients`.
- Follow-ups: `loadFollowups`, `getFollowUpList`, `syncFollowupCompletion`,
  `renderNurseHub`, `allocateWorkload`.
- Tasks: `renderRoleTaskCard`, `renderRoleTaskHub`, `updateTaskField`,
  `loadStaffRoster`, `getWorkingAs` ("Working as…" header selector).
- Billing modal: `updateCPTAnalytics`, `runReconciliationScan`.
- Drawer: `inspectPatient`, billing card near `billingDataMissing`.
- Demo (write-free, labeled): `runClinicalSimulation`.

## House rules (honest data — enforced June 2026, keep it that way)

- Missing data renders **"—"** or **"[FILL IN]"** — never an invented insurer,
  attorney, DOB, phone, amount, or status. Status may be `""` → "— Not Set".
- Demos are labeled demos and never write to real data files.
- APIs never claim success for work that didn't happen (see `/api/sync-note`'s
  `LOCAL_ONLY` pattern).
- No fabricated KPIs/counters; everything on screen traces to a real record.
- Secrets in `.env` only. `service_account.json` stays untracked.

## Usage habits (for the human too)

- One task per session; start a fresh session instead of continuing a marathon
  — long sessions re-pay their own history on every message.
- Scope requests ("change X in function Y") instead of "look around".
- `/model sonnet` for routine edit days; Fable/Opus for planning days.
- Ultracode / multi-agent sweeps only when explicitly requested.

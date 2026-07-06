# N8N Office Build — Overnight Handoff (2026-06-28)

## TL;DR

n8n itself is **actually running** (PID 886, listening on `localhost:5678`, editor accessible at `http://localhost:5678`) with a freshly-migrated SQLite store at `data/n8n.sqlite`. Four flows are scaffolded end-to-end as paired artifacts — a Python module under `python/flows/` (FastAPI + CLI + offline smoke test) and a matching `n8n-workflows/*.json` graph — for **triage-router**, **patient-lookup**, **file-request**, and **appointment**. All four smoke tests pass green right now with zero external creds, because every PHI/EMR call is gated behind env-var presence checks and the writes are `NotImplementedError` per Q5. What's blocked is real traffic: no Anthropic key, no Google service-account JSON, no RingCentral webhook URL pointed at us, and no n8n workflows imported into the running instance yet. **Tomorrow's first move is to import the four JSON files into the n8n editor and wire the four env vars in `.env`** — after that the triage front door is one webhook subscription away from live.

## What runs RIGHT NOW (verified)

| Thing | How to run | What you'll see |
|---|---|---|
| **n8n editor** (already running) | Open `http://localhost:5678` | Login screen; PID 886, started via `start_n8n.sh`, logs at `logs/n8n.log` |
| **Triage router smoke test** | `python3 /Users/shubh/n8n-office/python/flows/triage-router.py` | 8 cases pass: 4 intents end-to-end, RC envelope extraction, JSON-fence stripping, verification short-circuit, unknown-label coercion. Exits 0. |
| **Patient lookup smoke test** | `python3 /Users/shubh/n8n-office/python/flows/patient-lookup.py` | 10 routing/merge assertions pass (score, dedup, ambiguous, error rollup). Exits 0. |
| **File-request smoke test** | `python3 /Users/shubh/n8n-office/python/flows/file-request.py` | 6 stages pass: pick → HTML gate → SMS dispatch → re-decide idempotency → reject path → empty-payload reject. Exits 0. |
| **Appointment smoke test** | `python3 /Users/shubh/n8n-office/python/flows/appointment.py` | 9 checks pass: validation, dry-run shape, fail-closed patient lookup, conflict detection, clinic-identity copy. Exits 0. |
| **Brain HTML viz** | `open /Users/shubh/brain/viz/brain.html` | Three.js neural-network viz; 5 neurons rendered from real wiki files, click a neuron for the side panel. First load needs internet (unpkg). |
| **Brain TUI** | `python3 /Users/shubh/brain/viz/brain_tui.py` (Ctrl-C to quit) | Rich-based 3-panel TUI: category counts, pulsing brain frames, scrolling synapse fires from real `[[wikilinks]]`. 3 neurons in the vault. |
| **Integrations import probe** | `cd /Users/shubh/n8n-office && python3 -c "import sys; sys.path.insert(0,'python'); from integrations import audit_log, emr_bridge, ringcentral_adapter; print('OK')"` | Prints `imports OK`. Confirms the three shared adapters load with no missing-dep errors. |
| **n8n process check** | `lsof -i :5678` and `ps -p $(cat /Users/shubh/n8n-office/n8n.pid)` | Shows the node process on port 5678. |

## What's scaffolded (needs creds to wire up)

### `python/flows/triage-router.py`  (paired: `n8n-workflows/triage-router.json`)
Front door: RingCentral SMS webhook → signature verify → Claude Haiku classifier → per-intent dispatcher → audit row. Always returns 200 fast (per Q10).
- `ANTHROPIC_API_KEY` — Anthropic console (https://console.anthropic.com/) → API Keys. **Request a BAA before pointing at real PHI (Q11).**
- `RC_WEBHOOK_VERIFICATION_TOKEN` — RingCentral Developer Portal → your app → Webhooks/Notifications → "Validation Token" field for the subscription.
- `AUDIT_LOG_PEPPER` — generate locally: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. **Back this up to the password manager alongside `.env` and `data/n8n.sqlite` (Q6) — losing the pepper breaks the hash chain.**

### `python/flows/patient-lookup.py`  (paired: `n8n-workflows/patient-lookup.json`)
Parallel fan-out across SIS / Svigg / Drive via `emr_bridge`, weighted-confidence merge, ambiguous → Q4 SMS gate, error → Q10 dead-letter. Read-only by construction.
- `AUDIT_LOG_PEPPER` — same as above (shared).
- `ANTHROPIC_API_KEY` — same as above (shared).
- `GOOGLE_SERVICE_ACCOUNT_JSON` — path to a service-account JSON with Drive read scope (see Antigravity's `service_account.json` pattern; create a new key in Google Cloud Console → IAM → Service Accounts → Keys).
- `SIS_USERNAME` / `SIS_PASSWORD` — Atlantic SIS portal creds (already in Antigravity's `.env`; reuse via `importlib` per Q6).
- `SVIGG_USERNAME` / `SVIGG_PASSWORD` — Svigg billing portal creds (same source).
- `RINGCENTRAL_JWT` — RingCentral app credentials block (same JWT Antigravity's `ringcentral_sync.py` uses).

### `python/flows/file-request.py`  (paired: `n8n-workflows/file-request.json`)
Drive lookup → deterministic best-file pick → staff HTML approval gate at `/approval/{token}` → on APPROVE, SMS the share link via RingCentral; idempotent on re-decide.
- `GOOGLE_SERVICE_ACCOUNT_JSON` — same source as above.
- `DRIVE_PATIENT_ROOT_FOLDER_ID` — copy from the URL of the "Patients" root folder in Google Drive (the long ID after `/folders/`).
- `AUDIT_PEPPER` — same generated value as `AUDIT_LOG_PEPPER` above (note the variable name differs; file-request reads `AUDIT_PEPPER`).
- `STAFF_APPROVAL_BASE_URL` — the public URL where this FastAPI surface will live (for v1 = `http://localhost:8001` is fine; later, the tunneled URL when going off-LAN).

### `python/flows/appointment.py`  (paired: `n8n-workflows/appointment.json`)
**Last to wire** (depends on Q5 sign-off for SIS writes). Stages a `STAGED` audit row + fires Q4 staff SMS with YES/NO/EDIT prompts; the actual `emr_bridge.book_appointment()` call stays at `NotImplementedError` until per-op sign-off.
- `AUDIT_LOG_PEPPER` — same as above.
- `APPROVAL_STAFF_NUMBER` — designated on-call staff RingCentral DID, E.164 format (e.g. `+12155550100`).
- `CLINIC_SMS_FROM_NUMBER` — the clinic's RingCentral DID for outbound SMS.
- `CLINIC_MAIN_LINE` — the clinic's main voice number printed in patient-facing copy.

### Shared integrations (`python/integrations/`)
Already in place and imported by all four flows:
- `audit_log.py` — SHA-256 hash-chained SQLite append-only log (Q7). Writes to `cache/audit.log` SQLite file. Has `verify_chain()` for monthly integrity checks.
- `emr_bridge.py` — cache-fronted lookup adapter across SIS/Svigg/Drive (reads merged `patient_database.json` from Antigravity). Writes raise `NotImplementedError` (Q5).
- `ringcentral_adapter.py` — wraps Antigravity's `get_access_token()` via `importlib` (Q6 single-source); webhook signature verify + send_sms + validation handshake.

## Locked design decisions

- **Q2**: Patient Lookup first → File Request second → Appointment last — multi-source lookup is the substrate every other flow needs, and the `emr_bridge` adapter is already smoke-tested.
- **Q3**: LLM triage via Claude Haiku, Anthropic API direct — rules will fail on multi-intent; freellmapi is dev-only; Hermes parked; Anthropic offers a BAA.
- **Q4**: Staff approval gate = SMS DM to a designated RingCentral number — reuses RC, keeps staff in one channel, mobile-first.
- **Q5**: EMR is read-only for v1; writes are `NotImplementedError` stubs that unlock per-op behind written sign-off — a misclick in RPA writes has no atomic undo.
- **Q6**: Credentials in `.env` chmod 0600 on FileVault; reuse Antigravity adapters via `importlib`; skip macOS Keychain — Keychain's friction beats its marginal benefit over FileVault + 0600.
- **Q7**: Audit log = separate SQLite with SHA-256 hash chain (`audit_log.py`) — n8n's built-in log is operational telemetry, not a HIPAA audit trail.
- **Q8**: Integrate, don't replace — Antigravity stays the staff UI; Koko stays separate (different surface); `intake_gs` stays for v1 (works today, just shipped commit c33d52a).
- **Q9**: Clinic identity only, no named persona ("Atlantic Pain & Wellness") — named personas raise empathy expectations the bot can't deliver on hard topics.
- **Q10**: One retry per node, 30s backoff; second failure → dead-letter SMS to on-call + audit row + n8n exec marked failed; Antigravity dashboard gets a "Stuck Flows" card — covers transient blips without inviting infinite loops.
- **Q11**: No BAA for n8n (self-hosted); BAA register required for Anthropic / Google Workspace / RingCentral; FileVault + unique-logins-with-MFA + tamper-evident audit + encrypted backups + annual SRA + written P&P + BAA register covers ~90% of OCR asks for a single-office practice.

## Brain visualizations

Both verified working tonight.

- **HTML (Three.js)**: `/Users/shubh/brain/viz/brain.html` — `open /Users/shubh/brain/viz/brain.html` (5 neurons, hash-seeded firing, click for side panel).
- **TUI (Rich)**: `/Users/shubh/brain/viz/brain_tui.py` — `python3 /Users/shubh/brain/viz/brain_tui.py` (3 neurons, pulsing brain frames, scrolling synapses; Ctrl-C to quit).

Both scan `/Users/shubh/brain/wiki/` live, so adding a new `.md` page with `[[wikilinks]]` auto-materializes on the next run (TUI) or after re-running `build_brain_data.py` and re-inlining (HTML). 19 wikilinks currently reference stub pages that don't exist yet — they're silently dropped from the graph until those pages get created.

## Tomorrow's first 30 minutes (in order)

1. **Confirm n8n is still up.** `lsof -i :5678 | head -3` — if empty, restart with `cd /Users/shubh/n8n-office && ./start_n8n.sh`.
2. **Open the editor.** `open http://localhost:5678` — complete the owner-account setup if prompted (this is a fresh install; first user becomes admin).
3. **Import the four workflow JSON files.** In the n8n editor: Workflows → Import from File → pick each of:
   - `/Users/shubh/n8n-office/n8n-workflows/triage-router.json`
   - `/Users/shubh/n8n-office/n8n-workflows/patient-lookup.json`
   - `/Users/shubh/n8n-office/n8n-workflows/file-request.json`
   - `/Users/shubh/n8n-office/n8n-workflows/appointment.json`
   Leave all four **inactive** for now.
4. **Set per-node retry policy on each HTTP / Execute Command node**: open each node → Settings tab → Retry on Fail = on, Max Tries = 2, Wait Between Tries = 30000ms (Q10).
5. **Wire the minimum-viable env block.** Edit `/Users/shubh/n8n-office/.env` and add:
   ```
   AUDIT_LOG_PEPPER=<paste secrets.token_urlsafe(32) output>
   AUDIT_PEPPER=<same value as above>
   ANTHROPIC_API_KEY=sk-ant-...
   ```
   Then back up `.env` + `data/n8n.sqlite` to the password manager (Q6).
6. **Restart n8n** so the new env loads: `kill $(cat /Users/shubh/n8n-office/n8n.pid) && sleep 2 && cd /Users/shubh/n8n-office && ./start_n8n.sh`.
7. **Smoke-test the patient-lookup flow inside n8n** (read-only, no creds beyond pepper needed): in the editor, open the patient-lookup workflow → Execute Workflow with a manual payload like `{"name":"Dorca Jones","phone":"5551234"}` → confirm it falls through to the Execute Command fallback and prints a result.
8. **Sanity-check the audit chain.** `python3 -c "import sys; sys.path.insert(0,'/Users/shubh/n8n-office/python'); from integrations.audit_log import AuditLog; import os; a = AuditLog(db_path='/Users/shubh/n8n-office/cache/audit.log', pepper=os.environ['AUDIT_LOG_PEPPER']); print(a.verify_chain())"` — should print an "OK"-style result if any rows were written.

After that, the next units of work (in order) are: wire `GOOGLE_SERVICE_ACCOUNT_JSON` + `DRIVE_PATIENT_ROOT_FOLDER_ID` to activate file-request, then point a RingCentral SMS webhook subscription at the triage-router URL.

## What I did NOT do

- **No real EMR integration** — `emr_bridge` reads only from the merged `patient_database.json` that Antigravity's RPA agents populate; no new portal traffic was generated tonight.
- **No live PHI traffic** — every flow ran in dry-run / smoke mode against fixtures, not real patients.
- **No clinic-hardware deployment** — this is your dev machine; the production install on the office workstation is its own task.
- **No n8n workflow activation** — the four JSON files exist on disk but were **not imported** into the running n8n instance and **not activated**. Workflows ship with `active: false`.
- **No Anthropic BAA request** — Q11 says it must be in place before pointing Haiku at real PHI; that's an operator action, not a code action.
- **No real RingCentral SMS sends or webhook subscription.** The adapter is wired and import-tested; nothing has actually hit the RC API tonight.
- **No `Stuck Flows` dashboard card on Antigravity** (Q10) — that's a small `/api/logs`-style polling card on `dashboard.html`, not in scope for the n8n build.
- **No port of `intake_gs` to n8n** — per Q8 it stays in Apps Script for v1.

## Open questions for the morning

1. **Anthropic BAA timing.** Do you want to file the BAA request before any wiring (safest) or in parallel with non-PHI plumbing (faster)? The triage front door can be exercised end-to-end with synthetic test SMS bodies that contain no PHI in the meantime.
2. **Which RingCentral number is the staff approval line (`APPROVAL_STAFF_NUMBER`)?** Needs to be a phone a real human watches during clinic hours. If it's your personal cell, decide whether to add an after-hours fallback now or punt to v2.
3. **Drive folder layout.** `DRIVE_PATIENT_ROOT_FOLDER_ID` assumes one root with per-patient subfolders. Is that the actual layout today, or do files live in mixed locations (some in `Patients/`, some in `Imaging/`, some attached to chart notes)? The file-request picker's deterministic scoring needs the answer to avoid surfacing wrong files.

## Approximate token cost

Rough estimate for the overnight build (planning + subagent dispatch + verification, excluding this handoff):
- Planning / Q&A synthesis (Opus): ~80–120k tokens.
- 4 flow scaffolds via Sonnet implementer subagents (one per flow, each producing ~30–40 KB of Python + JSON + smoke output): ~250–350k tokens.
- 2 brain viz scaffolds (Sonnet): ~60–90k tokens.
- Audit-log + EMR bridge + RC adapter integrations: ~80–120k tokens (already shipped before tonight in some form; tonight added wiring).
- Verification / smoke-test reads: ~10–20k tokens.

**Total: roughly 480–700k tokens.** Cost depends on Opus-vs-Sonnet split; ballpark $4–$8 at current rates if mostly Sonnet, $10–$20 if Opus-heavy. The expensive bit was planning + decision synthesis; the implementations themselves were Sonnet, which is the right ratio per the operating protocol.

# Porting this framework to a new practice / business

This repo is two things stacked together:

1. A **generic, business-agnostic back-office assistant framework** — auth,
   sessions, an audit log, a human-in-the-loop approval queue, an LLM agent
   loop, a comms layer (email + RingCentral), a daily manager report, an
   in-app scheduler, and a single-page UI. None of this knows the word
   "Atlantic".
2. A set of **Atlantic Pain & Wellness adapters** bolted onto that framework —
   the specific EMR tools (SIS + Svigg), the referral-audit flow, and the
   `python/integrations` / `python/flows` scrapers. This is the only part that
   is practice-specific.

To stand the app up for a different business you keep #1 verbatim and swap #2.
This document is the **single source of truth** for what is core vs. adapter,
how to swap the adapters, and every environment variable the app reads.

---

## 1. Core modules (app-agnostic — keep as-is)

These carry no practice-specific logic. A new deployment reuses them unchanged.

| Module | Responsibility |
|---|---|
| `app/config.py` | Loads `.env` (non-destructive), derives `SETTINGS`, picks the PHI-safe LLM provider, parses `GMAIL_ACCOUNTS`, scheduler knobs. The one place env is read. |
| `app/db.py` | SQLite schema + `get_conn()` connection helper (WAL). Tables: audit, approvals, referrals, manager reports, sessions, conversation history, messages. |
| `app/audit.py` | `audit.log(kind=…, outcome=…, …)` append-only event log. Never stores PHI — ids only. |
| `app/auth.py` | Users file (PBKDF2), session issue/verify, offboarding. |
| `app/approvals.py` | The approval **queue mechanics** — create/list/decide/execute a card, status transitions, the `_execute` dispatcher. *(The per-action EMR bodies inside `_execute` are adapters — see §2.)* |
| `app/providers.py` | LLM provider abstraction (Anthropic / OpenAI / none), PHI-safe gating. |
| `app/agent.py` | The **agent loop** — turn management, tool-call plumbing, read/write tool split, on-screen approval routing. *(The tool **registry** `READ_TOOLS`/`WRITE_TOOLS` and `_dispatch_tool` bodies are adapters — see §2.)* |
| `app/comms.py` | Multi-account Gmail poll (IMAP), send (SMTP), RingCentral message-store poll (SMS/fax/voicemail). Generic — no practice logic. |
| `app/manager.py` | The daily manager report generator. |
| `app/scheduler.py` | The in-app asyncio background loop (comms poll + daily manager + daily referral audit). Generic; each job degrades to a no-op when its prerequisites are absent. |
| `app/server.py` | FastAPI app, routes, startup/shutdown wiring (starts/cancels the scheduler), `/api/health`. |
| `app/static/{index.html,app.js,style.css}` | The single-page UI (login, home, chat, approvals, referrals, inbox). Brand strings live here + in `SETTINGS.BRAND`. |

**What "keep as-is" means:** you should not need to edit these to change
*which business* the app serves. You will touch `static/*` only for the brand
string / logo, and `config.py` only if you add a genuinely new env knob.

---

## 2. Atlantic adapters (swap these per business)

Everything practice-specific lives in a small, well-bounded set of seams.

| Seam | File(s) | What it is | How to replace |
|---|---|---|---|
| **Agent tool registry** | `app/agent.py` — `READ_TOOLS` + `WRITE_TOOLS` lists | The JSON tool schemas the LLM sees (e.g. `emr_search`, `patient_360`, `request_booking`). | Rewrite the two lists to describe *your* systems' read/write operations. Keep the read/write split (writes route to approval). |
| **Tool executor** | `app/agent.py` — `_dispatch_tool` | Maps a tool name → the real call into the EMR/back-end. | Replace each branch body with a call into your integration. Keep the return contract `(content, ok, summary)`. |
| **Approval executor branches** | `app/approvals.py` — `_execute` and its `_execute_book` / `_execute_cancel` / `_execute_reschedule` helpers | The code that actually commits an approved write to the EMR. `_execute_send_email` / `_execute_send_sms` are **generic comms** and stay. | Replace the EMR-write helpers (`_execute_book/cancel/reschedule`) with your system's write calls. Do **not** change the gating/approval semantics around them. |
| **Referral audit** | `app/referral_audit.py` | Atlantic's "did every emailed referral get booked?" reconciliation flow. | Keep if you have an equivalent referral pipeline; otherwise disable it (leave Gmail/`EMR_ENABLED` unset — the scheduler skips it silently). |
| **EMR / portal integrations** | `python/integrations/*` (`sis_client.py`, `svigg_scraper.py`, `emr_bridge.py`, `emr_session_manager.py`, `ringcentral_adapter.py`) and `python/flows/*` | The actual scrapers/clients for SIS Complete + Svigg/WEBeDoctor and the n8n-style flows. | Swap for your back-end's client library or REST calls. `ringcentral_adapter.py` is reusable if you use RingCentral. |
| **Brand string** | `SETTINGS.BRAND` in `app/config.py`; the two brand blocks + `<title>` in `app/static/index.html` | Display name shown in the shell and login card. | Change the string(s). |

> **Never touch, in any port:** the EMR write **gates** (allow-lists, execute
> kill-switches in `svigg_scraper.py`), the auth/session code, and the approval
> **execution semantics** (a write only commits after on-screen human approval;
> a reschedule never books the new slot until the original cancel is *verified*
> gone). Swap the *bodies* that talk to your EMR, not the *choke points* around
> them.

---

## 3. Porting recipe (step by step)

1. **Clone the repo, keep §1 modules untouched.**
2. **Swap the tool registry.** In `app/agent.py`, rewrite `READ_TOOLS` and
   `WRITE_TOOLS` so the schemas describe your systems' operations. Keep read
   tools read-only and put every mutating operation in `WRITE_TOOLS` so it
   routes through approval.
3. **Swap the tool executor.** In `app/agent.py`, replace each `_dispatch_tool`
   branch body to call your integration. Preserve the `(content, ok, summary)`
   return contract and the "no PHI in `summary`/logs" rule.
4. **Swap the approval write executors.** In `app/approvals.py`, replace the
   `_execute_book` / `_execute_cancel` / `_execute_reschedule` bodies. Leave
   `_execute_send_email` / `_execute_send_sms` alone (they are generic comms).
   Do **not** alter the surrounding gating or status transitions.
5. **Point comms at your mailboxes / phone system.** Set `GMAIL_ACCOUNTS`
   (and/or the legacy `GMAIL_ADDRESS`+`GMAIL_APP_PASSWORD`) and the `RC_*`
   RingCentral vars. Nothing in `comms.py` needs editing.
6. **Decide on referral audit.** Keep `app/referral_audit.py` if you have a
   referral pipeline; otherwise leave Gmail or `EMR_ENABLED` unset and the
   scheduler's daily audit job skips itself.
7. **Rebrand.** Change `SETTINGS.BRAND` in `config.py` and the brand strings /
   `<title>` in `app/static/index.html`. Optionally adjust palette in
   `style.css`.
8. **Set `.env`.** Fill the env table in §4 for your deployment (LLM key,
   `EMR_ENABLED`, comms creds, scheduler cadence). Start with `EMR_ENABLED=0`.
9. **Verify.** Run `python3 verify_app.py` (offline, `EMR_ENABLED=0`,
   `APP_SCHEDULER=0`, restores `app/data/` byte-for-byte). It must end
   `VERIFY_APP PASS N/N`.
10. **Deploy.** Follow `README-DEPLOY.md` (launchd + TLS reverse proxy).

---

## 4. Environment reference (every env the app reads)

All are read (indirectly) through `app/config.py`, which loads this repo's own
`.env` non-destructively (a value already in the real environment always wins).
Put secrets in `.env`, never in the launchd plist or git.

### 4.1 LLM provider (pick one; PHI-first auto-select `anthropic → openai → none`)

| Env | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | _(unset)_ | Selects provider `anthropic` (PHI-safe under BAA). |
| `OPENAI_API_KEY` | _(unset)_ | Selects provider `openai` (PHI-safe under BAA). |
| `CHAT_PROVIDER` | _(auto)_ | Force a provider. `openai_compat` is **non-PHI-safe** → chat is refused entirely. |
| `GPT_ENDPOINT` / `GPT_API_KEY` / `GPT_MODEL` | _(unset)_ | Only used when `CHAT_PROVIDER=openai_compat` is explicitly set. A stray `GPT_ENDPOINT` does **not** auto-select a provider. |
| `CHAT_MODEL` | provider default | Override chat model. |
| `MANAGER_MODEL` | = `CHAT_MODEL` | Override the manager-report model. |

### 4.2 EMR master switch

| Env | Default | Effect |
|---|---|---|
| `EMR_ENABLED` | `1` | `0/false/no` disables ALL EMR reads/writes (reads error, approved writes fail closed). Recommended `0` until verified. |

### 4.3 Server / auth / cookies

| Env | Default | Effect |
|---|---|---|
| `APP_PORT` | `8787` | Listen port. |
| `APP_HOST` | `127.0.0.1` | Bind address — keep loopback, put TLS in front. |
| `APP_ENV` | `dev` | `prod` marks the session cookie **Secure** (requires HTTPS). |
| `APPROVER_ROLES` | `manager,admin` | Comma-separated roles allowed to approve/execute writes and run the manager. |

### 4.4 Comms — Gmail (multi-account)

| Env | Default | Effect |
|---|---|---|
| `GMAIL_ACCOUNTS` | _(unset)_ | Comma-separated `address:app_password` pairs — the multi-mailbox source of truth. Every account is polled; each stored message's recipient is that account address. The **first** account is the default sender. |
| `GMAIL_ADDRESS` | _(unset)_ | **Back-compat.** Treated as one account (deduped into `GMAIL_ACCOUNTS`). If set, names the default sender. |
| `GMAIL_APP_PASSWORD` | _(unset)_ | App password paired with `GMAIL_ADDRESS`. |

> Zero accounts configured → email poll reports `not configured` (no crash).
> A per-account failure does not stop the other mailboxes; the poll result
> surfaces each account's count (or `error: <class>`) under `email_accounts`.

### 4.5 Comms — RingCentral (SMS / fax / voicemail)

| Env | Default | Effect |
|---|---|---|
| `RC_SERVER_URL` | `https://platform.ringcentral.com` | RingCentral platform base URL. |
| `RC_CLIENT_ID` | _(unset)_ | App client id. |
| `RC_CLIENT_SECRET` | _(unset)_ | App client secret. |
| `RC_JWT` | _(unset)_ | JWT credential. Empty → RC poll reports `not configured`. |
| `RC_FROM_NUMBER` | _(unset)_ | Sending number for outbound SMS. |

> RC ingest covers **all inbound** message types from the message store —
> SMS (body = text), fax (body = page-count summary + attachment metadata in
> `detail`, never downloaded), and voicemail (body = duration summary +
> attachment metadata). Deduped by RingCentral id. The Inbox UI channel filter
> exposes `email` / `sms` / `fax` / `voicemail`.

### 4.6 In-app scheduler (`app/scheduler.py`)

| Env | Default | Effect |
|---|---|---|
| `APP_SCHEDULER` | `1` | `0` disables the whole background loop (used by `verify_app.py`). |
| `COMMS_POLL_MINUTES` | `5` | Comms-poll cadence in minutes. `0` disables just the periodic poll job. |
| `MANAGER_RUN_HOUR` | `2` | Local-time hour (0–23) for the once-daily manager report. |
| `AUDIT_RUN_HOUR` | `7` | Local-time hour (0–23) for the once-daily referral audit (only when `EMR_ENABLED` and Gmail configured; else skipped silently). |

### 4.7 Referral Sheet intake (optional)

| Env | Default | Effect |
|---|---|---|
| `REFERRALS_SHEET_ID` | _(unset)_ | Google Sheet polled for referrals. Empty / missing service account / missing `googleapiclient` → `not configured`, no crash. |
| `SERVICE_ACCOUNT_JSON` | `…/Antigravity/service_account.json` | Service-account credentials path for the sheet. |

### 4.8 EMR write allow-lists — GO-LIVE ONLY (Atlantic/Svigg adapter)

These live in the `svigg_scraper.py` adapter and are **fail-closed** by
default (only the designated test account is writable). See the ⚠️ warnings in
`README-DEPLOY.md` §4.7 before widening. A different back-end's writes will
have their own gates — keep an equivalent fail-closed default.

| Env | Format | Effect |
|---|---|---|
| `SVIGG_BOOKING_ALLOWLIST` | `acct,acct,…` or `*` | Accounts allowed to be booked. `*` removes the per-account guard. |
| `SVIGG_CANCEL_ALLOWLIST` | `acct:nametoken,…` or `*` | acct→required-last-name-token bindings for cancellation. `*` drops the name binding. |
| `SVIGG_BOOKING_EXECUTE` | `1/true/yes` | Separate kill-switch that must also be truthy before any booking commit. |

> `python/integrations/*` and `python/flows/*` adapters read additional
> practice-specific env (`SIS_*`, `WEBEDOCTOR_*`, `RC_WEBHOOK_VERIFICATION_TOKEN`,
> `AUDIT_*`, `DRIVE_*`, `TRIAGE_*`, `PATIENT_LOOKUP_*`, `ANTIGRAVITY_DIR`). Those
> are Atlantic-adapter env and are replaced wholesale when you swap the
> integrations — they are not part of the core framework contract.

---

## 5. Quick sanity checklist for a new deployment

- [ ] `READ_TOOLS`/`WRITE_TOOLS` rewritten for the new back-end; writes route to approval.
- [ ] `_dispatch_tool` branches call the new integration; return `(content, ok, summary)`.
- [ ] `_execute_book/cancel/reschedule` swapped; gating + approval semantics untouched.
- [ ] Brand string changed in `config.py` + `static/index.html`.
- [ ] `.env` filled from §4; started with `EMR_ENABLED=0`.
- [ ] `python3 verify_app.py` ends `VERIFY_APP PASS N/N`.
- [ ] launchd + TLS proxy configured per `README-DEPLOY.md`.

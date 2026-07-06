# Deploying the Atlantic Back-Office Assistant

Internal web app for **Atlantic Pain & Wellness · Head Injury Institute**
(Bala Cynwyd office). Chat assistant + human-approved EMR writes + referral
intake + a manager report. **It handles PHI.** Keep it on the practice's own
machine / private network. This is not a public SaaS.

- Repo root: `/Users/shubh/n8n-office`
- Entry point: `python3 -m app.server`
- Data (sqlite db, users, referrals, logs): `app/data/` — **never commit it.**

> **Reusing this app for another practice/business?** The framework is
> business-agnostic; only the EMR tool registry, approval executors, referral
> audit, and `python/integrations` are Atlantic-specific. The **single source
> of truth** for what is core vs. adapter, the step-by-step porting recipe, and
> the **complete environment-variable reference** is
> [`FRAMEWORK-PORTING.md`](FRAMEWORK-PORTING.md). This deploy guide covers
> running *this* Atlantic deployment; that doc covers standing up a new one.

---

## 1. Prerequisites

- Python **3.14** with FastAPI + uvicorn installed (see `requirements-app.txt`).
- Confirm the interpreter you will deploy with actually has the deps:

  ```bash
  /usr/local/bin/python3 -c "import fastapi, uvicorn, sqlite3; print('deps ok')"
  ```

  If that path is wrong for your machine, find the right one
  (`which python3`) and use it everywhere below and in the launchd plist.

Install deps (once):

```bash
cd /Users/shubh/n8n-office
python3 -m pip install -r requirements-app.txt
```

---

## 2. Run it locally (foreground)

```bash
cd /Users/shubh/n8n-office
python3 -m app.server            # binds 127.0.0.1:8787 by default
# or override:
python3 -m app.server --host 127.0.0.1 --port 8787
```

Open <http://127.0.0.1:8787/>. Health check (no auth required):

```bash
curl -s http://127.0.0.1:8787/api/health
# -> {"ok": true, "emr_enabled": ..., "provider": "...", "phi_safe": ...}
```

The database, users file, and data dirs are created automatically under
`app/data/` on first start.

---

## 3. Create login users

Every `/api/*` route except `/api/health` and `/api/login` requires a session,
so create at least one user before anyone can log in. This prompts for the
password twice (it is never passed on the command line):

```bash
cd /Users/shubh/n8n-office
python3 -m app.server --create-user alice --role staff
# password for alice: ********
# confirm password:   ********
```

Roles are `staff`, `manager`, or `admin`; `staff` is the default. **Approving
EMR writes and running the manager require an approver role** (`manager` or
`admin` by default — see `APPROVER_ROLES` in §4.3). A plain `staff` account can
queue change cards via chat but cannot approve them, so the human-in-the-loop
choke point stays a genuine second party. Create at least one approver station
user:

```bash
python3 -m app.server --create-user station --role manager
```

Passwords are stored as PBKDF2-HMAC-SHA256 (200k iterations, per-user salt) in
`app/data/users.json` — **that file is a secret; back it up, don't commit it.**

**Offboarding — revoke access immediately.** Removing a user must also kill
their live sessions (otherwise a valid cookie keeps working for up to the 12h
TTL). Use the CLI flag, which deletes the user AND purges their session rows:

```bash
python3 -m app.server --delete-user alice
```

(Session lookups also re-check `users.json` on every request, so a user
removed by any means loses access on their next call.)

---

## 4. Configuration (`.env`)

`app/config.py` loads, on import, `KEY=VALUE` lines from **this app's own**
`/Users/shubh/n8n-office/.env` only, **without** overwriting anything already
in the real environment (real env wins). Put secrets in `.env` — **never** in
the launchd plist (which is world-readable) or in git.

> The shared `/Users/shubh/Documents/Antigravity/.env` is **not** read here.
> The EMR scrapers load their own portal creds (SIS/WEBeDoctor) from it
> themselves at connect time; pulling it into this process would leak those
> portal passwords into this app's environment and — because it defines a
> `GPT_ENDPOINT`/`GPT_API_KEY` — could hand this app a **non-PHI-safe** LLM it
> never chose. Keep this app's LLM + any secrets it needs in **its own** `.env`.

### 4.1 LLM provider (pick ONE; leave the rest unset)

Provider selection is **PHI-first** and never silently picks a non-PHI-safe
backend. Auto-selection order is `anthropic` → `openai` → `none`. If none is
set, the app still runs — chat just replies that no LLM is configured (HTTP
200, no crash).

| Env key | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | provider = `anthropic`, **PHI-safe** (BAA cloud). Default model `claude-haiku-4-5-20251001`. |
| `OPENAI_API_KEY` | provider = `openai`, **PHI-safe** (BAA cloud). Default model `gpt-4o-mini`. |
| _(none set)_ | provider = `none`. Chat explains no LLM is configured. |

Optional overrides: `CHAT_MODEL`, `MANAGER_MODEL`.

> **A stray `GPT_ENDPOINT` in the environment does NOT select a provider.** The
> non-PHI-safe local `openai_compat` proxy is used **only** when you EXPLICITLY
> set `CHAT_PROVIDER=openai_compat` (plus `GPT_ENDPOINT`/`GPT_API_KEY`). Even
> then, because this is a PHI tool, **chat is refused entirely** on that
> provider — `run_chat` returns a static "not PHI-safe" reply and sends nothing
> you type (or any stored history) to the proxy. There is deliberately no
> "type patient data into a free model" path.

> **PHI rule:** only `anthropic` and `openai` are treated as PHI-safe. On any
> other provider the app disables EMR tools, refuses chat, and keeps patient
> data out of the prompt. Do **not** try to defeat this to use a free proxy on
> real patients.

### 4.2 EMR master switch

| Env key | Default | Effect |
|---|---|---|
| `EMR_ENABLED` | `1` | `1/true/yes` = EMR reads/writes allowed. Set `0` to disable ALL EMR access (reads return errors, approved writes fail with `EMR disabled`). Recommended `0` until the deployment is verified. |

### 4.3 Server / cookies

| Env key | Default | Effect |
|---|---|---|
| `APP_PORT` | `8787` | listen port |
| `APP_HOST` | `127.0.0.1` | bind address — **keep it loopback** and put TLS in front (§6) |
| `APP_ENV` | `dev` | `prod` marks the session cookie **Secure** → the app MUST be served over HTTPS or logins silently break. Use `prod` only behind a TLS proxy. |
| `APPROVER_ROLES` | `manager,admin` | comma-separated roles allowed to approve/execute EMR writes and run the manager. A `staff` account can queue cards but gets `403` on the decision/manager endpoints. |

> **Rate limiting behind the proxy:** the login limiter keys on the first
> `X-Forwarded-For` hop (falling back to `client.host`), so per-client
> accounting survives the loopback TLS proxy in §6 — make sure the proxy sets
> `X-Forwarded-For` (the sample nginx config does). Only **failed** logins
> count toward the limit, so a burst of successful sign-ins never locks staff
> out, and a failed login is audited against the client **IP only** (never the
> submitted username — so a password fat-fingered into the username box is
> never stored).

### 4.4 Referral Sheet intake (optional)

| Env key | Default | Effect |
|---|---|---|
| `REFERRALS_SHEET_ID` | _(empty)_ | Google Sheet to poll for referrals. If empty or the service-account file / `googleapiclient` is missing, sheet polling reports "not configured" and never crashes. |
| `SERVICE_ACCOUNT_JSON` | `…/Antigravity/service_account.json` | service-account credentials for the sheet |

Drop-folder intake always works regardless: put referral `*.json` files in
`app/data/referrals_in/` and POST `/api/referrals/ingest` (or use the UI).

### 4.5 Comms — email & phone (optional)

The Inbox polls **email** (Gmail via IMAP/SMTP app-password) and **RingCentral**
(SMS, fax, voicemail). All keys empty → each arm reports `not configured` and
the app runs fine without comms.

**Gmail — multiple mailboxes.** The practice runs **three** mailboxes —
`mainlinesurgery@`, `mainlinepain@`, and `mainsurgical@gmail.com` — and
**referrals mostly arrive at `mainlinepain@`**. Configure all of them at once
with `GMAIL_ACCOUNTS`; every mailbox is polled and each stored message's
recipient is the account it arrived at. A per-account failure never stops the
other mailboxes (the poll result surfaces each account's new-count, or
`error: <class>`, and the Inbox "Poll now" toast shows the per-account
breakdown).

| Env key | Default | Effect |
|---|---|---|
| `GMAIL_ACCOUNTS` | _(empty)_ | Comma-separated `address:app_password` pairs — the multi-mailbox source of truth. Every account is polled; the **first** account is the default sender for outbound email. Use a Gmail **app password** per account, not the login password. |
| `GMAIL_ADDRESS` | _(empty)_ | **Back-compat single account.** Treated as one account and deduped into `GMAIL_ACCOUNTS`; if set it names the default sender. |
| `GMAIL_APP_PASSWORD` | _(empty)_ | App password paired with `GMAIL_ADDRESS`. |

> Zero accounts configured → email poll reports `not configured` (no crash).
> Outbound email uses the first configured account by default; a caller may
> target a specific mailbox via `send_email(from_account=…)`, which must be one
> of the configured addresses (else it errors — no silent fallback).

**RingCentral — SMS + fax + voicemail.** The poller ingests **all inbound**
message-store types: SMS (body = the text), fax (body = a page-count summary;
attachment metadata is stored, the file is **not** downloaded), and voicemail
(body = a duration summary + attachment metadata). Deduped by RingCentral id.
The Inbox channel filter exposes `email` / `sms` / `fax` / `voicemail`.

| Env key | Default | Effect |
|---|---|---|
| `RC_SERVER_URL` | `https://platform.ringcentral.com` | RingCentral platform base URL. |
| `RC_CLIENT_ID` | _(empty)_ | App client id. |
| `RC_CLIENT_SECRET` | _(empty)_ | App client secret. |
| `RC_JWT` | _(empty)_ | JWT credential. Empty → the RC arm reports `not configured`. |
| `RC_FROM_NUMBER` | _(empty)_ | Sending number for outbound SMS. |

### 4.6 In-app scheduler (background jobs)

`app/scheduler.py` runs an asyncio background loop started at server startup
(and cancelled cleanly on shutdown). One tick every 60s, each job wrapped so a
failure is audited (`kind=scheduler`) and never crashes the loop. It runs three
jobs: a periodic **comms poll**, a once-daily **manager report**, and a
once-daily **referral audit** (only when `EMR_ENABLED` and Gmail are configured;
otherwise skipped silently). Current status is exposed under `scheduler` in
`GET /api/health`.

| Env key | Default | Effect |
|---|---|---|
| `APP_SCHEDULER` | `1` | `0/false/no` disables the entire background loop. `verify_app.py` runs with `APP_SCHEDULER=0` so a test run never fires real polls/manager/audits. |
| `COMMS_POLL_MINUTES` | `5` | Comms-poll cadence in minutes. `0` disables just the periodic poll (the daily jobs still run). |
| `MANAGER_RUN_HOUR` | `2` | Local-time hour (0–23) the daily manager report runs. |
| `AUDIT_RUN_HOUR` | `7` | Local-time hour (0–23) the daily referral audit runs. |

> The scheduler is additive — poll/manager/audit can still be triggered
> manually from the UI/API. Leave `APP_SCHEDULER=1` in production so the app
> keeps its inbox and reports current on its own; set `0` only for
> test/debug runs where you want no background activity.

### 4.7 🔴 EMR write allow-lists — GO-LIVE ONLY (read the warnings)

EMR **writes** (Svigg booking / cancellation) are fail-closed at the scraper.
Out of the box, only the designated **test account `22041163`** can be
written, so you cannot touch a real patient even by accident. To go live for
real patients you widen the allow-lists **via env — no source edit needed:**

| Env key | Format | Effect |
|---|---|---|
| `SVIGG_BOOKING_ALLOWLIST` | `acct,acct,…` or `*` | Adds accts allowed to be **booked**. `*` allows **any** acct (removes the per-acct guard entirely). |
| `SVIGG_CANCEL_ALLOWLIST` | `acct:nametoken,…` or `*` | Adds `acct → required-last-name-token` bindings for **cancellation**. `*` sets "allow any" and drops the acct→name binding, permitting cancel of any appointment with a non-empty last name. |
| `SVIGG_BOOKING_EXECUTE` | `1/true/yes` | Separate kill-switch that must ALSO be truthy before any booking commit runs. |

> ### ⚠️ These flags let the app change the live production calendar.
> - **`*` is a loaded gun.** It removes the guard that stops the app from
>   writing to a real patient's record. Prefer explicit acct lists; only use
>   `*` after the booking/cancel paths are fully trusted in production.
> - The **cancel** name-token binding is the last defense that keeps an
>   allow-listed acct from being aimed at the wrong patient's row. `*` removes
>   it. Widen cancel with explicit `acct:name` pairs whenever possible.
> - **Never** claim a booking is "verified" — Svigg booking has no
>   auto-verify; the app says so in every result. Svigg's calendar shows a
>   multi-day window and the clicked cell decides the real date; double-check.
> - All writes still require **on-screen human approval** in the app. The
>   allow-lists gate what the approval is even *allowed to attempt*.
> - Keep these in `.env`, not the plist. Start empty; add one real acct;
>   verify end-to-end on that one patient before widening further.

---

## 5. Verify the deployment (offline smoke test)

`verify_app.py` runs the real server on port **8788** with `EMR_ENABLED=0` and
`APP_SCHEDULER=0` (so the background loop never fires during the test) and
drives the HTTP API end-to-end using only the Python stdlib — **no EMR, no
real LLM calls.** It snapshots `app/data/` before the run and restores it
byte-for-byte afterward (even on crash), so it will not clobber real users or
the database. One of the checks asserts `GET /api/health` reports the scheduler
as disabled in that run.

```bash
cd /Users/shubh/n8n-office
python3 verify_app.py
# ... one PASS/FAIL line per check ...
# VERIFY_APP PASS N/N     <-- exit code 0 only if EVERY check passed
```

Run this after install and after any config change.

---

## 6. Reverse proxy + TLS (required for `APP_ENV=prod`)

Bind the app to loopback (`APP_HOST=127.0.0.1`) and terminate TLS in a proxy.
PHI over the wire must be encrypted; a Secure cookie also needs HTTPS.

### Caddy (auto-HTTPS)

```caddy
backoffice.internal.example {
    reverse_proxy 127.0.0.1:8787
}
```

(For a LAN-only hostname without a public CA, use Caddy's internal CA:
`tls internal` inside the site block, and trust its root on client machines.)

### nginx

```nginx
server {
    listen 443 ssl;
    server_name backoffice.internal.example;

    ssl_certificate     /etc/ssl/backoffice/fullchain.pem;
    ssl_certificate_key /etc/ssl/backoffice/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8787;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Once HTTPS is in front, set `APP_ENV=prod` so the session cookie is marked
Secure. (Serving the app directly over plain http with `APP_ENV=prod` will
break login because the browser drops the Secure cookie.)

---

## 7. Run under launchd (macOS, start-on-login + auto-restart)

An example is at `deploy/launchd.example.plist` (label
`com.atlantic.backoffice`). It runs `python3 -m app.server`, keeps it alive on
crash, sets `WorkingDirectory` to the repo root, and writes stdout/stderr to
`app/data/logs/`. Review the paths/port/env inside it first.

```bash
# 1. Make sure the log dir exists
mkdir -p /Users/shubh/n8n-office/app/data/logs

# 2. Install a copy (edit paths/port/interpreter inside it first)
cp /Users/shubh/n8n-office/deploy/launchd.example.plist \
   ~/Library/LaunchAgents/com.atlantic.backoffice.plist

# 3. Load it
launchctl load ~/Library/LaunchAgents/com.atlantic.backoffice.plist

# Status / logs
launchctl list | grep com.atlantic.backoffice
tail -f /Users/shubh/n8n-office/app/data/logs/backoffice.out.log
tail -f /Users/shubh/n8n-office/app/data/logs/backoffice.err.log

# Stop / uninstall
launchctl unload ~/Library/LaunchAgents/com.atlantic.backoffice.plist
rm ~/Library/LaunchAgents/com.atlantic.backoffice.plist
```

Notes:
- It is a **LaunchAgent** (runs as your user), not a LaunchDaemon — the app
  handles PHI and must run with normal user ownership, **never as root**.
- The plist runs `bash -lc 'cd /Users/shubh/n8n-office && exec python3 -m
  app.server'`, so `python3` resolves from your login-shell PATH (the same
  interpreter you use interactively). If you need a specific interpreter, adjust
  the command string or your shell PATH.
- Host/port are read from `.env` (`APP_HOST` / `APP_PORT`), not CLI flags — set
  them there if the defaults (`127.0.0.1:8787`) don't fit.
- Secrets go in `.env` (loaded by `config.py`), **not** in the plist. Only
  non-secret toggles (`APP_ENV`, `EMR_ENABLED`, and scheduler knobs like
  `APP_SCHEDULER`) belong in its `EnvironmentVariables`.

---

## 8. PHI / HIPAA cautions

- **This app processes PHI.** Treat the machine and `app/data/` accordingly.
- Serve it only on the practice's private network / loopback + TLS. Do not
  expose it to the public internet.
- Only PHI-safe LLM providers (`anthropic`, `openai` — under a signed BAA) may
  receive patient data. The free `openai_compat` proxy is never auto-selected;
  when explicitly configured it is refused for chat entirely (no PHI, no
  history sent). Keep it that way.
- Secrets (`.env`, `users.json`, `service_account.json`) are never committed
  and never logged. **Do not print PHI to browser console or server logs** —
  the code logs referral/DB **ids**, never patient names, so nothing PHI-bearing
  reaches `app/data/logs/*.log` (which the nightly backup in §9 copies off-box).
- All EMR writes go through on-screen human approval and honest results — the
  app never claims a write succeeded unless the EMR response said so. A
  reschedule will **not** book the new slot unless the original cancel is
  *verified* gone, so a patient can never end up holding two live appointments.
- Access is per-user with sessions expiring after 12 hours; provision one
  account per real person. **Offboarding:** run
  `python3 -m app.server --delete-user NAME` (removes the user *and* purges
  their sessions). Session lookups also re-check `users.json` every request, so
  a removed user loses access immediately rather than waiting out the TTL.

---

## 9. Backups

Back up **`app/data/`** — it holds everything stateful and secret:

- `app.db` — audit log, approvals, referrals, manager reports, sessions,
  conversation history (**PHI-bearing**).
- `users.json` — login credentials (hashed, but sensitive).
- `referrals_in/` — inbound referral drop folder (+ `processed/`).

Suggested nightly snapshot (adjust destination; keep it encrypted / off-box):

```bash
tar -czf "$HOME/backups/backoffice-$(date +%F).tgz" \
    -C /Users/shubh/n8n-office app/data
```

Because `app.db` runs in WAL mode, either stop the app before copying or use
`sqlite3 app/data/app.db ".backup '/path/backup.db'"` for a consistent copy.
Store backups encrypted and treat them as PHI.

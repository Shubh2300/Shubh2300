---
name: secure-secrets-handling
description: How to handle API keys, passwords, tokens, and credentials in THIS project so no new secret exposure is ever added. Use this whenever a task involves an API key, password, access token, service account, .env, config, or any credential — adding an integration, reading a key, wiring a new agent, or preparing a commit. It codifies the project's existing good patterns (env-based key resolution + placeholder detection in gpt_client.py, .env loading in server.py) and the rules: never hardcode secrets, never put keys in client-side dashboard.html, keep .gitignore correct, never commit service_account.json. The user has deferred the big secrets cleanup — this skill's job is to make sure we never make it worse and that anything you write hides secrets properly inline.
---

# Secure Secrets Handling

**Project stance:** the existing key/password issues are being fixed *later* — that's fine. Your job is the opposite of cleanup: **never add new exposure.** Any code you write must route secrets correctly from the start. Hiding them properly inline costs nothing extra.

## The rules

1. **Never hardcode a secret.** No literal keys, passwords, or tokens in source — Python, JS, HTML, JSON, or docs (including example URLs).
2. **Read from the environment.** Secrets come from `os.environ`, loaded from `.env` at startup. `server.py` already has `_load_dotenv()` — reuse it; don't roll a new loader.
3. **Resolve keys with fallbacks + placeholder detection**, the way `gpt_client.py` does: try the provider-specific env var, then alternates, and treat empty/`your_api_key_here`/`sk-...` style values as *missing*. Copy that pattern (`_get_api_key` / `_is_placeholder_key`) rather than reading a raw env var blindly.
4. **Keys never reach the browser.** `dashboard.html` is client-side — anything in it is public. Never embed a key there. The browser calls `server.py`; the **server** holds the key and proxies the upstream API (this is exactly how Koko works via `/api/koko`).
5. **Degrade gracefully when a secret is missing.** Don't crash or silently fail — surface a clear state (Koko shows **OFFLINE** with the exact reason and a setup hint). New integrations should do the same.
6. **Never commit credential files.** `service_account.json` is a **live Google service-account private key** — it must stay untracked and gitignored. Same for any `*-key.json`, token cache, or downloaded credential.

## When you ADD a new secret

Do all four, every time:

1. Read it via `os.environ` (with placeholder detection).
2. Add a **placeholder** entry to `.env.example` (commit this) and the real value only to `.env` (gitignored).
3. Make sure `.gitignore` covers its file/pattern.
4. Note it in `README.md` setup so the next person knows to set it.

## .gitignore — current gaps to keep closed

The repo's `.gitignore` already covers `.env`, `.env.*`, `*.key`, `*.pem`, `*.p12`, `secrets.json`, `scratch/`, `*.log`. Two known untracked items are **not** yet covered and must never be committed:

```gitignore
# Google service-account credential (LIVE private key) — never commit
service_account.json
*-service-account.json

# WebEDoctor RPA debug screenshots (transient, may show portal/PHI)
webedoctor_login_*.png
```

If you touch `.gitignore` or stage files, confirm these are excluded. The debug screenshots can contain a login portal (and potentially PHI), so treat them as sensitive, not just clutter.

## Quick checklist before finishing any secret-touching change

- [ ] No literal secret anywhere in the diff (grep your change for keys/passwords).
- [ ] Secret read from `os.environ`, not hardcoded; placeholder values treated as missing.
- [ ] No key shipped to the browser (`dashboard.html`); server proxies instead.
- [ ] New secret → `.env.example` placeholder + `.gitignore` + README note.
- [ ] `service_account.json` and `webedoctor_login_*.png` still untracked.
- [ ] Missing-secret path degrades with a clear message, not a crash.

## Reference implementations in this repo

- `gpt_client.py` — `_get_api_key()`, `_is_placeholder_key()`: the gold standard for resolving + validating a key.
- `server.py` — `_load_dotenv()`: the env loader; the `/api/koko` handler: server-side proxy pattern + graceful OFFLINE.

"""
app/config.py — Central settings for the back-office assistant web app.

Exposes a single ``SETTINGS`` namespace consumed by every other module
(``from app.config import SETTINGS``). On import this module:

  1. Loads ``KEY=VALUE`` lines from ``BASE_DIR/.env`` ONLY, WITHOUT
     overwriting anything already present in ``os.environ`` (real environment
     always wins). This app owns its own ``.env``; it deliberately does NOT
     read the Antigravity ``.env`` — the EMR scrapers load their own portal
     creds from ``ANTIGRAVITY_DIR/.env`` via their own ``_load_env`` at
     construction time, so pulling that file in here would only leak portal
     secrets (SIS_PASSWORD/WEBEDOCTOR_PASS…) into this process env and, worse,
     silently hand this app an ``openai_compat`` LLM key it never configured.
  2. Inserts the EMR integrations dir at ``sys.path[0]`` so sibling
     modules (e.g. ``emr_session_manager``) import cleanly.
  3. Ensures ``DATA_DIR`` exists.

HONESTY / SECURITY NOTES
  - Secrets come only from the environment / this app's own .env — never
    hard-coded, and never inherited from another repo's .env.
  - Provider resolution is PHI-first: ``anthropic`` -> ``openai`` -> ``none``.
    The NON-PHI-safe ``openai_compat`` proxy is selected ONLY when the
    operator EXPLICITLY sets ``CHAT_PROVIDER=openai_compat`` — it is never
    auto-selected just because a ``GPT_ENDPOINT`` happens to be in the env.
  - ``PHI_SAFE_LLM`` gates whether patient data may flow to the configured
    chat provider. The free local ``openai_compat`` proxy is NOT PHI-safe;
    when it is the provider, chat is refused outright (see ``app.agent``).
  - Cookies are only marked ``secure`` in prod (``APP_ENV=prod``); dev runs
    on plain http://127.0.0.1 so a secure cookie would silently break login.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "app" / "data"


# ---------------------------------------------------------------------------
# .env loading (non-destructive: never clobber a pre-set os.environ value)
# ---------------------------------------------------------------------------

def _load_env_file(path: Path) -> None:
    """Parse simple ``KEY=VALUE`` lines from *path* into ``os.environ``.

    Blank lines and ``#`` comments are skipped. ``export`` prefixes and
    surrounding quotes on the value are stripped. A key already present in
    the environment is left untouched, so the real environment always wins.
    """
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - unreadable .env is non-fatal
        logger.warning("could not read env file %s: %s", path, exc)
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


# This app's OWN .env only. The Antigravity .env is deliberately NOT loaded
# here (the EMR scrapers load their portal creds from it themselves); pulling
# it in would leak portal secrets into this process and could auto-hand this
# app a non-PHI-safe LLM key it never chose.
_load_env_file(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# sys.path: make EMR integrations importable as top-level modules
# ---------------------------------------------------------------------------

_INTEGRATIONS = str(BASE_DIR / "python" / "integrations")
if _INTEGRATIONS not in sys.path:
    sys.path.insert(0, _INTEGRATIONS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _bool_env(name: str, default: str) -> bool:
    return _env(name, default).strip().lower() in ("1", "true", "yes")


def _int_env(name: str, default: int) -> int:
    raw = _env(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid int for %s=%r; using %d", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

_ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
_OPENAI_API_KEY = _env("OPENAI_API_KEY")
_GPT_ENDPOINT = _env("GPT_ENDPOINT")
_GPT_API_KEY = _env("GPT_API_KEY")
_GPT_MODEL = _env("GPT_MODEL")
_CHAT_PROVIDER_ENV = _env("CHAT_PROVIDER").strip().lower()

# Provider resolution is PHI-FIRST and NEVER silently picks a non-PHI-safe
# backend. Auto-selection only ever chooses a PHI-safe cloud (anthropic ->
# openai) or 'none'. The NON-PHI-safe openai_compat proxy is used ONLY when
# the operator EXPLICITLY opts in with CHAT_PROVIDER=openai_compat — so a
# stray GPT_ENDPOINT in the environment can never route patient chat to an
# unsafe model. When openai_compat is chosen, app.agent REFUSES LLM chat
# entirely (this is a PHI tool; there is no type-PHI-into-an-unsafe-model
# path).
if _CHAT_PROVIDER_ENV == "openai_compat":
    _CHAT_PROVIDER = "openai_compat"
elif _CHAT_PROVIDER_ENV == "anthropic":
    _CHAT_PROVIDER = "anthropic"
elif _CHAT_PROVIDER_ENV == "openai":
    _CHAT_PROVIDER = "openai"
elif _ANTHROPIC_API_KEY:
    _CHAT_PROVIDER = "anthropic"
elif _OPENAI_API_KEY:
    _CHAT_PROVIDER = "openai"
else:
    _CHAT_PROVIDER = "none"

# PHI may only flow to first-party clouds we have BAAs with. The free local
# openai-compatible proxy is explicitly NOT PHI-safe.
_PHI_SAFE_LLM = _CHAT_PROVIDER in ("anthropic", "openai")

_DEFAULT_MODELS = {
    # User directive 2026-07-02: Claude Fable 5 is the chat brain whenever an
    # Anthropic key is present (gpt-4o-mini hallucinated tool calls).
    "anthropic": "claude-fable-5",
    "openai": "gpt-4o-mini",
    "openai_compat": _GPT_MODEL,
    "none": _GPT_MODEL,
}
# Provider-specific override wins, then generic CHAT_MODEL, then the default —
# so a Claude model name in ANTHROPIC_CHAT_MODEL can sit inert in .env without
# ever being sent to OpenAI while the anthropic provider is not yet active.
_PROVIDER_MODEL_ENV = {
    "anthropic": "ANTHROPIC_CHAT_MODEL",
    "openai": "OPENAI_CHAT_MODEL",
    "openai_compat": "GPT_MODEL",
}
_CHAT_MODEL = (
    _env(_PROVIDER_MODEL_ENV.get(_CHAT_PROVIDER, ""))
    or _env("CHAT_MODEL")
    or _DEFAULT_MODELS.get(_CHAT_PROVIDER, "")
)

_APP_ENV = _env("APP_ENV") or "dev"


# ---------------------------------------------------------------------------
# Comms lane (email + SMS) — all OPTIONAL. When a credential is absent the
# corresponding arm in app.comms degrades to 'not configured' and never raises,
# so the app runs identically on a box with no mail/telephony creds.
# ---------------------------------------------------------------------------

# Gmail (consumer account, IMAP/SMTP app-password flow).
#
# MULTI-ACCOUNT: the practice runs THREE mailboxes (mainlinesurgery@,
# mainlinepain@, mainsurgical@gmail.com — referrals mostly arrive at
# mainlinepain@). GMAIL_ACCOUNTS is a comma-separated list of
# "address:app_password" pairs. The app-password itself never contains a
# colon or a comma (Google renders it as 16 lowercase letters, usually shown
# in 4 groups), so a plain split on ':' / ',' is safe.
#
# BACK-COMPAT: the legacy single-account GMAIL_ADDRESS + GMAIL_APP_PASSWORD
# pair is still honoured and folded into the same account list, deduped by
# address (case-insensitively) so a box that also lists that address in
# GMAIL_ACCOUNTS does not poll it twice. The FIRST account is the default
# sender for send_email.
_GMAIL_ADDRESS = _env("GMAIL_ADDRESS")
_GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD")


def _parse_gmail_accounts(raw: str, legacy_addr: str, legacy_pw: str) -> list:
    """Parse GMAIL_ACCOUNTS into a deduped ``list[(address, app_password)]``.

    Accepts a comma-separated list of ``address:app_password`` pairs. The
    legacy ``GMAIL_ADDRESS``/``GMAIL_APP_PASSWORD`` pair (when both are set)
    is appended as one more account. Duplicate addresses (case-insensitive)
    keep only their FIRST occurrence, so order — and thus the default sender —
    is stable and predictable. Malformed entries (blank, no ':', empty half)
    are skipped silently; a bad line must never crash config import.
    """
    accounts: list = []
    seen: set = set()

    def _add(addr: str, pw: str) -> None:
        addr = (addr or "").strip()
        pw = (pw or "").strip()
        if not addr or not pw:
            return
        key = addr.lower()
        if key in seen:
            return
        seen.add(key)
        accounts.append((addr, pw))

    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        addr, _, pw = entry.partition(":")
        _add(addr, pw)

    # Legacy single-account pair folds in last (deduped by address).
    _add(legacy_addr, legacy_pw)
    return accounts


_GMAIL_ACCOUNTS = _parse_gmail_accounts(
    _env("GMAIL_ACCOUNTS"), _GMAIL_ADDRESS, _GMAIL_APP_PASSWORD
)

# RingCentral (OAuth 2.0 JWT credentials flow). These keys are NOT in any .env
# yet; everything downstream must degrade to 'not configured' cleanly without
# them. RC_SERVER_URL keeps the platform default; the rest default to ''.
_RC_SERVER_URL = (_env("RC_SERVER_URL") or "https://platform.ringcentral.com").rstrip("/")
_RC_CLIENT_ID = _env("RC_CLIENT_ID")
_RC_CLIENT_SECRET = _env("RC_CLIENT_SECRET")
_RC_JWT = _env("RC_JWT")
_RC_FROM_NUMBER = _env("RC_FROM_NUMBER")


# ---------------------------------------------------------------------------
# In-app scheduler (app.scheduler) — an asyncio background loop started at
# server startup. All knobs OPTIONAL with safe defaults; the loop degrades to
# a no-op when a job's prerequisites (creds / EMR) are absent, and can be
# switched off entirely with APP_SCHEDULER=0.
# ---------------------------------------------------------------------------

# Master on/off switch. Default ON; APP_SCHEDULER=0 disables the loop (used by
# verify_app so a test run never fires real polls / manager / referral audits).
_SCHEDULER_ENABLED = _bool_env("APP_SCHEDULER", "1")
# Comms poll cadence in minutes; 0 disables the periodic poll job.
_COMMS_POLL_MINUTES = _int_env("COMMS_POLL_MINUTES", 5)
# Local-time hour (0-23) for the once-daily manager and referral-audit jobs.
_MANAGER_RUN_HOUR = _int_env("MANAGER_RUN_HOUR", 2)
_AUDIT_RUN_HOUR = _int_env("AUDIT_RUN_HOUR", 7)


SETTINGS = SimpleNamespace(
    # Paths
    BASE_DIR=BASE_DIR,
    DATA_DIR=DATA_DIR,
    DB_PATH=DATA_DIR / "app.db",
    USERS_PATH=DATA_DIR / "users.json",
    # Server
    APP_PORT=_int_env("APP_PORT", 8787),
    HOST=_env("APP_HOST") or "127.0.0.1",
    SESSION_TTL_HOURS=12,
    APP_ENV=_APP_ENV,
    SECURE_COOKIES=(_APP_ENV == "prod"),
    # Roles permitted to APPROVE/EXECUTE EMR writes and run the manager.
    # Default 'manager,admin' — a plain 'staff' account can queue cards but
    # cannot approve them (the human-in-the-loop choke point stays a real
    # second party). Override via env APPROVER_ROLES (comma-separated).
    APPROVER_ROLES=frozenset(
        r.strip().lower()
        for r in (_env("APPROVER_ROLES") or "manager,admin").split(",")
        if r.strip()
    ),
    # EMR
    EMR_ENABLED=_bool_env("EMR_ENABLED", "1"),
    # LLM providers
    ANTHROPIC_API_KEY=_ANTHROPIC_API_KEY,
    OPENAI_API_KEY=_OPENAI_API_KEY,
    GPT_ENDPOINT=_GPT_ENDPOINT,
    GPT_API_KEY=_GPT_API_KEY,
    GPT_MODEL=_GPT_MODEL,
    CHAT_PROVIDER=_CHAT_PROVIDER,
    PHI_SAFE_LLM=_PHI_SAFE_LLM,
    CHAT_MODEL=_CHAT_MODEL,
    MANAGER_MODEL=_env("MANAGER_MODEL") or _CHAT_MODEL,
    # Referrals
    REFERRALS_SHEET_ID=_env("REFERRALS_SHEET_ID"),
    # Default to a repo-local path (not the Antigravity checkout this app
    # documents as NOT wanting to depend on — see the module header). On any box
    # where the file is absent the referrals arm degrades to "not configured"
    # via the sa_path.exists() guards in referrals.py / comms.py, exactly like
    # the other optional credentials. We use BASE_DIR/service_account.json rather
    # than "" because Path("").exists() is truthy (resolves to cwd) and would
    # slip past those guards into a later hard failure (audit finding #15).
    SERVICE_ACCOUNT_JSON=(
        _env("SERVICE_ACCOUNT_JSON")
        or str(BASE_DIR / "service_account.json")
    ),
    # Comms — Gmail (IMAP/SMTP app-password). Empty when not configured.
    #
    # GMAIL_ACCOUNTS is the multi-account source of truth: a deduped
    # list[(address, app_password)] spanning every configured mailbox. The
    # scalar GMAIL_ADDRESS/GMAIL_APP_PASSWORD below stay for back-compat and
    # name the DEFAULT SENDER — the first configured account — so
    # comms.send_email keeps its single-sender signature. When only
    # GMAIL_ACCOUNTS is set (no legacy scalars), the default sender is derived
    # from the first account so the legacy accessors are never empty while a
    # working account exists.
    GMAIL_ACCOUNTS=_GMAIL_ACCOUNTS,
    GMAIL_ADDRESS=(_GMAIL_ADDRESS or (_GMAIL_ACCOUNTS[0][0] if _GMAIL_ACCOUNTS else "")),
    GMAIL_APP_PASSWORD=(_GMAIL_APP_PASSWORD or (_GMAIL_ACCOUNTS[0][1] if _GMAIL_ACCOUNTS else "")),
    # Comms — RingCentral (JWT). RC_SERVER_URL keeps the platform default;
    # the client/secret/JWT/from-number are empty until provisioned, which
    # keeps the SMS arm at 'not configured'.
    RC_SERVER_URL=_RC_SERVER_URL,
    RC_CLIENT_ID=_RC_CLIENT_ID,
    RC_CLIENT_SECRET=_RC_CLIENT_SECRET,
    RC_JWT=_RC_JWT,
    RC_FROM_NUMBER=_RC_FROM_NUMBER,
    # Scheduler (app.scheduler). SCHEDULER_ENABLED gates the whole loop;
    # COMMS_POLL_MINUTES=0 disables just the periodic comms poll; the two
    # RUN_HOUR knobs pick the local-time hour for the daily manager/audit jobs.
    SCHEDULER_ENABLED=_SCHEDULER_ENABLED,
    COMMS_POLL_MINUTES=_COMMS_POLL_MINUTES,
    MANAGER_RUN_HOUR=_MANAGER_RUN_HOUR,
    AUDIT_RUN_HOUR=_AUDIT_RUN_HOUR,
    # Branding
    BRAND="Atlantic Pain & Wellness · Head Injury Institute",
)


# Side effect required by the contract: guarantee the data dir exists so
# db.py / auth.py / referrals.py can write without a first-run race.
DATA_DIR.mkdir(parents=True, exist_ok=True)

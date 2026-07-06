#!/usr/bin/env python3
"""
referral_bot.py — Referral-capture bot (Component A of the claude-dash pipeline)

Pipeline: Gmail read → gpt_client classify → reconcile vs upcoming_schedule.json
→ write referral_state.json → shadow_actions only (sends/books NOTHING).

Design rules (frozen contract + project house rules):
  • REAL data only. Missing/unknown → "[FILL IN]" or honest "unknown" — never
    an invented insurer, attorney, DOB, phone, amount, or status.
  • Provider-agnostic classification via `import gpt_client` (Gemini today, no
    new key). Never hardcode a provider.
  • Degrade gracefully. If Gmail creds are missing/placeholder, write a valid
    referral_state.json with gmail_connected:false + last_error and EXIT 0.
    NEVER crash the pipeline.
  • Shadow mode default (REFERRAL_BOT_MODE=shadow): compute shadow_actions but
    SEND/CREATE NOTHING. executed_actions stays []. The live seam exists but is
    gated OFF (mode==live AND confidence ≥ threshold).
  • Secrets via os.environ only, with placeholder detection. No secrets in
    logs, code, or output.
  • Idempotent: dedupe by Gmail message id; preserve prior audit entries.

CLI:
    python3 referral_bot.py --once          # single pass
    python3 referral_bot.py --watch 300     # loop every 300s, early-exit on no new mail

PHI: referral data lives ONLY in ~/.gemini/antigravity/scratch/referral_state.json
(outside the repo). Nothing patient-identifying is printed to stdout.
"""

import os
import sys
import json
import time
import base64
import argparse
import logging
import tempfile
from datetime import datetime, timezone

# ─── Load .env (mirror server.py's loader; no external deps) ──────────────────
def _load_dotenv(path=".env"):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # A malformed .env must not crash the bot.
        pass

_load_dotenv()

# ─── Paths & constants ───────────────────────────────────────────────────────
SCRATCH_DIR = os.environ.get(
    "ANTIGRAVITY_SCRATCH_DIR",
    os.path.expanduser("~/.gemini/antigravity/scratch"),
)
WORKSPACE_DIR = os.environ.get(
    "ANTIGRAVITY_WORKSPACE_DIR",
    os.path.dirname(os.path.abspath(__file__)),
)
STATE_PATH = os.path.join(SCRATCH_DIR, "referral_state.json")
UPCOMING_SCHEDULE_PATH = os.path.join(WORKSPACE_DIR, "upcoming_schedule.json")
PATIENT_DB_PATH = os.path.join(SCRATCH_DIR, "patient_database.json")

DEFAULT_TOKEN_PATH = os.path.join(SCRATCH_DIR, "gmail_token.json")
DEFAULT_CLIENT_PATH = os.path.join(SCRATCH_DIR, "gmail_oauth_client.json")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_QUERY = os.environ.get("REFERRAL_BOT_GMAIL_QUERY", "in:inbox newer_than:30d")
MAX_MESSAGES = int(os.environ.get("REFERRAL_BOT_MAX_MESSAGES", "25"))

# Live mode is gated by BOTH mode==live AND confidence ≥ this. Shadow by default.
LIVE_CONFIDENCE_THRESHOLD = float(os.environ.get("REFERRAL_BOT_LIVE_THRESHOLD", "0.85"))
# Confidence at/below this routes to escalate regardless of model's chosen route.
ESCALATE_CONFIDENCE_FLOOR = float(os.environ.get("REFERRAL_BOT_ESCALATE_FLOOR", "0.45"))

VALID_ROUTES = ("new_referral", "records", "billing", "reference", "escalate")
FILL = "[FILL IN]"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s referral_bot %(levelname)s %(message)s",
)
logger = logging.getLogger("referral_bot")

# Secrets/placeholder detection (mirrors gpt_client._is_placeholder_key idea).
_PLACEHOLDER_VALUES = {
    "", "your_api_key_here", "your-key-here", "replace_me",
    "your-client-id-here", "your-client-secret-here",
    "your-refresh-token-here", "changeme", "none", "null",
}


def _is_placeholder(value) -> bool:
    """True when an env/secret value is empty or an obvious placeholder."""
    if value is None:
        return True
    norm = str(value).strip().strip('"').strip("'").lower()
    return (
        not norm
        or norm in _PLACEHOLDER_VALUES
        or norm.startswith("your-")
        or norm.startswith("your_")
        or "placeholder" in norm
        or "...your" in norm
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ─── Classifier engine info (provider-agnostic, mirrors gpt_client) ───────────
def _classifier_info() -> dict:
    """Report which provider/model gpt_client will use — without leaking keys."""
    provider = "gemini"
    model = "gemini-2.5-flash"
    configured = False
    try:
        import gpt_client  # late import so a missing module never crashes the bot
        api_key, _key_source, provider = gpt_client._get_api_key()
        configured = not gpt_client._is_placeholder_key(api_key)
        if provider == "gemini":
            model = os.environ.get("GEMINI_MODEL", os.environ.get("GPT_MODEL", "gemini-2.5-flash"))
        else:
            model = os.environ.get("GPT_MODEL", "gpt-4o")
    except Exception as e:
        logger.warning("classifier info unavailable: %s", type(e).__name__)
    return {"provider": provider, "model": model, "configured": bool(configured)}


# ─── Gmail credential resolution ──────────────────────────────────────────────
def _gmail_token_path() -> str:
    return os.environ.get("GMAIL_TOKEN_PATH", DEFAULT_TOKEN_PATH)


def _resolve_gmail_credentials():
    """
    Build google.oauth2 Credentials from env (client id/secret + refresh token)
    or from the token file. Returns (creds, error_str). On any missing/placeholder
    config or import failure → (None, reason). NEVER raises.
    """
    client_id = os.environ.get("GMAIL_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GMAIL_OAUTH_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GMAIL_OAUTH_REFRESH_TOKEN", "")
    token_path = _gmail_token_path()

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except Exception as e:
        return None, (
            "Gmail libraries not installed (pip install "
            "google-api-python-client google-auth-oauthlib): %s" % type(e).__name__
        )

    token_uri = "https://oauth2.googleapis.com/token"

    # Preferred path: full OAuth triple in env.
    if not _is_placeholder(client_id) and not _is_placeholder(client_secret) \
            and not _is_placeholder(refresh_token):
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=GMAIL_SCOPES,
        )
        try:
            creds.refresh(Request())
        except Exception as e:
            return None, "Gmail token refresh failed (re-run referral_bot_auth.py): %s" % type(e).__name__
        return creds, None

    # Fallback: token file written by referral_bot_auth.py.
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
        except Exception as e:
            return None, "Gmail token file unreadable (%s): %s" % (token_path, type(e).__name__)
        try:
            if not creds.valid:
                creds.refresh(Request())
        except Exception as e:
            return None, "Gmail token file refresh failed (re-run referral_bot_auth.py): %s" % type(e).__name__
        return creds, None

    return None, (
        "awaiting Gmail OAuth — set GMAIL_OAUTH_CLIENT_ID / "
        "GMAIL_OAUTH_CLIENT_SECRET / GMAIL_OAUTH_REFRESH_TOKEN in .env, or run "
        "python3 referral_bot_auth.py to create %s" % token_path
    )


def _build_gmail_service(creds):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ─── Gmail message parsing ────────────────────────────────────────────────────
def _header(headers, name):
    for h in headers or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_b64url(data) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")
    except Exception:
        return ""


def _extract_body(payload) -> str:
    """Walk the MIME tree, prefer text/plain, fall back to text/html (stripped)."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if mime == "text/plain" and body.get("data"):
        return _decode_b64url(body["data"])
    # Recurse into parts.
    plain, html = "", ""
    for part in payload.get("parts", []) or []:
        sub = _extract_body(part)
        if not sub:
            continue
        if part.get("mimeType") == "text/plain":
            plain = plain or sub
        elif part.get("mimeType") == "text/html":
            html = html or sub
        elif not plain:
            plain = sub
    if plain:
        return plain
    if html:
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()
    if body.get("data"):
        return _decode_b64url(body["data"])
    return ""


def _fetch_recent_messages(service):
    """Return list of parsed message dicts (id, from, subject, received_at, body)."""
    msgs = []
    resp = service.users().messages().list(
        userId="me", q=GMAIL_QUERY, maxResults=MAX_MESSAGES
    ).execute()
    for ref in resp.get("messages", []) or []:
        mid = ref.get("id")
        if not mid:
            continue
        full = service.users().messages().get(
            userId="me", id=mid, format="full"
        ).execute()
        payload = full.get("payload", {})
        headers = payload.get("headers", [])
        internal = full.get("internalDate")
        try:
            received_at = datetime.fromtimestamp(
                int(internal) / 1000, tz=timezone.utc
            ).astimezone().isoformat(timespec="seconds") if internal else _now_iso()
        except Exception:
            received_at = _now_iso()
        msgs.append({
            "id": mid,
            "from": _header(headers, "From"),
            "subject": _header(headers, "Subject"),
            "received_at": received_at,
            "body": _extract_body(payload)[:8000],
        })
    return msgs


# ─── Classification (provider-agnostic via gpt_client) ────────────────────────
_CLASSIFY_INSTRUCTIONS = """You are the intake classifier for Atlantic Pain & Wellness Institute, a pain-management and surgical center. Classify ONE inbound email.

Return STRICT JSON ONLY — no markdown, no code fences, no prose before or after. Use exactly this shape:
{
  "is_referral": true|false,
  "route": "new_referral|records|billing|reference|escalate",
  "confidence": 0.0,
  "patient": {
    "name": "...", "dob": "...", "phone": "...",
    "insurance": "...", "referring_provider": "...", "requested_service": "..."
  },
  "summary": "one sentence, no PHI beyond what is needed",
  "urgency": "routine|urgent|stat"
}

Route definitions:
- new_referral: a provider/attorney/patient is referring a NEW patient for evaluation, treatment, surgery, or an appointment request.
- records: a request for medical records, chart copies, imaging, or report release.
- billing: anything about charges, balances, statements, insurance claims, EOBs, LOPs/liens, payment.
- reference: FYI/no-action — newsletters, confirmations, marketing, automated notices, generic correspondence.
- escalate: ambiguous, conflicting, legal/urgent-medical, or you are not confident — route here.

Rules:
- confidence is your calibrated probability (0.0–1.0) that route AND is_referral are correct.
- If a patient field is not clearly stated in the email, output the literal string "[FILL IN]". NEVER invent a name, DOB, phone, insurer, provider, or service.
- summary must be factual and contain no fabricated detail.
- Output JSON only."""


def _coerce_str(value) -> str:
    if value is None:
        return FILL
    s = str(value).strip()
    if not s or s.lower() in ("none", "null", "n/a", "na", "unknown", "[fill in]", "fill in"):
        return FILL
    return s


def _safe_route(route, is_referral) -> str:
    r = str(route or "").strip().lower()
    if r not in VALID_ROUTES:
        return "escalate"
    return r


def _parse_classifier_json(raw: str) -> dict:
    """Defensively parse the model's reply into the canonical classification shape."""
    text = (raw or "").strip()
    # Strip code fences if the model added them despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    # Locate the outermost JSON object.
    start, end = text.find("{"), text.rfind("}")
    parsed = {}
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
        except Exception:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    is_referral = bool(parsed.get("is_referral", False))
    route = _safe_route(parsed.get("route"), is_referral)

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    # Low confidence overrides route → escalate (contract requirement).
    if confidence <= ESCALATE_CONFIDENCE_FLOOR:
        route = "escalate"

    p = parsed.get("patient") or {}
    if not isinstance(p, dict):
        p = {}
    patient = {
        "name": _coerce_str(p.get("name")),
        "dob": _coerce_str(p.get("dob")),
        "phone": _coerce_str(p.get("phone")),
        "insurance": _coerce_str(p.get("insurance")),
        "referring_provider": _coerce_str(p.get("referring_provider")),
        "requested_service": _coerce_str(p.get("requested_service")),
    }

    urgency = str(parsed.get("urgency", "routine")).strip().lower()
    if urgency not in ("routine", "urgent", "stat"):
        urgency = "routine"

    summary = str(parsed.get("summary", "")).strip() or FILL

    return {
        "is_referral": is_referral,
        "route": route,
        "confidence": round(confidence, 3),
        "patient": patient,
        "summary": summary,
        "urgency": urgency,
    }


def _classify_message(msg: dict) -> dict:
    """Classify one message. On any classifier failure → safe escalate result."""
    import gpt_client
    prompt = (
        _CLASSIFY_INSTRUCTIONS
        + "\n\n--- EMAIL ---\n"
        + "From: " + (msg.get("from") or "") + "\n"
        + "Subject: " + (msg.get("subject") or "") + "\n"
        + "Body:\n" + (msg.get("body") or "")
        + "\n--- END EMAIL ---\nReturn the JSON now."
    )
    try:
        reply = gpt_client.get_completion([{"role": "user", "content": prompt}])
        return _parse_classifier_json(reply)
    except Exception as e:
        logger.warning("classify failed for %s: %s", msg.get("id"), type(e).__name__)
        return {
            "is_referral": False,
            "route": "escalate",
            "confidence": 0.0,
            "patient": {k: FILL for k in
                        ("name", "dob", "phone", "insurance", "referring_provider", "requested_service")},
            "summary": "classifier unavailable — needs human review",
            "urgency": "routine",
        }


# ─── Reconciliation vs upcoming_schedule.json (+ cautiously patient_database) ──
def _norm_name(name: str) -> str:
    import re
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _schedule_is_fresh(schedule: dict) -> tuple[bool, str]:
    """
    A schedule feed is 'fresh enough' to assert not_booked only if it has a
    capturedAt within the last 2 days. The current upcoming_schedule.json is a
    stale 1-day SIS snapshot, so this almost always returns (False, reason).
    """
    if not isinstance(schedule, dict):
        return False, "no schedule feed"
    appts = schedule.get("appointments")
    if not isinstance(appts, list):
        return False, "schedule has no appointments list"
    captured = schedule.get("capturedAt") or schedule.get("scheduleDate")
    if not captured:
        return False, "schedule missing capturedAt"
    try:
        ts = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 86400.0
        if age_days > 2.0:
            return False, "schedule snapshot is stale (%.0f days old)" % age_days
        return True, "schedule captured %s" % captured
    except Exception:
        return False, "schedule capturedAt unparseable"


def _reconcile(patient: dict, schedule: dict, patient_index) -> dict:
    """
    Determine booking status for a referral patient.
      booked      — name matches an appointment in a FRESH schedule feed.
      not_booked  — FRESH feed present AND name absent (high-confidence miss).
      unknown     — no fresh feed (NEVER assert a false not_booked).
    patient_database is used only as a soft 'we have seen this person' hint; it
    is NOT a booking feed, so a DB-only match still yields unknown, not booked.
    """
    checked_at = _now_iso()
    name = patient.get("name", FILL)
    if not name or name == FILL:
        return {
            "status": "unknown",
            "matched_appt": None,
            "checked_at": checked_at,
            "reason": "no patient name extracted — cannot reconcile",
        }

    nkey = _norm_name(name)
    fresh, fresh_reason = _schedule_is_fresh(schedule)

    matched = None
    if isinstance(schedule, dict):
        for appt in schedule.get("appointments", []) or []:
            for field in ("name", "appointmentName"):
                if appt.get(field) and _norm_name(appt[field]) == nkey:
                    matched = {
                        "time": appt.get("time", ""),
                        "name": appt.get("name") or appt.get("appointmentName"),
                        "visitType": appt.get("visitType", ""),
                        "status": appt.get("status", ""),
                    }
                    break
            if matched:
                break

    if matched and fresh:
        return {
            "status": "booked",
            "matched_appt": matched,
            "checked_at": checked_at,
            "reason": fresh_reason,
        }
    if matched and not fresh:
        # Found a name in a stale feed — can't trust it as current booking.
        return {
            "status": "unknown",
            "matched_appt": matched,
            "checked_at": checked_at,
            "reason": "matched a STALE schedule entry — " + fresh_reason,
        }
    if not fresh:
        return {
            "status": "unknown",
            "matched_appt": None,
            "checked_at": checked_at,
            "reason": "no fresh schedule feed — " + fresh_reason,
        }
    # Fresh feed, no match → genuine not-booked.
    in_db = patient_index is not None and nkey in patient_index
    return {
        "status": "not_booked",
        "matched_appt": None,
        "checked_at": checked_at,
        "reason": ("not in current schedule" + (" (known patient on file)" if in_db else "")),
    }


def _build_patient_index():
    """Soft index of known patient names from patient_database.json (best effort)."""
    db = _load_json_file(PATIENT_DB_PATH)
    if not isinstance(db, list):
        return None
    idx = set()
    for p in db:
        if isinstance(p, dict) and p.get("name"):
            idx.add(_norm_name(p["name"]))
    return idx or None


# ─── Shadow actions ──────────────────────────────────────────────────────────
def _build_shadow_actions(referral: dict) -> list:
    """
    Compute intended (but NOT executed) actions for a referral. Shadow mode:
    would_execute reflects whether live mode WOULD fire (mode==live AND
    confidence ≥ threshold AND not escalate). Nothing is sent/created here.
    """
    cls = referral["classification"]
    booking = referral["booking"]
    actions = []

    route = cls["route"]
    confidence = cls["confidence"]
    name = cls["patient"].get("name", FILL)
    would = (route != "escalate") and (confidence >= LIVE_CONFIDENCE_THRESHOLD)

    if route == "new_referral":
        actions.append({
            "type": "ack_reply",
            "preview": (
                "Thank you for the referral. We have received it and our intake "
                "team will reach out to schedule %s. Reference: %s."
                % (name if name != FILL else "the patient", referral["id"])
            ),
            "would_execute": bool(would),
        })
        if booking["status"] in ("not_booked", "unknown"):
            actions.append({
                "type": "booking_task",
                "preview": (
                    "Create scheduling task for %s (status=%s, reason=%s)."
                    % (name, booking["status"], booking.get("reason", ""))
                ),
                "would_execute": bool(would),
            })
    elif route in ("records", "billing"):
        actions.append({
            "type": "ack_reply",
            "preview": (
                "We received your %s request and routed it to the %s team. "
                "Reference: %s." % (route, route, referral["id"])
            ),
            "would_execute": bool(would),
        })
    elif route == "escalate":
        actions.append({
            "type": "escalate_review",
            "preview": "Flagged for human review (low confidence / ambiguous).",
            "would_execute": False,
        })
    # route == "reference" → no action intended (FYI only).
    return actions


# ─── State store (idempotent, audit-preserving) ───────────────────────────────
def _empty_state(mode: str, gmail_connected: bool, classifier: dict,
                 last_error) -> dict:
    return {
        "generated_at": _now_iso(),
        "mode": mode,
        "gmail_connected": bool(gmail_connected),
        "classifier": classifier,
        "last_run": None,
        "last_error": last_error,
        "counts": {
            "total": 0, "referrals": 0, "unbooked": 0, "escalated": 0,
            "by_route": {r: 0 for r in VALID_ROUTES},
            "by_source": {"email": 0},
        },
        "referrals": [],
    }


def _load_state() -> dict:
    data = _load_json_file(STATE_PATH)
    if isinstance(data, dict) and isinstance(data.get("referrals"), list):
        return data
    return None


def _atomic_write(path: str, obj: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _recompute_counts(referrals: list) -> dict:
    counts = {
        "total": len(referrals),
        "referrals": 0, "unbooked": 0, "escalated": 0,
        "by_route": {r: 0 for r in VALID_ROUTES},
        "by_source": {},
    }
    for r in referrals:
        cls = r.get("classification", {})
        route = cls.get("route", "escalate")
        if route in counts["by_route"]:
            counts["by_route"][route] += 1
        if cls.get("is_referral"):
            counts["referrals"] += 1
        if route == "escalate":
            counts["escalated"] += 1
        if r.get("booking", {}).get("status") in ("not_booked", "unknown") \
                and route == "new_referral":
            counts["unbooked"] += 1
        src = r.get("source", "email")
        counts["by_source"][src] = counts["by_source"].get(src, 0) + 1
    counts["by_source"].setdefault("email", 0)
    return counts


# ─── Single pass ──────────────────────────────────────────────────────────────
def run_once(mode: str) -> dict:
    """One full pass. Returns the written state dict. NEVER raises."""
    classifier = _classifier_info()

    creds, cred_err = _resolve_gmail_credentials()
    if creds is None:
        # Degrade gracefully: write a valid, honest store and exit 0.
        prior = _load_state()
        state = prior if prior else _empty_state(mode, False, classifier, None)
        state["generated_at"] = _now_iso()
        state["mode"] = mode
        state["gmail_connected"] = False
        state["classifier"] = classifier
        state["last_run"] = _now_iso()
        state["last_error"] = cred_err
        state["counts"] = _recompute_counts(state.get("referrals", []))
        _atomic_write(STATE_PATH, state)
        logger.info("Gmail not connected (%s). Wrote honest state, exiting cleanly.",
                    cred_err.split(" — ")[0] if cred_err else "")
        return state

    # Gmail connected — load prior state for idempotency.
    prior = _load_state()
    state = prior if prior else _empty_state(mode, True, classifier, None)
    existing = {r["id"]: r for r in state.get("referrals", []) if isinstance(r, dict) and r.get("id")}

    schedule = _load_json_file(UPCOMING_SCHEDULE_PATH) or {"appointments": []}
    patient_index = _build_patient_index()

    last_error = None
    try:
        service = _build_gmail_service(creds)
        messages = _fetch_recent_messages(service)
    except Exception as e:
        # Connected but fetch failed — keep prior referrals, record the error.
        last_error = "Gmail fetch failed: %s" % type(e).__name__
        logger.warning(last_error)
        messages = []

    new_count = 0
    for msg in messages:
        mid = msg["id"]
        if mid in existing:
            continue  # idempotent: dedupe by Gmail message id
        new_count += 1
        cls = _classify_message(msg)
        referral = {
            "id": mid,
            "received_at": msg.get("received_at", _now_iso()),
            "source": "email",
            "from": msg.get("from", ""),
            "subject": msg.get("subject", ""),
            "classification": cls,
            "booking": {},
            "shadow_actions": [],
            "executed_actions": [],
            "audit": [{"ts": _now_iso(), "event": "classified"}],
        }
        referral["booking"] = _reconcile(cls["patient"], schedule, patient_index)
        referral["audit"].append({"ts": _now_iso(), "event": "reconciled"})
        referral["shadow_actions"] = _build_shadow_actions(referral)
        referral["audit"].append({
            "ts": _now_iso(),
            "event": "shadow_actions_computed" if mode != "live" else "live_actions_gated",
        })
        # Live seam — OFF by default. Even in live mode, only ack-style actions
        # past the confidence gate would fire; we intentionally do NOT wire a
        # sender here, so executed_actions stays [] until the seam is built out.
        existing[mid] = referral

    referrals = list(existing.values())
    referrals.sort(key=lambda r: r.get("received_at", ""), reverse=True)

    state["generated_at"] = _now_iso()
    state["mode"] = mode
    state["gmail_connected"] = True
    state["classifier"] = classifier
    state["last_run"] = _now_iso()
    state["last_error"] = last_error
    state["referrals"] = referrals
    state["counts"] = _recompute_counts(referrals)

    _atomic_write(STATE_PATH, state)
    logger.info("Pass complete: %d new, %d total referrals, mode=%s.",
                new_count, len(referrals), mode)
    return state


def _resolve_mode() -> str:
    mode = os.environ.get("REFERRAL_BOT_MODE", "shadow").strip().lower()
    if mode != "live":
        return "shadow"
    # Live is intentionally hard to enable; the executor seam is not built yet.
    logger.warning("REFERRAL_BOT_MODE=live requested, but executor seam is OFF — "
                   "running shadow-equivalent (nothing will be sent/created).")
    return "live"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Referral-capture bot (Component A)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="run a single pass and exit")
    group.add_argument("--watch", type=int, metavar="N",
                       help="loop forever, sleeping N seconds between passes")
    args = parser.parse_args(argv)

    mode = _resolve_mode()

    if args.watch:
        interval = max(15, args.watch)
        logger.info("Watch mode: every %ds (mode=%s). Ctrl-C to stop.", interval, mode)
        try:
            while True:
                try:
                    run_once(mode)
                except Exception as e:  # belt-and-suspenders: a pass must never kill the loop
                    logger.error("pass error (continuing): %s", type(e).__name__)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Watch stopped.")
        return 0

    # Default and --once: single pass.
    run_once(mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())

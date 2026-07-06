#!/usr/bin/env python3
"""
triage-router.py — Triage Agent + Intent Router (front door of the n8n office).

Receives RingCentral inbound-SMS webhooks, verifies the delivery, asks Claude
Haiku to classify the message into one of four intents
({appointment, file_request, patient_lookup, general_message}), writes one row
to the SHA-256 hash-chained audit log, and dispatches to a per-intent handler
stub. The handlers themselves live in sibling files and are imported lazily so
this front door stays useful even before they exist.

Per Q3 (locked decisions): triage runs through the Anthropic API directly —
freellmapi is dev-only, Hermes is parked, and Anthropic is the only path that
is both BAA-eligible and operationally simple. Per Q11: a Business Associate
Agreement with Anthropic must be in place before this is pointed at real PHI.

Per Q10: webhook acks return 200 fast. The verify+classify+dispatch happens
inline today (single-tenant clinic, low volume); when load grows, swap the
inline call for an enqueue + background worker without changing the public
shape of POST /sms.

Per Q7: every request — verified or not, classified or not, dispatched or not
— writes exactly one row to audit_log so the chain captures the whole story.

Run:
    pip install fastapi uvicorn anthropic
    # populate env vars (see TODO: WIRE CREDS markers below), then:
    uvicorn flows.triage-router:app --host 127.0.0.1 --port 8787
    # or run the smoke test:
    python3 flows/triage-router.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

# ─── Make the integrations package importable without installing it ───────────
# The flow lives at <repo>/python/flows/, and the adapters live at
# <repo>/python/integrations/. Adding <repo>/python/ to sys.path lets us write
# `from integrations import ...` regardless of where uvicorn was launched from.
_PYTHON_DIR = Path(__file__).resolve().parent.parent
if str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))

# ─── Load .env at import time (no external dep needed) ────────────────────────
# Mirrors the Antigravity pattern in server.py — the .env file at the repo root
# (chmod 0600 on a FileVault disk per Q6) is the canonical secret store. We do
# NOT overwrite values that are already in the environment so operators can
# still override per-process via `KEY=val uvicorn ...`.
def _load_dotenv(path: str = ".env") -> None:
    env_path = _PYTHON_DIR.parent / path
    if not env_path.exists():
        return
    try:
        with env_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        # .env is optional — failing to read it is fine for the smoke test path.
        pass


_load_dotenv()

# ─── FastAPI is optional at import time so the smoke test can run without it ──
try:
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import JSONResponse, PlainTextResponse
    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover — smoke test path doesn't need FastAPI
    FastAPI = Request = Response = None  # type: ignore[assignment]
    JSONResponse = PlainTextResponse = None  # type: ignore[assignment]
    FASTAPI_AVAILABLE = False

# ─── Adapters — must succeed (they're in this repo, not a third-party dep) ────
from integrations import audit_log as _audit_log_mod
from integrations import ringcentral_adapter

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("TRIAGE_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("triage-router")

# ─── Config / env vars ────────────────────────────────────────────────────────
# Every credential is sourced from env. Where a key is required for the flow
# to do real work (vs. smoke test), we annotate the TODO so an operator
# grepping the repo can find every hole at once.

# TODO: WIRE CREDS - ANTHROPIC_API_KEY
#   The Anthropic API key used by the Haiku triage classifier. Provision via
#   https://console.anthropic.com/. Per Q11, a signed BAA must be in place
#   before this is pointed at real PHI.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# TODO: WIRE CREDS - RC_WEBHOOK_VERIFICATION_TOKEN
#   The shared secret RingCentral echoes on every webhook delivery in the
#   `Verification-Token` header. Set this to the same value you pass to
#   ringcentral_adapter.subscribe_sms_webhook(..., verification_token=...).
#   When empty, deliveries are accepted unverified (DEV ONLY).
RC_WEBHOOK_VERIFICATION_TOKEN = os.environ.get("RC_WEBHOOK_VERIFICATION_TOKEN", "")

# TODO: WIRE CREDS - AUDIT_LOG_PEPPER
#   Per-install secret mixed into the SHA-256 patient-id hash in the audit
#   log. MUST be stable across the lifetime of the log — rotating it breaks
#   the ability to correlate rows for the same patient. Min 32 random bytes
#   recommended; back it up alongside the FileVault recovery key.
AUDIT_LOG_PEPPER = os.environ.get("AUDIT_LOG_PEPPER", "")

# Audit log file path. Defaults to the repo's cache dir so the file co-locates
# with the other operational state on the FileVault volume (per Q6/Q11).
AUDIT_LOG_PATH = os.environ.get(
    "AUDIT_LOG_PATH",
    str(_PYTHON_DIR.parent / "cache" / "audit_log.sqlite"),
)

# Triage model — keep configurable so the operator can pin a version or
# upgrade without code edits. Default points at the cheap classifier per Q3.
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "claude-haiku-4-5")

# Hard cap on the SMS body we ship to the classifier. Inbound SMS rarely
# exceeds 1.6kB; this is belt-and-braces against a runaway concatenated MMS.
MAX_BODY_CHARS = int(os.environ.get("TRIAGE_MAX_BODY_CHARS", "4000"))

# The four locked intent labels. Anything the classifier returns outside this
# set falls back to "general_message" so staff sees the raw text.
VALID_INTENTS = (
    "appointment",
    "file_request",
    "patient_lookup",
    "general_message",
)
FALLBACK_INTENT = "general_message"

# Per Q9 — clinic identity, no persona. This is the only phrase the front door
# is allowed to echo back to a patient unprompted; everything else goes through
# the Q4 staff approval gate downstream.
PATIENT_ACK_TEMPLATE = (
    "This is Atlantic Pain & Wellness. We received your message and a "
    "staff member will follow up. For urgent issues, call our main line. "
    "For emergencies, call 911."
)


# ─── Lazy singletons (audit log + anthropic client) ───────────────────────────
# Built on first use so module import has no side effects beyond loading .env.
_audit_singleton: Optional[_audit_log_mod.AuditLog] = None
_anthropic_singleton: Any = None


def _get_audit() -> _audit_log_mod.AuditLog:
    global _audit_singleton
    if _audit_singleton is None:
        if not AUDIT_LOG_PEPPER:
            raise RuntimeError(
                "AUDIT_LOG_PEPPER is not set; refusing to start the audit log "
                "with an empty pepper (would weaken the patient-id hash to a "
                "plain SHA-256 lookup table). See TODO: WIRE CREDS."
            )
        _audit_singleton = _audit_log_mod.AuditLog(
            AUDIT_LOG_PATH, pepper=AUDIT_LOG_PEPPER
        )
        logger.info("audit log opened at %s", AUDIT_LOG_PATH)
    return _audit_singleton


def _get_anthropic():
    """Return a configured anthropic.Anthropic client.

    Imported lazily so unit tests / the smoke test path don't need the SDK
    installed. Raises a clear error if the key is missing — the caller turns
    that into a 200 ack with audit row outcome=error per Q10.
    """
    global _anthropic_singleton
    if _anthropic_singleton is not None:
        return _anthropic_singleton
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; cannot classify. "
            "See TODO: WIRE CREDS at top of file."
        )
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover — install is operator's job
        raise RuntimeError(
            "anthropic SDK not installed. Run: pip install anthropic"
        ) from exc
    _anthropic_singleton = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_singleton


# ─── Triage prompt ────────────────────────────────────────────────────────────
# Kept short and explicit. Multi-intent messages ("can I reschedule AND get my
# MRI") are why Q3 chose an LLM over rules — the prompt asks for the PRIMARY
# intent and lists the rest in `secondary`, which staff sees in the audit log.
TRIAGE_SYSTEM_PROMPT = (
    "You are the triage classifier for Atlantic Pain & Wellness's SMS front "
    "door. Read the patient's inbound SMS and return ONLY a JSON object with "
    "these exact fields:\n"
    '  {"intent": <one of: appointment, file_request, patient_lookup, '
    'general_message>,\n'
    '   "secondary": [<zero or more of the same labels>],\n'
    '   "confidence": <"high"|"medium"|"low">,\n'
    '   "reason": <one short sentence, no PHI>}\n'
    "\n"
    "Definitions:\n"
    "  appointment      = schedule, reschedule, cancel, confirm a visit\n"
    "  file_request     = patient asks for records, imaging, notes, forms\n"
    "  patient_lookup   = patient asks 'who is my doctor', 'do you have me on "
    "file', 'is my insurance in network', identity/account questions\n"
    "  general_message  = anything else, including clinical questions, "
    "complaints, thank-yous, and unrecognized requests\n"
    "\n"
    "Pick general_message when in doubt. Do NOT diagnose, advise, or address "
    "clinical content. Return JSON only, no markdown fences."
)


def classify_intent(body: str, *, client=None) -> dict:
    """Ask Claude Haiku to classify the SMS body.

    Returns a dict shaped like the TRIAGE_SYSTEM_PROMPT contract, always
    including a valid `intent` from VALID_INTENTS (fall back to
    general_message on any parse/validation problem).

    `client` may be passed in for testing; production callers leave it None
    and we use the module-level singleton.
    """
    body = (body or "").strip()
    if not body:
        return {
            "intent": FALLBACK_INTENT,
            "secondary": [],
            "confidence": "low",
            "reason": "empty body",
        }
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]

    cli = client or _get_anthropic()
    msg = cli.messages.create(
        model=TRIAGE_MODEL,
        max_tokens=200,
        system=TRIAGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": body}],
    )

    # The SDK returns a list of content blocks; we only ask for one text block.
    text = ""
    for block in getattr(msg, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "") or ""
            break

    parsed = _safe_parse_intent(text)
    if parsed["intent"] not in VALID_INTENTS:
        logger.warning("classifier returned unknown intent %r; falling back",
                       parsed.get("intent"))
        parsed["intent"] = FALLBACK_INTENT
        parsed.setdefault("reason", "unknown intent label from classifier")
    return parsed


def _safe_parse_intent(text: str) -> dict:
    """Best-effort JSON parse with a safe fallback.

    Claude is asked for raw JSON, but if it occasionally wraps the object in
    ```json fences we strip them. Anything we can't parse becomes a
    general_message with confidence=low — the patient still gets the ack, and
    staff sees the raw text in the audit log row.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        # Strip ``` and an optional `json` tag.
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {
            "intent": FALLBACK_INTENT,
            "secondary": [],
            "confidence": "low",
            "reason": "classifier returned non-JSON",
        }
    if not isinstance(obj, dict):
        return {
            "intent": FALLBACK_INTENT,
            "secondary": [],
            "confidence": "low",
            "reason": "classifier returned non-object",
        }
    obj.setdefault("intent", FALLBACK_INTENT)
    obj.setdefault("secondary", [])
    obj.setdefault("confidence", "medium")
    obj.setdefault("reason", "")
    # Coerce types just enough to survive downstream consumers.
    if not isinstance(obj["secondary"], list):
        obj["secondary"] = []
    obj["intent"] = str(obj["intent"]).strip().lower()
    return obj


# ─── Intent handler dispatch ──────────────────────────────────────────────────
# Each handler returns a small dict describing what it did. They are imported
# lazily so this router stays useful before the sibling flows exist (per Q2,
# patient_lookup ships first, then file_request, then appointment).
def _stub_handler(intent_name: str) -> Callable[..., dict]:
    def _stub(payload: dict, classification: dict) -> dict:
        logger.info("intent %s queued (handler not yet wired)", intent_name)
        return {
            "handled": False,
            "intent": intent_name,
            "note": f"handler for {intent_name!r} not yet wired; "
                    "queued for staff review",
        }
    return _stub


def _load_handler(intent: str) -> Callable[..., dict]:
    """Try to import a sibling flow module, fall back to a stub.

    Convention: a handler for intent X lives at flows/X.py and exposes
    `def handle(payload: dict, classification: dict) -> dict`.
    """
    # Map intent label -> module filename (hyphens, to match the existing
    # flows/triage-router.py naming convention).
    module_map = {
        "appointment": "appointment",
        "file_request": "file-request",
        "patient_lookup": "patient-lookup",
        "general_message": "general-message",
    }
    mod_basename = module_map.get(intent)
    if not mod_basename:
        return _stub_handler(intent)
    # Use importlib so the hyphenated filename doesn't fight Python's import
    # syntax. This mirrors the pattern ringcentral_adapter.py uses for
    # importing Antigravity's get_access_token().
    import importlib.util
    candidate = _PYTHON_DIR / "flows" / f"{mod_basename}.py"
    if not candidate.exists():
        return _stub_handler(intent)
    spec = importlib.util.spec_from_file_location(
        f"_flows_{mod_basename.replace('-', '_')}", candidate
    )
    if spec is None or spec.loader is None:
        return _stub_handler(intent)
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover — module-author error
        logger.exception("failed to load handler for %s: %s", intent, exc)
        return _stub_handler(intent)
    handler = getattr(mod, "handle", None)
    if not callable(handler):
        logger.warning("flow module %s has no callable handle()", candidate)
        return _stub_handler(intent)
    return handler


# ─── RingCentral payload extraction ───────────────────────────────────────────
# RingCentral's instant-message webhook nests the SMS under
# body.changes[].newMessages[] (full record) OR — for the lightweight payload
# variant — under body.body.from/to/subject. We tolerate both shapes plus a
# direct test payload so the smoke test path doesn't need to construct a full
# nested RC envelope.
def extract_sms_fields(payload: Mapping[str, Any]) -> dict:
    """Pull (from_number, to_number, text, message_id) out of a webhook body.

    Returns a dict with those four keys; missing fields become empty strings.
    Never raises — staff can review the raw payload in the audit log if this
    returns a half-empty dict.
    """
    out = {
        "from_number": "",
        "to_number": "",
        "text": "",
        "message_id": "",
    }
    if not isinstance(payload, Mapping):
        return out

    # Shape 1 — direct fields (used by smoke tests and simple senders).
    if any(k in payload for k in ("from", "to", "text", "body")):
        out["from_number"] = str(payload.get("from", "") or "")
        out["to_number"] = str(payload.get("to", "") or "")
        out["text"] = str(payload.get("text") or payload.get("body") or "")
        out["message_id"] = str(payload.get("id", "") or "")

    # Shape 2 — RingCentral instant-message envelope.
    body = payload.get("body") if isinstance(payload.get("body"), Mapping) else None
    if body:
        # Lightweight: body has from/to/subject/id.
        if "subject" in body and not out["text"]:
            out["text"] = str(body.get("subject", "") or "")
        from_obj = body.get("from") if isinstance(body.get("from"), Mapping) else None
        if from_obj and not out["from_number"]:
            out["from_number"] = str(from_obj.get("phoneNumber", "") or "")
        to_list = body.get("to") if isinstance(body.get("to"), list) else None
        if to_list and not out["to_number"]:
            first = to_list[0] if isinstance(to_list[0], Mapping) else {}
            out["to_number"] = str(first.get("phoneNumber", "") or "")
        if "id" in body and not out["message_id"]:
            out["message_id"] = str(body.get("id", "") or "")

        # Full envelope: body.changes[*].newMessages[*]
        changes = body.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if not isinstance(change, Mapping):
                    continue
                new_msgs = change.get("newMessages")
                if not isinstance(new_msgs, list) or not new_msgs:
                    continue
                msg = new_msgs[0] if isinstance(new_msgs[0], Mapping) else {}
                if not out["text"]:
                    out["text"] = str(msg.get("subject", "") or "")
                if not out["from_number"]:
                    f = msg.get("from") if isinstance(msg.get("from"), Mapping) else {}
                    out["from_number"] = str(f.get("phoneNumber", "") or "")
                if not out["to_number"]:
                    t_list = msg.get("to") if isinstance(msg.get("to"), list) else []
                    if t_list and isinstance(t_list[0], Mapping):
                        out["to_number"] = str(t_list[0].get("phoneNumber", "") or "")
                if not out["message_id"]:
                    out["message_id"] = str(msg.get("id", "") or "")
                break

    return out


# ─── Core request handling — split out so the smoke test can exercise it ──────
def process_inbound_sms(
    payload: Mapping[str, Any],
    *,
    verification: Mapping[str, Any],
    audit: Optional[_audit_log_mod.AuditLog] = None,
    classifier: Optional[Callable[[str], dict]] = None,
) -> dict:
    """Verify-aware processing pipeline. Always writes one audit row.

    `verification` is the dict returned by ringcentral_adapter.verify_webhook.
    `audit` and `classifier` are injected for tests; production callers leave
    them None and we wire in the singletons.

    Returns the response body the FastAPI route will JSON-encode. We
    DELIBERATELY swallow all exceptions here — the caller still 200s per
    Q10, and the audit row captures the failure for later review.
    """
    fields = extract_sms_fields(payload)
    actor = fields["from_number"] or "unknown_sms_sender"
    audit_ref = audit  # may be None in dry-run

    # If verification failed, log it and short-circuit. We still 200 so
    # RingCentral doesn't retry-then-disable the subscription (Q10).
    if not verification.get("ok", False):
        _safe_audit_log(
            audit_ref,
            actor=actor,
            intent="triage-classify",
            action="WEBHOOK_REJECT",
            result_summary="verification_failed",
            error_msg=str(verification.get("reason", "unknown")),
            patient_id=actor or None,
        )
        return {
            "ok": False,
            "stage": "verification",
            "reason": verification.get("reason", "verification failed"),
        }

    # Classify. On any failure (no API key, transient SDK error) we fall back
    # to general_message + low confidence — staff still gets the raw text.
    cls_fn = classifier or classify_intent
    classification: dict
    classify_error: Optional[str] = None
    try:
        classification = cls_fn(fields["text"])
    except Exception as exc:  # pragma: no cover — guarded at smoke-test time
        logger.exception("classification failed: %s", exc)
        classify_error = f"{type(exc).__name__}: {exc}"
        classification = {
            "intent": FALLBACK_INTENT,
            "secondary": [],
            "confidence": "low",
            "reason": "classifier_error",
        }

    intent = classification.get("intent", FALLBACK_INTENT)
    # Belt-and-braces: callers can inject a custom classifier that returns
    # labels outside VALID_INTENTS (the production classify_intent already
    # coerces, but other callers shouldn't have to know that). The dispatcher
    # is the right place to enforce the invariant — keep `intent` valid here
    # so the per-intent handler lookup is always against a known label.
    if intent not in VALID_INTENTS:
        logger.warning(
            "dispatcher received unknown intent %r; coercing to %s",
            intent, FALLBACK_INTENT,
        )
        classification["intent"] = FALLBACK_INTENT
        intent = FALLBACK_INTENT

    # Dispatch to the per-intent handler (stub if not yet implemented).
    handler = _load_handler(intent)
    handler_error: Optional[str] = None
    try:
        handler_result = handler(dict(payload), classification)
    except Exception as exc:  # pragma: no cover — guarded at smoke-test time
        logger.exception("handler %s failed: %s", intent, exc)
        handler_error = f"{type(exc).__name__}: {exc}"
        handler_result = {"handled": False, "intent": intent,
                          "error": handler_error}

    # ONE audit row per inbound SMS. result_summary stays short and
    # PHI-free — patient_id is hashed by the audit module, raw text never
    # lands in the log.
    result_summary = (
        f"intent={intent} "
        f"conf={classification.get('confidence', '?')} "
        f"handled={bool(handler_result.get('handled', False))}"
    )
    err = classify_error or handler_error
    _safe_audit_log(
        audit_ref,
        actor=actor,
        intent="triage-classify",
        action="SMS_INBOUND",
        result_summary=result_summary,
        error_msg=err,
        patient_id=actor or None,
    )

    return {
        "ok": True,
        "intent": intent,
        "confidence": classification.get("confidence"),
        "secondary": classification.get("secondary", []),
        "reason": classification.get("reason"),
        "handler": handler_result,
        "ack_template": PATIENT_ACK_TEMPLATE,
    }


def _safe_audit_log(
    audit: Optional[_audit_log_mod.AuditLog],
    **kwargs: Any,
) -> None:
    """Log if we have a live audit object, otherwise warn-and-skip.

    The smoke test path passes `audit=None` so we don't have to construct an
    AuditLog (which requires a real pepper). In production the singleton is
    always live; if it ever isn't, we degrade to a log warning rather than
    blowing up the webhook (Q10: keep returning 200 to RingCentral).
    """
    if audit is None:
        logger.info("audit (dry-run): %s",
                    {k: v for k, v in kwargs.items() if k != "patient_id"})
        return
    try:
        audit.log(**kwargs)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("audit log write failed: %s", exc)


# ─── FastAPI app ──────────────────────────────────────────────────────────────
# Built only if FastAPI is installed; otherwise `app` is None and the module
# remains importable for the smoke test path.
if FASTAPI_AVAILABLE:
    app = FastAPI(
        title="Atlantic Pain & Wellness — Triage Router",
        description=(
            "Front door for inbound SMS from RingCentral. Verifies the "
            "webhook, classifies the message with Claude Haiku, and "
            "dispatches to the appropriate flow handler. Every request "
            "writes one row to the hash-chained audit log."
        ),
        version="0.1.0",
    )

    @app.get("/healthz")
    def healthz() -> dict:
        """Lightweight readiness probe. Doesn't touch Anthropic or the audit
        log so it's safe to point a local Caddy/nginx at it for keepalives."""
        return {
            "ok": True,
            "anthropic_configured": bool(ANTHROPIC_API_KEY),
            "audit_log_pepper_configured": bool(AUDIT_LOG_PEPPER),
            "rc_verification_configured": bool(RC_WEBHOOK_VERIFICATION_TOKEN),
            "model": TRIAGE_MODEL,
        }

    @app.post("/sms")
    async def sms(request: Request) -> Response:
        """RingCentral inbound-SMS webhook endpoint.

        Always 200s per Q10 — error details live in the audit log. The
        Validation-Token handshake (subscription create/renew) is honored by
        echoing the token in the response header AND body within 5s.
        """
        # Read once — FastAPI lets us re-read body, but parsing twice would
        # double-cost on large MMS payloads.
        raw_body = await request.body()
        # Header object supports dict-style get() with case-insensitive lookup.
        verification = ringcentral_adapter.verify_webhook(
            request.headers,
            raw_body,
            expected_token=RC_WEBHOOK_VERIFICATION_TOKEN or None,
        )

        # Handshake — echo the Validation-Token and STOP. Don't try to parse
        # JSON; the handshake body is empty.
        if verification.get("mode") == "validation":
            tok = verification.get("validation_token") or ""
            logger.info("RC validation handshake — echoing token")
            return PlainTextResponse(
                content=tok,
                status_code=200,
                headers={"Validation-Token": tok},
            )

        # Best-effort JSON parse. RC always sends application/json, but a
        # corrupt/empty body still gets a 200 + error audit row per Q10.
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("invalid JSON in /sms body: %s", exc)
            payload = {"_parse_error": str(exc), "_raw_len": len(raw_body)}

        try:
            audit = _get_audit()
        except RuntimeError as exc:
            # Pepper missing — degrade to dry-run audit rather than 500.
            logger.error("audit unavailable: %s", exc)
            audit = None

        result = process_inbound_sms(
            payload,
            verification=verification,
            audit=audit,
        )
        # 200 always; payload tells the operator (or n8n if it polls) what
        # happened. The patient does NOT see this — staff replies via Q4 SMS.
        return JSONResponse(content=result, status_code=200)

else:  # pragma: no cover — keep `app` defined for import-time consumers
    app = None  # type: ignore[assignment]


# ─── Smoke test (no network, no real audit DB) ────────────────────────────────
def _smoke_test() -> int:
    """Verify the routing logic without touching Anthropic or the real DB.

    Strategy: feed three synthetic payloads (appointment, file_request, junk),
    short-circuit the classifier with a deterministic stub, and confirm the
    intent → handler dispatch produces the expected stub output for each.
    Exit code 0 = pass, non-zero = fail (so CI can pick it up).
    """
    print("triage-router smoke test")
    print("=" * 60)

    payloads = [
        {
            "name": "appointment intent",
            "verification": {"ok": True, "mode": "delivery"},
            "payload": {
                "from": "+12155550101",
                "to": "+18005550199",
                "text": "Hi can I reschedule my Tuesday appt to next week?",
            },
            "stub_intent": "appointment",
            "expected_intent": "appointment",
        },
        {
            "name": "file_request intent",
            "verification": {"ok": True, "mode": "delivery"},
            "payload": {
                "from": "+12155550102",
                "to": "+18005550199",
                "text": "Could you send me my MRI report from last month?",
            },
            "stub_intent": "file_request",
            "expected_intent": "file_request",
        },
        {
            "name": "patient_lookup intent",
            "verification": {"ok": True, "mode": "delivery"},
            "payload": {
                "from": "+12155550103",
                "to": "+18005550199",
                "text": "Do you have me on file? Last name Garcia.",
            },
            "stub_intent": "patient_lookup",
            "expected_intent": "patient_lookup",
        },
        {
            "name": "general_message fallback (unknown label from classifier)",
            "verification": {"ok": True, "mode": "delivery"},
            "payload": {
                "from": "+12155550104",
                "to": "+18005550199",
                "text": "Thank you so much, you guys are the best!",
            },
            "stub_intent": "compliment",  # not in VALID_INTENTS
            "expected_intent": "general_message",
        },
        {
            "name": "verification failure short-circuits",
            "verification": {"ok": False, "mode": "delivery",
                             "reason": "verification token missing or mismatched"},
            "payload": {
                "from": "+12155550199",
                "to": "+18005550199",
                "text": "doesn't matter — we should never classify this",
            },
            "stub_intent": "should_never_be_called",
            "expected_intent": None,  # short-circuits before classification
        },
        {
            "name": "RC envelope shape — body.changes[*].newMessages[*]",
            "verification": {"ok": True, "mode": "delivery"},
            "payload": {
                "body": {
                    "changes": [
                        {
                            "newMessages": [
                                {
                                    "id": "msg-abc",
                                    "from": {"phoneNumber": "+12155550105"},
                                    "to": [{"phoneNumber": "+18005550199"}],
                                    "subject": "need to cancel friday",
                                }
                            ]
                        }
                    ]
                }
            },
            "stub_intent": "appointment",
            "expected_intent": "appointment",
        },
    ]

    failures: list[str] = []
    classify_calls: list[str] = []

    for case in payloads:
        # Stub classifier: records the body it was given, returns the chosen
        # intent. Exposes the same shape `classify_intent` returns.
        def _stub_classifier(body: str, *, _intent=case["stub_intent"]) -> dict:
            classify_calls.append(body)
            return {
                "intent": _intent,
                "secondary": [],
                "confidence": "high",
                "reason": "stubbed for smoke test",
            }

        before = len(classify_calls)
        result = process_inbound_sms(
            case["payload"],
            verification=case["verification"],
            audit=None,  # dry-run audit (logs to console)
            classifier=_stub_classifier,
        )

        # Verification-failed cases must short-circuit before classification.
        if case["expected_intent"] is None:
            if result.get("ok") is not False:
                failures.append(
                    f"{case['name']}: expected ok=False, got {result!r}"
                )
            if len(classify_calls) != before:
                failures.append(
                    f"{case['name']}: classifier should NOT have been called"
                )
            print(f"  [pass] {case['name']}")
            continue

        if result.get("intent") != case["expected_intent"]:
            failures.append(
                f"{case['name']}: expected intent={case['expected_intent']!r}, "
                f"got {result.get('intent')!r}"
            )
            print(f"  [FAIL] {case['name']}: {result}")
            continue

        # Sanity-check that handler dispatch happened and produced a dict.
        if not isinstance(result.get("handler"), dict):
            failures.append(
                f"{case['name']}: handler result missing or not a dict"
            )
            print(f"  [FAIL] {case['name']}: handler result {result.get('handler')!r}")
            continue

        # RC envelope case must surface the embedded text.
        if case["name"].startswith("RC envelope"):
            if len(classify_calls) == before:
                failures.append(
                    f"{case['name']}: classifier was not called"
                )
                print(f"  [FAIL] {case['name']}: no classify call recorded")
                continue
            last_body = classify_calls[-1]
            if "cancel friday" not in last_body.lower():
                failures.append(
                    f"{case['name']}: classifier got wrong body {last_body!r}"
                )
                print(f"  [FAIL] {case['name']}: body={last_body!r}")
                continue

        print(f"  [pass] {case['name']}  -> intent={result['intent']}")

    # Bonus: confirm classify_intent's JSON parser tolerates a fenced reply.
    fenced = "```json\n{\"intent\": \"appointment\", \"secondary\": [], "\
             "\"confidence\": \"high\", \"reason\": \"reschedule\"}\n```"
    parsed = _safe_parse_intent(fenced)
    if parsed.get("intent") != "appointment":
        failures.append(f"_safe_parse_intent fenced: got {parsed!r}")
    else:
        print("  [pass] _safe_parse_intent strips ```json fences")

    # Bonus: an unknown intent label coerces to general_message at the
    # classify_intent layer (the dispatcher relies on this invariant).
    class _FakeBlock:
        type = "text"
        text = '{"intent": "totally_made_up", "confidence": "high"}'

    class _FakeMsg:
        content = [_FakeBlock()]

    class _FakeClient:
        class messages:  # noqa: N801 — mirrors anthropic SDK shape
            @staticmethod
            def create(**_kwargs):
                return _FakeMsg()

    coerced = classify_intent("ignore me", client=_FakeClient())
    if coerced["intent"] != FALLBACK_INTENT:
        failures.append(
            f"unknown intent coercion: expected {FALLBACK_INTENT}, "
            f"got {coerced!r}"
        )
    else:
        print("  [pass] classify_intent coerces unknown labels to "
              f"{FALLBACK_INTENT}")

    print("=" * 60)
    if failures:
        print(f"FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK — all smoke tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())

# VENDORED from mainlinesurgery-a11y/n8n-office @ backoffice-autopilot-live-20260705, commit eec3888
# Source path: python/integrations/ringcentral_adapter.py
# Re-vendored from the REAL, HAR-verified production repo (do not edit lightly).
# RingCentral SMS/fax/voicemail adapter (webhook verify + send).
#!/usr/bin/env python3
"""
==============================================================================
DEPRECATED — DO NOT PATCH THIS MODULE FOR THE LIVE APP.
==============================================================================
The live n8n-office application routes ALL RingCentral traffic (SMS send +
inbound SMS/fax/voicemail poll) through ``app/comms.py`` — see its
``send_sms`` / ``poll_ringcentral`` functions and the RingCentral JWT token
exchange there. THAT is the implementation to change.

This ``ringcentral_adapter.py`` is a separate, DIVERGENT copy retained only
because the prototype ``python/flows/*`` scripts (file-request / appointment /
triage-router) still import it. It is NOT wired into the running app. Do not
add features or fixes here expecting the app to pick them up — they won't.
The file is intentionally kept (not deleted) so those stray flow imports don't
break; new work belongs in ``app/comms.py``.
==============================================================================

ringcentral_adapter.py — RingCentral SMS + webhook adapter for the n8n office.

Reuses the JWT auth that Antigravity already has working (see
~/Documents/Antigravity/ringcentral_sync.py — `get_access_token()`). We import
that helper directly so credentials and the token-exchange flow live in exactly
one place; if the auth pattern there ever changes, this adapter inherits the
fix for free.

Public surface (what n8n workflows call):

    send_sms(to, body, from_=None)
        Send an outbound SMS. `to` is an E.164 string (e.g. "+12155551234").
        Returns the parsed RingCentral message record.

    verify_webhook(headers, body)
        Validate a webhook delivery from RingCentral. Two modes:
          1. Validation handshake — RingCentral POSTs with a
             `Validation-Token` header; we just need to echo it back. The
             caller checks `result["mode"] == "validation"` and replies with
             header `Validation-Token: <result["validation_token"]>`.
          2. Signed delivery — when the subscription was created with a
             `verificationToken`, RingCentral sends it back on every push in
             the `Verification-Token` header. We compare it constant-time.
        Returns a dict: {"ok": bool, "mode": "validation"|"delivery",
        "validation_token": str|None, "reason": str}.

    subscribe_sms_webhook(callback_url, expires_in=315360000,
                          verification_token=None)
        Create (or replace) a PubNub-free HTTP push subscription for
        `/restapi/v1.0/account/~/extension/~/message-store/instant?type=SMS`.
        `callback_url` MUST be HTTPS and publicly reachable (Tailscale Funnel
        or ngrok — see "Public webhook endpoint" below). Default expiry is the
        RingCentral max (~10 years; the server may cap lower). Returns the
        subscription record (id, status, expirationTime).

Outbound SMS sending also requires the `SMS` scope on the RingCentral app
(in addition to ReadCallLog / ReadMessages already enabled for call/fax sync).
Subscriptions require `Subscriptions` (sometimes shown as `WebhookSubscriptions`
in the console). Add both in the Developer Console → your app → Permissions.

==============================================================================
Public webhook endpoint — recommendation
==============================================================================

RingCentral webhook delivery addresses MUST be HTTPS with a valid cert and a
stable URL (the subscription stores the URL; if it rotates, every push 404s
and the subscription eventually expires).

  Option A — Tailscale Funnel (recommended)
    - Free on personal/solo plans.
    - FIXED URL of the form https://<machine>.<tailnet>.ts.net — survives
      restarts, no token expiry.
    - Valid TLS cert from Let's Encrypt managed by Tailscale.
    - One-time setup:
        tailscale funnel --bg --https=443 http://localhost:5678
      (Where 5678 is the n8n webhook port; adjust to your endpoint.)
    - The resulting URL is what you pass to subscribe_sms_webhook().

  Option B — ngrok
    - Free tier gives you a RANDOM URL each restart — every restart breaks
      the existing subscription and you must re-run subscribe_sms_webhook().
      Paid tier ($8/mo+) gets you a reserved domain.
    - Use only if Tailscale is unavailable or you already have a paid plan.

  Option C — Cloud (Cloudflare Tunnel, Fly.io, etc.)
    - Best for production; out of scope for this adapter (still works the
      same — just pass the HTTPS URL).

==============================================================================
Rate limits, retries, error handling
==============================================================================

RingCentral applies per-endpoint, per-minute "API groups":
  - Heavy   (call-log, message-store list)  ~10 req/min   — used by Antigravity
  - Medium  (SMS send, message read)        ~40 req/min
  - Light   (subscription CRUD)             ~50 req/min
  - Auth    (token exchange)                ~5  req/min
On a 429 the response includes `Retry-After` (seconds). We honor it once with
a single backoff retry; persistent 429s should bubble up so the caller can
slow its workflow (n8n's "Wait" node is the right place to throttle).

Webhook deliveries: RingCentral retries non-2xx pushes up to ~9 times with
exponential backoff (~1s → ~10 min), then disables the subscription. ALWAYS
return 200 fast (queue the work; don't process inline) and ALWAYS echo the
Validation-Token on the handshake POST or the subscription is rejected.

==============================================================================
"""

from __future__ import annotations

import hmac
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional


# ---------------------------------------------------------------------------
# Auth — import the Antigravity helper instead of re-implementing it.
# ---------------------------------------------------------------------------

_ANTIGRAVITY_DIR = os.path.expanduser("~/Documents/Antigravity")


def _load_antigravity_auth():
    """Import `get_access_token` and `RC_SERVER` from the existing
    ringcentral_sync.py without polluting sys.modules with a real package.
    Uses importlib so we don't depend on the Antigravity dir being on
    PYTHONPATH and don't trigger its `if __name__ == '__main__'` block."""
    src = os.path.join(_ANTIGRAVITY_DIR, "ringcentral_sync.py")
    if not os.path.exists(src):
        raise RuntimeError(
            f"Cannot find Antigravity RingCentral auth at {src}. "
            "Set ANTIGRAVITY_DIR or check the repo path."
        )
    spec = importlib.util.spec_from_file_location("_antigravity_rc_sync", src)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to build import spec for {src}")
    mod = importlib.util.module_from_spec(spec)
    # The Antigravity module calls _load_dotenv() at import time using its own
    # __file__ to find .env — that's exactly what we want, so just load it.
    spec.loader.exec_module(mod)
    return mod


# Loaded lazily on first call so importing this module is cheap and doesn't
# require the Antigravity repo to be present until you actually make a call.
_rc_sync = None


def _auth():
    global _rc_sync
    if _rc_sync is None:
        _rc_sync = _load_antigravity_auth()
    return _rc_sync


def _server() -> str:
    return _auth().RC_SERVER


def _token() -> str:
    return _auth().get_access_token()


# ---------------------------------------------------------------------------
# HTTP helper with one-shot 429 backoff.
# ---------------------------------------------------------------------------


def _request(
    method: str,
    path: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    body: Any = None,
    timeout: int = 30,
    _retry: bool = True,
) -> dict:
    url = path if path.startswith("http") else f"{_server()}{path}"
    hdrs = {"Authorization": f"Bearer {_token()}"}
    if headers:
        hdrs.update(headers)

    if body is None:
        data = None
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {}
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                return json.loads(raw.decode("utf-8"))
            return {"_raw": raw, "_content_type": ct}
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:
            wait = int(e.headers.get("Retry-After", "1") or "1")
            time.sleep(min(wait, 60))
            return _request(method, path, headers=headers, body=body,
                            timeout=timeout, _retry=False)
        # Surface the upstream error body so callers (and n8n) can log it.
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise RuntimeError(
            f"RingCentral {method} {url} -> {e.code} {e.reason}: {err_body}"
        ) from e


# ---------------------------------------------------------------------------
# Outbound SMS
# ---------------------------------------------------------------------------


def send_sms(to: str, body: str, from_: Optional[str] = None) -> dict:
    """Send a single outbound SMS via the authenticated extension.

    `to`    — recipient phone in E.164 (e.g. "+12155551234"). A bare 10-digit
              US number is also accepted; we'll prefix "+1".
    `body`  — message text (max 1000 chars; RC will split into multipart).
    `from_` — sender DID (E.164). If omitted, RingCentral uses the extension's
              default SMS-enabled number. Required if the extension has
              multiple SMS numbers — RC returns 400 otherwise.

    Required scope: SMS. Returns the RC message record (includes `id` and
    `messageStatus` — usually "Queued" immediately; poll the message store or
    subscribe to a message-store webhook to watch it transition to "Sent").
    """
    if not to or not body:
        raise ValueError("send_sms requires both `to` and `body`")
    if not to.startswith("+"):
        digits = "".join(ch for ch in to if ch.isdigit())
        if len(digits) == 10:
            to = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            to = f"+{digits}"
        else:
            raise ValueError(f"Cannot normalize recipient {to!r} to E.164")

    payload: dict = {"to": [{"phoneNumber": to}], "text": body}
    if from_:
        payload["from"] = {"phoneNumber": from_}

    return _request(
        "POST",
        "/restapi/v1.0/account/~/extension/~/sms",
        body=payload,
    )


# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------


def _get_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup that tolerates dict, Headers, or list."""
    if hasattr(headers, "get"):
        # Try a few common spellings before doing a full scan.
        for variant in (name, name.lower(), name.upper(), name.title()):
            value = headers.get(variant)
            if value is not None:
                return value
    target = name.lower()
    try:
        items = headers.items()
    except AttributeError:
        items = headers
    for key, value in items:
        if str(key).lower() == target:
            return value
    return None


def verify_webhook(
    headers: Mapping[str, str],
    body: Optional[bytes] = None,
    *,
    expected_token: Optional[str] = None,
) -> dict:
    """Validate a webhook request from RingCentral.

    RingCentral has two flavors of "verification" the caller must handle:

      1. Validation handshake (one-time, on subscription create/renew):
         RC POSTs to the callback URL with header `Validation-Token: <token>`
         and an empty body. The HTTP response MUST echo that token in either
         the `Validation-Token` header or the body within 5 seconds, else the
         subscription is rejected. Our caller looks at
         `result["mode"] == "validation"` and replies accordingly.

      2. Per-delivery verification token (optional but recommended): if the
         subscription was created with `deliveryMode.verificationToken`, RC
         echoes that token in the `Verification-Token` header on every push.
         We compare it constant-time against `expected_token` (defaults to the
         `RC_WEBHOOK_VERIFICATION_TOKEN` env var).

    `body` is accepted but currently unused — RingCentral does NOT sign the
    payload with HMAC (unlike Stripe/GitHub); the verification token IS the
    only built-in integrity signal. The parameter is kept so callers can pass
    it without branching, and so a future RC signature scheme can slot in
    without breaking the signature.

    Returns {"ok": bool, "mode": "validation"|"delivery",
             "validation_token": str|None, "reason": str}.
    """
    validation_token = _get_header(headers, "Validation-Token")
    if validation_token:
        return {
            "ok": True,
            "mode": "validation",
            "validation_token": validation_token,
            "reason": "handshake — echo Validation-Token back in the response",
        }

    if expected_token is None:
        expected_token = os.environ.get("RC_WEBHOOK_VERIFICATION_TOKEN", "")

    presented = _get_header(headers, "Verification-Token") or ""
    if not expected_token:
        # No verification configured — accept but flag it so callers can warn.
        return {
            "ok": True,
            "mode": "delivery",
            "validation_token": None,
            "reason": "no expected token configured; delivery accepted unverified",
        }

    if presented and hmac.compare_digest(presented, expected_token):
        return {
            "ok": True,
            "mode": "delivery",
            "validation_token": None,
            "reason": "verification token matched",
        }

    return {
        "ok": False,
        "mode": "delivery",
        "validation_token": None,
        "reason": "verification token missing or mismatched",
    }


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


# Inbound SMS lives in the same message-store/instant event-filter family as
# inbound MMS/fax/voicemail — narrowing with `?type=SMS` keeps the firehose
# small. If you also want MMS, add a second filter with `?type=MMS`.
SMS_EVENT_FILTER = (
    "/restapi/v1.0/account/~/extension/~/message-store/instant?type=SMS"
)


def subscribe_sms_webhook(
    callback_url: str,
    *,
    expires_in: int = 315_360_000,
    verification_token: Optional[str] = None,
    extra_filters: Optional[list] = None,
) -> dict:
    """Create an inbound-SMS webhook subscription delivered to `callback_url`.

    `callback_url` MUST be HTTPS and publicly reachable. The RingCentral
    backend immediately POSTs a Validation-Token handshake to that URL — if it
    doesn't get a 200 with the echoed token in 5s, this call raises with the
    upstream error. See the "Public webhook endpoint" section at the top of
    this file for the Tailscale Funnel recipe.

    `expires_in` is the max subscription lifetime in seconds. RC caps this
    server-side (current cap is ~10 years for HTTP-push subs; if you pass a
    larger value the server clamps it silently). For short-lived dev, pass
    something like 3600.

    `verification_token` is an optional shared secret RC will echo on every
    delivery in the `Verification-Token` header. Pass the same value to
    `verify_webhook(..., expected_token=...)` (or set
    RC_WEBHOOK_VERIFICATION_TOKEN in the env). If omitted, deliveries are not
    verifiable — fine for dev, NOT fine for prod.

    Returns the subscription record, including the `id` you'll need to
    delete/renew it later.
    """
    if not callback_url.lower().startswith("https://"):
        raise ValueError(
            f"callback_url must be HTTPS, got {callback_url!r}. "
            "Use a Tailscale Funnel URL (https://<host>.<tailnet>.ts.net) "
            "or a paid ngrok reserved domain."
        )

    filters = [SMS_EVENT_FILTER]
    if extra_filters:
        filters.extend(extra_filters)

    delivery: dict = {
        "transportType": "WebHook",
        "address": callback_url,
    }
    if verification_token:
        delivery["verificationToken"] = verification_token

    payload = {
        "eventFilters": filters,
        "deliveryMode": delivery,
        "expiresIn": expires_in,
    }
    return _request("POST", "/restapi/v1.0/subscription", body=payload)


def list_subscriptions() -> dict:
    """List all subscriptions for the current account/extension. Useful when
    a callback URL changes and you need to find the stale subscription id to
    delete."""
    return _request("GET", "/restapi/v1.0/subscription")


def delete_subscription(subscription_id: str) -> None:
    """Cancel a subscription by id. Idempotent — a 404 is swallowed so this
    is safe to call during cleanup."""
    try:
        _request("DELETE", f"/restapi/v1.0/subscription/{subscription_id}")
    except RuntimeError as e:
        if "404" in str(e):
            return
        raise


# ---------------------------------------------------------------------------
# CLI — quick smoke tests so n8n operators can sanity-check without writing
# a workflow first.
# ---------------------------------------------------------------------------


def _cli(argv):
    import argparse

    p = argparse.ArgumentParser(description="RingCentral adapter smoke tests")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send-sms", help="Send a single SMS")
    s.add_argument("--to", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--from", dest="from_", default=None)

    sub_ = sub.add_parser("subscribe", help="Create an inbound-SMS webhook subscription")
    sub_.add_argument("--callback", required=True, help="HTTPS public URL")
    sub_.add_argument("--token", default=None,
                      help="Optional verification token RC will echo on every push")
    sub_.add_argument("--expires-in", type=int, default=315_360_000)

    sub.add_parser("list", help="List existing subscriptions")

    d = sub.add_parser("delete", help="Delete a subscription by id")
    d.add_argument("--id", required=True)

    args = p.parse_args(argv)
    if args.cmd == "send-sms":
        out = send_sms(args.to, args.body, from_=args.from_)
    elif args.cmd == "subscribe":
        out = subscribe_sms_webhook(
            args.callback,
            expires_in=args.expires_in,
            verification_token=args.token,
        )
    elif args.cmd == "list":
        out = list_subscriptions()
    elif args.cmd == "delete":
        delete_subscription(args.id)
        out = {"ok": True, "deleted": args.id}
    else:  # pragma: no cover — argparse enforces this
        raise SystemExit(f"unknown command {args.cmd}")
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _cli(sys.argv[1:])

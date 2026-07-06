"""
app/server.py — FastAPI surface for the back-office assistant.

Thin HTTP layer. All real work lives in the sibling modules
(``agent``, ``approvals``, ``referrals``, ``manager``, ``audit``, ``auth``);
this file only handles auth/session plumbing, request/response shaping, and
routing.

AUTH MODEL
  - A single ``apw_session`` cookie (HttpOnly, SameSite=Lax, ``secure`` only
    in prod) carries an opaque bearer token resolved by ``auth``.
  - Every ``/api/*`` route except ``/api/login`` and ``/api/health`` requires
    a live session; otherwise a 401 JSON body is returned (never an HTML
    redirect — this is an API-first app).

HONESTY NOTES
  - Login failures never say WHICH of username/password was wrong.
  - The rate limiter is best-effort in-memory (per-process); it protects a
    local single-instance deployment, not a horizontally scaled one — and we
    don't pretend otherwise.
  - No PHI is written to logs here; only actor usernames, action kinds, and
    outcomes reach ``audit`` / stdout.

Run: ``python3 -m app.server`` (see ``main()`` for --port/--host/--create-user).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
import time
from collections import deque
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import SETTINGS
from app import (db, audit, auth, approvals, agent, referrals, referral_audit,
                 manager, comms, scheduler, request_age)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

COOKIE_NAME = "apw_session"
_HISTORY_TURNS = 20
_LOGIN_RATE_LIMIT = 10  # attempts
_LOGIN_RATE_WINDOW = 60  # seconds

# Valid referral statuses (mirrors the frontend's status <select>). Anything
# else is rejected so a typo/injected value can't wedge the referral row.
_REFERRAL_STATUSES = {"new", "working", "booked", "declined"}

# Valid inbox-message statuses a human may set from the UI. Anything else is
# rejected so a typo/injected value can't wedge the message row.
_MESSAGE_STATUSES = {"new", "triaged", "replied", "archived", "sent"}

# In-memory, per-process login attempt log keyed by client IP. Best-effort;
# resets on restart. deque of monotonic timestamps per IP.
_login_attempts: dict[str, deque] = {}


app = FastAPI(title="Atlantic Back-Office Assistant", docs_url=None,
              redoc_url=None)

_STATIC_DIR = SETTINGS.BASE_DIR / "app" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)),
              name="static")


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    logger.info(
        "server ready · provider=%s · phi_safe=%s · emr=%s",
        SETTINGS.CHAT_PROVIDER, SETTINGS.PHI_SAFE_LLM, SETTINGS.EMR_ENABLED,
    )
    # Launch the in-app scheduler as a background task on THIS event loop, so
    # the daily manager / referral-audit coroutines share the loop the app
    # already uses (their asyncio locks assume it). scheduler.run() returns
    # immediately when APP_SCHEDULER=0, so the task simply completes at once.
    # The handle is stashed on app.state so _shutdown can cancel it cleanly.
    app.state.scheduler_task = asyncio.ensure_future(scheduler.run())


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Cancel the scheduler task and wait for it to unwind on shutdown.

    ``scheduler.run()`` re-raises ``CancelledError`` when cancelled (the correct
    asyncio contract), so we must catch it EXPLICITLY: it is a ``BaseException``,
    NOT an ``Exception``, so a bare ``except Exception`` would let it escape and
    surface as an error in the ASGI shutdown lifespan. We swallow it (plus any
    late loop error) so shutdown always completes cleanly.
    """
    task = getattr(app.state, "scheduler_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # a late loop error must not wedge shutdown
        logger.warning("scheduler task ended with error on shutdown: %s", exc)


# ---------------------------------------------------------------------------
# Auth plumbing
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """Best-effort real client IP.

    Behind the TLS reverse proxy the deploy docs mandate (app bound to
    127.0.0.1), ``request.client.host`` is always the proxy (127.0.0.1), which
    would collapse every client into one shared rate-limit bucket. Prefer the
    FIRST hop of ``X-Forwarded-For`` (the original client the proxy recorded)
    when present, so per-client accounting survives the proxy.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _over_login_limit(ip: str) -> bool:
    """Return True if *ip* has too many recent FAILED logins (no mutation).

    Prunes expired timestamps as a read side effect but does NOT record a new
    attempt — successful logins must not consume the budget. Failures are
    counted separately via ``_record_failed_login``.
    """
    now = time.monotonic()
    bucket = _login_attempts.setdefault(ip, deque())
    cutoff = now - _LOGIN_RATE_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return len(bucket) >= _LOGIN_RATE_LIMIT


def _record_failed_login(ip: str) -> None:
    """Charge one FAILED login attempt against *ip*'s window."""
    bucket = _login_attempts.setdefault(ip, deque())
    bucket.append(time.monotonic())


def current_user(request: Request) -> str:
    """FastAPI dependency: resolve the session cookie or raise 401."""
    token = request.cookies.get(COOKIE_NAME)
    username = auth.get_session_user(token)
    if not username:
        raise HTTPException(
            status_code=401, detail="authentication required"
        )
    return username


def require_approver(user: str = Depends(current_user)) -> str:
    """Dependency: require an APPROVER_ROLES member (else 403).

    Guards the two authority endpoints — approval decisions (which EXECUTE EMR
    writes) and manager runs. A plain 'staff' account can queue cards via chat
    but cannot approve them, so the human-in-the-loop choke point stays a real
    second party and a card's requester cannot rubber-stamp their own request.
    """
    role = (auth.get_user_role(user) or "staff").strip().lower()
    if role not in SETTINGS.APPROVER_ROLES:
        raise HTTPException(
            status_code=403,
            detail="approver role required (manager/admin)",
        )
    return user


@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException):
    """Force JSON (never HTML) for API errors like the 401 above."""
    return JSONResponse(
        status_code=exc.status_code, content={"error": exc.detail}
    )


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.post("/api/login")
async def api_login(request: Request, response: Response):
    ip = _client_ip(request)
    if _over_login_limit(ip):
        # Actor is the client IP only — never the (possibly mistyped) username.
        audit.log(ip, "auth", "login", "rate_limited", outcome="blocked")
        raise HTTPException(status_code=429, detail="too many attempts")

    try:
        body = await request.json()
    except Exception:
        body = {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not auth.verify_password(username, password):
        _record_failed_login(ip)
        # SECURITY: never persist the submitted username as the audit actor —
        # a user who fat-fingers their password into the username box would
        # otherwise store that password in plaintext and expose it via
        # /api/home. Attribute failures to the client IP only.
        audit.log(ip, "auth", "login", "bad_credentials", outcome="denied")
        raise HTTPException(status_code=401, detail="invalid credentials")

    raw_token = auth.create_session(username)
    role = auth.get_user_role(username) or "staff"
    response.set_cookie(
        key=COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=SETTINGS.SECURE_COOKIES,
        max_age=SETTINGS.SESSION_TTL_HOURS * 3600,
        path="/",
    )
    audit.log(username, "auth", "login", outcome="ok")
    return {"ok": True, "username": username, "role": role}


@app.post("/api/logout")
async def api_logout(request: Request, response: Response,
                     user: str = Depends(current_user)):
    token = request.cookies.get(COOKIE_NAME)
    auth.destroy_session(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    audit.log(user, "auth", "logout", outcome="ok")
    return {"ok": True}


@app.get("/api/me")
async def api_me(user: str = Depends(current_user)):
    return {"username": user, "role": auth.get_user_role(user) or "staff"}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def api_chat(request: Request, user: str = Depends(current_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    # get_conn() opens a fresh connection; the caller owns its lifecycle. Close
    # it in finally so sustained chat/home polling doesn't leak connection
    # objects + WAL file handles or hold read locks (audit #14 — matches the
    # try/finally pattern in approvals.decide / audit.log).
    conn = db.get_conn()
    try:
        ts = _now_iso()
        with conn:
            conn.execute(
                "INSERT INTO conversations (ts, username, role, content) "
                "VALUES (?, ?, ?, ?)",
                (ts, user, "user", message),
            )

        history = _load_history(user)
        result = await agent.run_chat(user, message, history)

        with conn:
            conn.execute(
                "INSERT INTO conversations (ts, username, role, content) "
                "VALUES (?, ?, ?, ?)",
                (_now_iso(), user, "assistant", result.get("reply", "")),
            )
        return result
    finally:
        conn.close()


def _load_history(user: str) -> list[dict]:
    """Return the last ``_HISTORY_TURNS`` turns (oldest-first) for *user*.

    Reads one extra row and drops it so the just-inserted user message isn't
    replayed as history to the agent.

    Robustness: the transcript can contain an UNPAIRED user row if a prior
    ``/api/chat`` crashed after inserting the user turn but before the
    assistant turn. Without care, once the window scrolls past that gap the
    replayed history could begin with an assistant turn — which the Anthropic
    Messages API rejects (first message must be user-role), breaking every
    subsequent turn. So we drop any LEADING assistant turns; the replayed
    history always starts with a user turn (or is empty).
    """
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM conversations WHERE username = ? "
            "ORDER BY id DESC LIMIT ?",
            (user, _HISTORY_TURNS + 1),
        ).fetchall()
    finally:
        conn.close()
    turns = [{"role": r["role"], "content": r["content"]}
             for r in reversed(rows)]
    turns = turns[:-1] if turns else []
    # Trim leading assistant turns so the window can't start assistant-first.
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

@app.get("/api/approvals")
async def api_approvals(status: Optional[str] = None,
                        user: str = Depends(current_user)):
    """Approval cards for the given status filter.

    ``approvals`` stays the FULL list for the requested filter — unchanged
    contract, so the Approvals tab (the management view) still shows every
    pending card, legacy ones included. ``legacy`` is an ADDITIVE annotated
    sub-view: the subset of pending cards whose REAL source time is > 48h old
    (see ``request_age``). It is only populated for the pending view
    (unfiltered or ``status=pending``) and is ``[]`` for every other status, so
    a caller can rely on the key without it ever double-counting decided cards.
    Nothing is removed from ``approvals`` — the split lives on the Home
    dashboard (``/api/home``), not here.
    """
    # High limit: the current/legacy split must run over the FULL pending set,
    # not list_approvals' default page of 50 — otherwise a backlog of >50 recent
    # legacy cards fills the window and the older current cards vanish.
    cards = approvals.list_approvals(status=status, limit=5000)
    if status in (None, "pending"):
        _current, legacy = request_age.split_pending(cards)
        return {"approvals": cards, "legacy": legacy}
    return {"approvals": cards, "legacy": []}


@app.post("/api/approvals/{approval_id}/decision")
async def api_approval_decision(approval_id: int, request: Request,
                                user: str = Depends(require_approver)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if "approve" not in body:
        raise HTTPException(status_code=400, detail="approve (bool) required")
    approve = bool(body.get("approve"))
    note = body.get("note") or ""
    try:
        card = await approvals.decide(
            approval_id, approve, decided_by=user, note=note
        )
    except (ValueError, KeyError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return card


@app.post("/api/approvals/{approval_id}/verify-need")
async def api_approval_verify_need(approval_id: int,
                                   user: str = Depends(current_user)):
    """On-demand: cross-reference this card against LIVE EMR data and report
    whether it still needs attention. Read-only (a live lookup, no EMR write),
    so it sits behind normal session auth, not require_approver. Slow by nature
    (one combined search + a bounded calendar scan) — the UI shows a spinner."""
    from app import verify_need as _vn

    card = approvals.get_approval(approval_id)
    if not card:
        raise HTTPException(status_code=404, detail="approval not found")
    try:
        return await _vn.verify_need(card)
    except Exception as exc:  # never 500 on a best-effort check
        logger.error("verify_need failed for #%s: %s", approval_id, exc)
        return {"verdict": "error", "detail": f"check failed: {exc}"}


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------

@app.get("/api/referrals")
async def api_referrals(status: Optional[str] = None,
                        user: str = Depends(current_user)):
    return {"referrals": referrals.list_referrals(status=status)}


@app.post("/api/referrals/{referral_id}/update")
async def api_referral_update(referral_id: int, request: Request,
                              user: str = Depends(current_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = body.get("status")
    if not status:
        raise HTTPException(status_code=400, detail="status required")
    if status not in _REFERRAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status; must be one of "
                   f"{sorted(_REFERRAL_STATUSES)}",
        )
    note = body.get("note") or ""
    # HONESTY: update_referral returns None for an unknown id — do NOT claim
    # success (or write an 'ok' audit row) for work that did not happen.
    updated = referrals.update_referral(referral_id, status, note=note)
    if updated is None:
        audit.log(user, "referral", "update",
                  {"id": referral_id, "status": status},
                  outcome="not_found")
        raise HTTPException(
            status_code=404, detail=f"referral {referral_id} not found"
        )
    audit.log(user, "referral", "update",
              {"id": referral_id, "status": status}, outcome="ok")
    return {"ok": True}


@app.post("/api/referrals/audit")
async def api_referrals_audit(request: Request,
                              user: str = Depends(current_user)):
    # Referral audit: check referrals against BOTH EMRs and raise a manual-task
    # approval card for any patient not yet on the schedule. Any staff member
    # may run an audit (it only QUEUES cards for approval — it never executes an
    # EMR write), so this is behind normal session auth, NOT require_approver.
    #
    # SLOW ROUTE: audit_referrals / scan_gmail_and_audit drive the live EMR
    # scraper (combined_search + a multi-day Svigg calendar scrape), which can
    # take a minute. Those coroutines run on the shared EMR event loop, so we
    # AWAIT them directly here (never asyncio.to_thread — that would run them on
    # a worker thread with a different loop and break the manager's asyncio
    # locks). A slow response is acceptable for this endpoint.
    try:
        body = await request.json()
    except Exception:
        body = {}

    window_days = body.get("window_days")
    try:
        window_days = int(window_days) if window_days is not None else 21
    except (TypeError, ValueError):
        window_days = 21

    if body.get("scan_gmail"):
        report = await referral_audit.scan_gmail_and_audit(
            user, since_days=window_days, appt_window_days=window_days
        )
    else:
        referral_list = body.get("referrals")
        if not isinstance(referral_list, list):
            referral_list = []
        report = await referral_audit.audit_referrals(
            referral_list, user, appt_window_days=window_days
        )

    audit.log(user, "referral_audit", "run",
              {"window_days": window_days,
               "scan_gmail": bool(body.get("scan_gmail"))},
              outcome="ok")
    return report


@app.post("/api/referrals/ingest")
async def api_referrals_ingest(user: str = Depends(current_user)):
    # ingest_all() does SYNCHRONOUS work — sqlite reads/writes and, when the
    # Google Sheet arm is configured, a blocking googleapiclient HTTP round
    # trip (seconds to minutes on a slow network). Running it inline would
    # freeze the single event loop for every other request (in-flight chat,
    # approvals, health). Offload it to a worker thread so the loop stays free.
    import asyncio
    result = await asyncio.to_thread(referrals.ingest_all)
    audit.log(user, "referral", "ingest", result, outcome="ok")
    return result


# ---------------------------------------------------------------------------
# Messages (shared email/SMS inbox)
# ---------------------------------------------------------------------------

@app.get("/api/messages")
async def api_messages(channel: Optional[str] = None,
                       status: Optional[str] = None,
                       limit: int = 50,
                       user: str = Depends(current_user)):
    return {
        "messages": comms.list_messages(
            channel=channel, status=status, limit=limit
        )
    }


@app.post("/api/messages/poll")
async def api_messages_poll(user: str = Depends(current_user)):
    # poll_all does SYNCHRONOUS network I/O (IMAP + RingCentral round trips);
    # offload to a worker thread so the single event loop isn't frozen for
    # every other request while a mailbox is polled. Each arm is independently
    # try/excepted inside comms, so this never raises — 'not configured' /
    # 'error: …' come back as data.
    import asyncio
    result = await asyncio.to_thread(comms.poll_all)
    audit.log(user, "message", "poll", result, outcome="ok")
    return result


@app.post("/api/messages/{message_id}/update")
async def api_message_update(message_id: int, request: Request,
                             user: str = Depends(current_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = body.get("status")
    if not status:
        raise HTTPException(status_code=400, detail="status required")
    if status not in _MESSAGE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status; must be one of "
                   f"{sorted(_MESSAGE_STATUSES)}",
        )
    # HONESTY: update_message returns False for an unknown id — do NOT claim
    # success (or write an 'ok' audit row) for work that did not happen.
    updated = comms.update_message(message_id, status)
    if not updated:
        audit.log(user, "message", "update",
                  {"id": message_id, "status": status},
                  outcome="not_found")
        raise HTTPException(
            status_code=404, detail=f"message {message_id} not found"
        )
    audit.log(user, "message", "update",
              {"id": message_id, "status": status}, outcome="ok")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Home dashboard + manager
# ---------------------------------------------------------------------------

@app.get("/api/home")
async def api_home(user: str = Depends(current_user)):
    reports = manager_latest_report()
    # Split the pending queue on real source age (see request_age). ``pending``
    # is the LIVE queue (current, <=48h or unknown-age fail-safe); ``legacy`` is
    # the aged backlog surfaced in its own collapsible box on the dashboard so
    # it never crowds out fresh requests. Both are honest subsets of the real
    # pending rows — no card is invented and none is dropped (current + legacy
    # == all pending).
    # High limit so the split sees ALL pending, not list_approvals' default 50
    # (a >50 legacy backlog would otherwise hide every current card).
    pending_all = approvals.list_approvals(status="pending", limit=5000)
    pending_current, pending_legacy = request_age.split_pending(pending_all)
    return {
        "pending": pending_current,
        "legacy": pending_legacy,
        "referrals_new": referrals.list_referrals(status="new"),
        "inbox_new": _inbox_new(),
        "manager_report": reports,
        "audit": audit.recent(30),
    }


def _inbox_new(limit: int = 10) -> list:
    """New INBOUND inbox messages for the home dashboard (newest first).

    list_messages has no direction filter, so fetch status='new' and keep only
    inbound rows here, capped at *limit*. Kept local so /api/home stays a pure
    read (never polls a mailbox).
    """
    rows = comms.list_messages(status="new", limit=max(limit * 3, limit))
    inbound = [r for r in rows if (r.get("direction") == "in")]
    return inbound[:limit]


def manager_latest_report() -> Optional[dict]:
    """Return the most recent stored manager report, or ``None``.

    Kept local (not in ``manager``) so ``/api/home`` never triggers an LLM
    call — it only surfaces what already exists.
    """
    import json
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT report FROM manager_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return json.loads(row["report"])
    except (TypeError, json.JSONDecodeError):
        return None


@app.post("/api/manager/run")
async def api_manager_run(user: str = Depends(require_approver)):
    report = await manager.run_manager()
    audit.log(user, "manager", "run", outcome="ok")
    return report


# ---------------------------------------------------------------------------
# Health (no auth) + index
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def api_health():
    return {
        "ok": True,
        "emr_enabled": SETTINGS.EMR_ENABLED,
        "provider": SETTINGS.CHAT_PROVIDER,
        "phi_safe": SETTINGS.PHI_SAFE_LLM,
        "scheduler": scheduler.get_status(),
    }


@app.get("/")
async def index():
    index_path = _STATIC_DIR / "index.html"
    if not index_path.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": "index.html not found — build the frontend"},
        )
    return FileResponse(str(index_path))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Atlantic back-office app")
    parser.add_argument("--port", type=int, default=SETTINGS.APP_PORT)
    parser.add_argument("--host", default=SETTINGS.HOST)
    parser.add_argument("--create-user", metavar="USERNAME",
                        help="provision a user (prompts for password) & exit")
    parser.add_argument("--role", default="staff",
                        help="role for --create-user (default: staff)")
    parser.add_argument("--delete-user", metavar="USERNAME",
                        help="offboard a user: remove them & purge their "
                             "sessions, then exit")
    args = parser.parse_args()

    db.init_db()

    if args.delete_user:
        removed = auth.delete_user(args.delete_user)
        if removed:
            print(f"deleted user {args.delete_user} and purged sessions")
        else:
            print(f"no such user {args.delete_user}; purged any sessions")
        return

    if args.create_user:
        pw = getpass.getpass(f"password for {args.create_user}: ")
        pw2 = getpass.getpass("confirm password: ")
        if pw != pw2:
            print("passwords did not match", file=sys.stderr)
            sys.exit(1)
        try:
            rec = auth.create_user(args.create_user, pw, role=args.role)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"created user {rec['username']} (role={rec['role']})")
        return

    import uvicorn

    # Dual-stack LOOPBACK: when bound to loopback, serve BOTH 127.0.0.1 (IPv4)
    # and ::1 (IPv6) so the browser reaches the app whether `localhost` resolves
    # to an A or AAAA record (Chrome often tries ::1 first and shows
    # ERR_CONNECTION_REFUSED if only IPv4 is bound). This stays loopback-only —
    # no external network exposure. uvicorn.run binds a single host, so we build
    # both loopback sockets ourselves and hand them to Server.serve.
    if args.host in ("127.0.0.1", "localhost", "::1"):
        import asyncio
        import socket

        sockets = []
        for family, laddr in ((socket.AF_INET, ("127.0.0.1", args.port)),
                              (socket.AF_INET6, ("::1", args.port))):
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                sock.bind(laddr)
                sock.listen(128)
                sock.set_inheritable(True)
                sockets.append(sock)
            except OSError as exc:
                logger.warning("scheduler bind %s skipped: %s", laddr, exc)
        if not sockets:  # both failed — fall back to the plain single-host run
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
            return
        config = uvicorn.Config(app, log_level="info")
        server = uvicorn.Server(config)
        asyncio.run(server.serve(sockets=sockets))
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

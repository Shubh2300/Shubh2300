#!/usr/bin/env python3
"""
verify_app.py — OFFLINE end-to-end smoke test for the back-office web app.

Runs the real FastAPI server (``python3 -m app.server``) on a throwaway port
with EMR and LLM effectively disabled, then drives the HTTP API with stdlib
``urllib`` only. NO EMR calls, NO real LLM calls, NO third-party deps.

Why it copies app/data
----------------------
``app/config.py`` fixes ``DATA_DIR = BASE_DIR/app/data`` with no env override,
so we cannot point the app at a scratch database. To avoid clobbering any real
users / sqlite db, this script snapshots ``app/data`` to a temp backup before
the run and restores it byte-for-byte in ``finally`` — even on crash / Ctrl-C.
The server is always killed in ``finally`` too.

HONESTY NOTES
  - We assert only what the contract promises. The LLM provider may be
    ``none`` in this environment; test 6 therefore checks that ``/api/chat``
    returns a *string* reply (HTTP 200), not any particular content — it must
    never fabricate an answer or 500 when no provider is configured.
  - EMR is forced off via ``EMR_ENABLED=0`` so no scraper / portal is touched.
  - Exit code is 0 only if every test passes; the SUMMARY line is the
    machine-readable result for the orchestrator.

Usage:  cd /Users/shubh/n8n-office && python3 verify_app.py
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "app" / "data"
PORT = 8788
HOST = "127.0.0.1"
BASE_URL = f"http://{HOST}:{PORT}"

TEST_USER = "smoketest"
TEST_PASS = "Sm0ke-Test-pw!"
# Approver-role user: EMR-write approvals + manager runs require an approver
# role (manager/admin). The plain 'staff' TEST_USER must be FORBIDDEN (403)
# from those endpoints — the human-in-the-loop choke point is role-gated.
APPROVER_USER = "smokemgr"
APPROVER_PASS = "Sm0ke-Mgr-pw!"

# ---------------------------------------------------------------------------
# tiny test harness
# ---------------------------------------------------------------------------

_results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    flag = "PASS" if ok else "FAIL"
    line = f"[{flag}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only; a shared cookie jar carries the session cookie)
# ---------------------------------------------------------------------------

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookie_jar)
)


def _request(method: str, path: str, body: dict | None = None,
             timeout: float = 15.0):
    """Return (status_code, headers, text). Never raises on 4xx/5xx."""
    url = BASE_URL + path
    data = None
    headers = {"Accept": "*/*"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.getcode(), dict(resp.headers), resp.read().decode(
                "utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode(
            "utf-8", "replace")


def _json_or_none(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _wait_for_health(proc: subprocess.Popen, deadline: float) -> bool:
    """Poll /api/health until 200 or the process dies or we time out."""
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            code, _, _ = _request("GET", "/api/health", timeout=2.0)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# data-dir snapshot / restore
# ---------------------------------------------------------------------------

def snapshot_data_dir() -> Path | None:
    """Copy app/data to a temp dir; return the backup path (or None)."""
    if not DATA_DIR.exists():
        return None
    backup = Path(tempfile.mkdtemp(prefix="verify_app_data_"))
    dest = backup / "data"
    shutil.copytree(DATA_DIR, dest)
    return backup


def restore_data_dir(backup: Path | None) -> None:
    """Restore app/data exactly from *backup* (or clear it if none existed)."""
    if backup is None:
        # app/data did not exist before the run — remove what the run created
        if DATA_DIR.exists():
            shutil.rmtree(DATA_DIR, ignore_errors=True)
        return
    src = backup / "data"
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    shutil.copytree(src, DATA_DIR)
    shutil.rmtree(backup, ignore_errors=True)


# ---------------------------------------------------------------------------
# server lifecycle
# ---------------------------------------------------------------------------

def _child_env() -> dict:
    env = dict(os.environ)
    env["EMR_ENABLED"] = "0"
    env["APP_ENV"] = "dev"          # plain-http cookies for the test client
    env["APP_PORT"] = str(PORT)
    env["APP_HOST"] = HOST
    # Keep the in-app scheduler OFF for the whole smoke suite: a test run must
    # never fire real comms polls / manager summaries / referral audits. With
    # APP_SCHEDULER=0, scheduler.run() returns immediately and get_status()
    # reports enabled=false — which check 11 asserts on /api/health.
    env["APP_SCHEDULER"] = "0"
    # This is an OFFLINE smoke suite: neutralise any Gmail / RingCentral creds
    # so the comms arms degrade to 'not configured' and 10a's precondition ("no
    # real mail/telephony creds") is ESTABLISHED by the harness — not an
    # accident of whichever .env the box happens to carry. Without this, a box
    # with real creds would do a live IMAP login / RC token exchange mid-test
    # (pulling real patient messages into the smoke DB) and 10a would go red on
    # a healthy box.
    #
    # We set each key to "" rather than pop it: config._load_env_file is
    # non-destructive ("key in os.environ" wins), so a bare pop would let a
    # .env in the app dir re-populate the cred. Presence-with-empty-value both
    # clears any inherited value AND blocks .env from re-adding it, and the
    # _{gmail,rc}_configured predicates treat "" as unconfigured.
    for key in ("GMAIL_ACCOUNTS", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
                "RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT",
                "RC_FROM_NUMBER"):
        env[key] = ""
    # Do NOT touch LLM keys — provider may legitimately be 'none' here.
    return env


def create_user(username: str = TEST_USER, password: str = TEST_PASS,
                role: str = "staff") -> tuple[bool, str]:
    """Run `python3 -m app.server --create-user` feeding the password twice.

    The server's --create-user prompts for the password via getpass; in a
    pipe getpass falls back to reading stdin, so we feed the password on two
    lines (password + confirm) to cover either prompt style.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "app.server",
         "--create-user", username, "--role", role],
        cwd=str(BASE_DIR),
        env=_child_env(),
        input=f"{password}\n{password}\n",
        text=True,
        capture_output=True,
        timeout=60,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    # Success is best-effort: exit 0, or the users file now contains the user.
    users_path = DATA_DIR / "users.json"
    created = False
    if users_path.exists():
        data = _json_or_none(users_path.read_text("utf-8"))
        created = bool(isinstance(data, dict) and username in data)
    ok = proc.returncode == 0 or created
    detail = "" if ok else f"rc={proc.returncode} out={out.strip()[:200]!r}"
    return ok, detail


def _login(username: str, password: str) -> bool:
    """Log in via the API, swapping the shared cookie jar's session cookie.

    Returns True on HTTP 200. The new session cookie replaces any prior one in
    ``_cookie_jar``, so subsequent ``_request`` calls act as *username*.
    """
    code, _, _ = _request(
        "POST", "/api/login", {"username": username, "password": password})
    return code == 200


def start_server() -> subprocess.Popen:
    logs = subprocess.DEVNULL
    return subprocess.Popen(
        [sys.executable, "-m", "app.server", "--port", str(PORT),
         "--host", HOST],
        cwd=str(BASE_DIR),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# individual tests
# ---------------------------------------------------------------------------

def test_health():
    code, _, text = _request("GET", "/api/health")
    body = _json_or_none(text) or {}
    ok = (code == 200 and body.get("ok") is True
          and "provider" in body)
    record("2. GET /api/health -> 200 ok:true provider present",
           ok, f"code={code} body={text[:160]}")
    return body.get("provider")


def test_scheduler_disabled():
    # 11. /api/health must expose the scheduler status block, and — because the
    # smoke server runs with APP_SCHEDULER=0 (see _child_env) — it must report
    # the loop DISABLED. This proves both that the health endpoint carries the
    # scheduler status and that the master off-switch is honoured (a test run
    # never spins up real comms/manager/audit jobs).
    code, _, text = _request("GET", "/api/health")
    body = _json_or_none(text) or {}
    sched = body.get("scheduler")
    ok = (code == 200 and isinstance(sched, dict)
          and sched.get("enabled") is False)
    record("11. GET /api/health -> scheduler.enabled == false (APP_SCHEDULER=0)",
           ok, f"code={code} scheduler={sched}")


def test_home_no_cookie():
    # fresh opener with NO cookies to prove auth is enforced
    try:
        req = urllib.request.Request(BASE_URL + "/api/home", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        code = -1
    record("3. GET /api/home without cookie -> 401", code == 401,
           f"code={code}")


def test_login_wrong():
    code, _, text = _request(
        "POST", "/api/login",
        {"username": TEST_USER, "password": "WRONG-password"})
    record("4. POST /api/login wrong password -> 401/403",
           code in (401, 403), f"code={code} body={text[:120]}")


def test_login_right_and_home():
    code, _, text = _request(
        "POST", "/api/login",
        {"username": TEST_USER, "password": TEST_PASS})
    login_ok = code == 200
    # cookie is now in the shared jar (used by _opener)
    has_cookie = any(c.name == "apw_session" for c in _cookie_jar)
    record("5a. POST /api/login correct -> 200 + apw_session cookie",
           login_ok and has_cookie,
           f"code={code} cookie={has_cookie} body={text[:120]}")

    code2, _, text2 = _request("GET", "/api/home")
    body = _json_or_none(text2) or {}
    keys_ok = all(k in body for k in
                  ("pending", "referrals_new", "manager_report", "audit"))
    record("5b. GET /api/home (authed) -> 200 with expected keys",
           code2 == 200 and keys_ok,
           f"code={code2} keys={sorted(body.keys())[:8]}")


def test_chat():
    code, _, text = _request("POST", "/api/chat", {"message": "hello"})
    body = _json_or_none(text) or {}
    reply = body.get("reply")
    ok = code == 200 and isinstance(reply, str)
    record("6. POST /api/chat -> 200 with reply string "
           "(provider may be none)", ok,
           f"code={code} reply_type={type(reply).__name__}")


def test_approvals_flow():
    # Enqueue a manual_task via app.approvals in the SAME db, then verify the
    # API sees it and can deny it. Runs as a child process so it uses the
    # server's config/db exactly.
    enqueue_code = (
        "import json;"
        "from app import db, approvals;"
        "db.init_db();"
        "card = approvals.enqueue('manual_task',"
        "  {'title':'smoke','detail':'verify_app manual task'},"
        "  reason='verify_app smoke', requested_by='verify_app');"
        "print(json.dumps({'id': card.get('id')}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", enqueue_code],
        cwd=str(BASE_DIR), env=_child_env(),
        text=True, capture_output=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    parsed = _json_or_none(out.splitlines()[-1]) if out else None
    approval_id = parsed.get("id") if isinstance(parsed, dict) else None
    if approval_id is None:
        record("7a. enqueue manual_task approval (app.approvals)", False,
               f"rc={proc.returncode} out={out[:160]!r} "
               f"err={(proc.stderr or '')[:160]!r}")
        record("7b. GET /api/approvals shows the queued card", False,
               "no approval id")
        record("7c. staff POST decision -> 403 (approver role required)",
               False, "no approval id")
        record("7d. manager POST decision approve:false -> status denied",
               False, "no approval id")
        return
    record("7a. enqueue manual_task approval (app.approvals)", True,
           f"id={approval_id}")

    code, _, text = _request("GET", "/api/approvals?status=pending")
    body = _json_or_none(text)
    rows = body if isinstance(body, list) else (
        body.get("approvals") if isinstance(body, dict) else None)
    found = bool(rows) and any(
        (isinstance(r, dict) and r.get("id") == approval_id) for r in rows)
    record("7b. GET /api/approvals shows the queued card", found,
           f"code={code} n={len(rows) if rows else 0}")

    # 7c. A plain 'staff' session (currently logged in as TEST_USER) MUST be
    # forbidden from deciding an approval — approving/executing EMR writes is
    # gated to approver roles (manager/admin). Expect 403.
    code_staff, _, text_staff = _request(
        "POST", f"/api/approvals/{approval_id}/decision",
        {"approve": False, "note": "staff-should-be-forbidden"})
    record("7c. staff POST decision -> 403 (approver role required)",
           code_staff == 403,
           f"code={code_staff} body={text_staff[:120]}")

    # 7d. An approver ('manager') can decide. Swap the session cookie by
    # logging in as the manager, then deny the card and expect status denied.
    logged_in = _login(APPROVER_USER, APPROVER_PASS)
    code2, _, text2 = _request(
        "POST", f"/api/approvals/{approval_id}/decision",
        {"approve": False, "note": "smoke"})
    body2 = _json_or_none(text2) or {}
    # card may be returned at top level or nested under a key
    status = body2.get("status") or (
        body2.get("card", {}) if isinstance(body2.get("card"), dict) else {}
    ).get("status")
    record("7d. manager POST decision approve:false -> status denied",
           logged_in and code2 == 200 and status == "denied",
           f"login={logged_in} code={code2} status={status!r}")

    # Restore the staff session for any later tests that assume TEST_USER.
    _login(TEST_USER, TEST_PASS)


def test_root_html():
    code, headers, text = _request("GET", "/")
    ctype = (headers.get("Content-Type") or headers.get("content-type")
             or "").lower()
    ok = code == 200 and "text/html" in ctype
    record("8. GET / -> 200 text/html", ok,
           f"code={code} ctype={ctype!r}")


def test_referral_ingest():
    inbox = DATA_DIR / "referrals_in"
    inbox.mkdir(parents=True, exist_ok=True)
    marker = "verify_app_smoke_referral"
    payload = {
        "patient_name": marker,
        "dob": "01/01/1990",
        "phone": "215-555-0100",
        "referrer": "Dr. Smoke",
        "reason": "verify_app smoke referral",
    }
    (inbox / "smoke_referral.json").write_text(
        json.dumps(payload), encoding="utf-8")

    code, _, text = _request("POST", "/api/referrals/ingest")
    ingest_ok = code == 200
    record("9a. POST /api/referrals/ingest -> 200", ingest_ok,
           f"code={code} body={text[:140]}")

    code2, _, text2 = _request("GET", "/api/referrals")
    body = _json_or_none(text2)
    rows = body if isinstance(body, list) else (
        body.get("referrals") if isinstance(body, dict) else None)
    found = bool(rows) and any(
        isinstance(r, dict) and r.get("patient_name") == marker
        for r in rows)
    record("9b. GET /api/referrals shows the dropped referral", found,
           f"code={code2} n={len(rows) if rows else 0}")


# ---------------------------------------------------------------------------
# comms lane (email + SMS inbox) — checks 10a-10d
# ---------------------------------------------------------------------------

# A unique marker so the inserted row is unambiguous even if a real db leaked
# through (it won't — data dir is snapshotted/restored — but be defensive).
_MSG_MARKER = "verify_app_smoke_msg"
_MSG_EXTERNAL_ID = "<verify-app-smoke@example.test>"


def _insert_inbound_message() -> int | None:
    """Insert a fake INBOUND email row via app.comms in a child process.

    Runs in the SAME config/db as the server (via _child_env), using the real
    comms._insert_message so the row matches production shape exactly. Returns
    the new row id, or None on failure. Idempotent-ish: INSERT OR IGNORE on the
    marker external_id means a re-run finds the existing row's id.
    """
    code = (
        "import json;"
        "from app import db, comms;"
        "db.init_db();"
        "rid = comms._insert_message("
        "  channel='email', direction='in',"
        "  sender='patient@example.test', recipient='office@example.test',"
        f"  subject={_MSG_MARKER!r}, body='Please send my records.',"
        f"  external_id={_MSG_EXTERNAL_ID!r}, thread_ref='', status='new');"
        "row = comms.get_message(rid) if rid else None;"
        # If the row was a dup (rid==0), look it up by the unique external_id so
        # we still return a real id for the rest of the checks.
        "conn = db.get_conn();"
        "found = conn.execute("
        f"  'SELECT id FROM messages WHERE external_id = ?', ({_MSG_EXTERNAL_ID!r},)"
        ").fetchone();"
        "conn.close();"
        "print(json.dumps({'id': (found['id'] if found else None)}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BASE_DIR), env=_child_env(),
        text=True, capture_output=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    parsed = _json_or_none(out.splitlines()[-1]) if out else None
    return parsed.get("id") if isinstance(parsed, dict) else None


def test_messages_poll():
    # 10a. Poll every channel. With no Gmail/RingCentral creds in the child env,
    # both arms must degrade cleanly to the string 'not configured' — never
    # raise, never fabricate a count.
    code, _, text = _request("POST", "/api/messages/poll")
    body = _json_or_none(text) or {}
    ok = (code == 200
          and body.get("email") == "not configured"
          and body.get("sms") == "not configured")
    record("10a. POST /api/messages/poll -> 200 email/sms 'not configured'",
           ok, f"code={code} body={text[:160]}")


def test_messages_list_and_home(message_id: int | None):
    # 10b. A directly-inserted inbound email must surface both in the messages
    # list and in /api/home's inbox_new bucket.
    if message_id is None:
        record("10b. GET /api/messages shows inserted row + /api/home inbox_new",
               False, "no message id (insert failed)")
        return
    code, _, text = _request("GET", "/api/messages?limit=50")
    body = _json_or_none(text) or {}
    rows = body.get("messages") if isinstance(body, dict) else None
    in_list = bool(rows) and any(
        isinstance(r, dict) and r.get("id") == message_id
        and r.get("subject") == _MSG_MARKER for r in rows)

    code2, _, text2 = _request("GET", "/api/home")
    home = _json_or_none(text2) or {}
    inbox_new = home.get("inbox_new")
    in_home = isinstance(inbox_new, list) and any(
        isinstance(r, dict) and r.get("id") == message_id for r in inbox_new)

    record("10b. GET /api/messages shows inserted row + /api/home inbox_new",
           in_list and in_home,
           f"list_code={code} in_list={in_list} home_code={code2} "
           f"in_home={in_home}")


def test_send_email_approval_card():
    # 10c. Enqueue a send_email approval carrying a FULL body, confirm the card
    # (and its body) is visible in /api/approvals, then have the manager DENY it
    # and confirm status 'denied'. The body must reach the approver verbatim —
    # it is an outbound PHI communication the human must read before it sends.
    body_text = "Hello, here are the records you requested. — Front desk"
    # Build the params dict in the child from an env var so we never have to
    # hand-embed a dict literal (and its braces) inside this f-string.
    child_env = _child_env()
    child_env["VERIFY_SEND_BODY"] = body_text
    enqueue_code = (
        "import json, os;"
        "from app import db, approvals;"
        "db.init_db();"
        "params = {'to':'patient@example.test','subject':'Your records',"
        "          'body': os.environ['VERIFY_SEND_BODY']};"
        "card = approvals.enqueue('send_email', params,"
        "  reason='verify_app smoke send_email', requested_by='verify_app');"
        "print(json.dumps({'id': card.get('id')}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", enqueue_code],
        cwd=str(BASE_DIR), env=child_env,
        text=True, capture_output=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    parsed = _json_or_none(out.splitlines()[-1]) if out else None
    approval_id = parsed.get("id") if isinstance(parsed, dict) else None
    if approval_id is None:
        record("10c. send_email approval card shows body in params; deny -> "
               "denied", False,
               f"rc={proc.returncode} out={out[:160]!r} "
               f"err={(proc.stderr or '')[:160]!r}")
        return

    code, _, text = _request("GET", "/api/approvals?status=pending")
    body = _json_or_none(text)
    rows = body if isinstance(body, list) else (
        body.get("approvals") if isinstance(body, dict) else None)
    card = None
    if rows:
        for r in rows:
            if isinstance(r, dict) and r.get("id") == approval_id:
                card = r
                break
    params = (card or {}).get("params") if isinstance(card, dict) else None
    body_in_params = (isinstance(params, dict)
                      and params.get("body") == body_text)

    # Manager denies (staff would be 403 — proven in 7c; here we go straight to
    # the deny outcome the check asks for).
    logged_in = _login(APPROVER_USER, APPROVER_PASS)
    code2, _, text2 = _request(
        "POST", f"/api/approvals/{approval_id}/decision",
        {"approve": False, "note": "verify_app smoke deny"})
    body2 = _json_or_none(text2) or {}
    status = body2.get("status") or (
        body2.get("card", {}) if isinstance(body2.get("card"), dict) else {}
    ).get("status")

    record("10c. send_email approval card shows body in params; deny -> denied",
           body_in_params and logged_in and code2 == 200
           and status == "denied",
           f"body_in_params={body_in_params} login={logged_in} "
           f"code={code2} status={status!r}")

    # Restore the staff session for the remaining checks.
    _login(TEST_USER, TEST_PASS)


def test_message_update(message_id: int | None):
    # 10d. A known id can be marked 'triaged' (200 ok); an unknown id is a
    # 404 — the server never claims success for a row that does not exist
    # (mirrors the referral-update honesty pattern).
    if message_id is None:
        record("10d. POST /api/messages/{id}/update triaged -> ok; unknown "
               "-> 404", False, "no message id (insert failed)")
        return
    code, _, text = _request(
        "POST", f"/api/messages/{message_id}/update", {"status": "triaged"})
    body = _json_or_none(text) or {}
    ok_update = code == 200 and body.get("ok") is True

    # Unknown id: use a huge id that cannot exist in a fresh smoke db.
    code2, _, _ = _request(
        "POST", "/api/messages/999999999/update", {"status": "triaged"})
    not_found = code2 == 404

    record("10d. POST /api/messages/{id}/update triaged -> ok; unknown -> 404",
           ok_update and not_found,
           f"update_code={code} ok={ok_update} unknown_code={code2}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    backup = None
    proc = None
    try:
        backup = snapshot_data_dir()
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Test 1 — create the smoke users (server must exist & run
        # --create-user): a plain 'staff' user and an approver ('manager').
        ok, detail = create_user(TEST_USER, TEST_PASS, role="staff")
        ok_mgr, detail_mgr = create_user(
            APPROVER_USER, APPROVER_PASS, role="manager")
        record("1. --create-user smoketest (staff) + smokemgr (manager)",
               ok and ok_mgr,
               detail or detail_mgr)
        if not ok:
            # Without a user, login-dependent tests cannot pass — but keep
            # going so the summary reflects the full surface.
            pass

        proc = start_server()
        up = _wait_for_health(proc, deadline=time.time() + 30)
        if not up:
            tail = ""
            if proc.poll() is not None and proc.stdout:
                try:
                    tail = proc.stdout.read()[-500:]
                except Exception:
                    tail = ""
            record("server startup (health reachable)", False,
                   f"server did not become healthy; tail={tail!r}")
            # Remaining HTTP tests will all fail fast; record them explicitly.
            for name in ("2. GET /api/health", "3. /api/home no cookie",
                         "4. login wrong", "5a. login correct",
                         "5b. /api/home authed", "6. /api/chat",
                         "7a-c. approvals", "8. GET /", "9. referrals",
                         "10a. messages poll", "10b. messages list/home",
                         "10c. send_email approval", "10d. message update",
                         "11. scheduler disabled"):
                record(name, False, "server not up")
            return 1

        # Tests 2-9
        test_health()
        test_scheduler_disabled()                  # 11 (health scheduler block)
        test_home_no_cookie()
        test_login_wrong()
        test_login_right_and_home()
        test_chat()
        test_approvals_flow()
        test_root_html()
        test_referral_ingest()

        # Comms lane (email + SMS inbox) — checks 10a-10d. test_approvals_flow
        # restored the TEST_USER (staff) session, so these run authed as staff.
        test_messages_poll()                       # 10a
        message_id = _insert_inbound_message()
        test_messages_list_and_home(message_id)    # 10b
        test_send_email_approval_card()            # 10c (re-logs staff at end)
        test_message_update(message_id)            # 10d
        return 0
    finally:
        stop_server(proc)
        restore_data_dir(backup)
        total = len(_results)
        passed = sum(1 for _, ok, _ in _results if ok)
        all_pass = bool(total) and passed == total
        print(f"VERIFY_APP {'PASS' if all_pass else 'FAIL'} "
              f"{passed}/{total}", flush=True)


def _compute_exit_code() -> int:
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    return 0 if (total and passed == total) else 1


if __name__ == "__main__":
    try:
        main()
        code = _compute_exit_code()
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)

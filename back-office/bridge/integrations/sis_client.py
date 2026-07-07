# VENDORED from mainlinesurgery-a11y/n8n-office @ backoffice-autopilot-live-20260705, commit eec3888
# Source path: python/integrations/sis_client.py
# Re-vendored from the REAL, HAR-verified production repo (do not edit lightly).
# SIS Complete REST client (read-only, via authenticated Playwright page).
#!/usr/bin/env python3
"""
sis_client.py — REST API client for SIS Complete EMR.

Uses a persistent Playwright browser context as the transport — all API calls
run inside the live browser via page.evaluate(fetch(...)), so Auth0 session
state is naturally preserved. Browser state persists across MCP restarts via
the persistent context dir.

SIS Complete is a modern Angular SPA. The REST API lives at:
    https://e03ws.siscomplete.cloud/mainline/api/
The Auth0 domain is:
    siscomplete-prd-01.us.auth0.com

Browser state: ~/.gemini/antigravity/scratch/emr_sessions/sis_browser_state/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SESSION_DIR = Path(os.path.expanduser("~/.gemini/antigravity/scratch/emr_sessions"))
BROWSER_STATE_DIR = SESSION_DIR / "sis_browser_state"

SIS_API_BASE = "https://e03ws.siscomplete.cloud/mainline/api"
SIS_FRONTEND_URL = "https://e03.siscomplete.cloud/mainline/"
SIS_LOGIN_URL = "https://e03.siscomplete.cloud/mainline/login/"

# Lazy import — Playwright may not be installed in all envs
_pw_module = None


def _get_pw():
    global _pw_module
    if _pw_module is None:
        from playwright.async_api import async_playwright
        _pw_module = async_playwright
    return _pw_module


def _load_env():
    """Load .env from Antigravity dir (same pattern as svigg_scraper.py)."""
    env_path = Path(
        os.environ.get("ANTIGRAVITY_DIR", "/Users/shubh/Documents/Antigravity")
    ) / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class EMRSessionExpired(Exception):
    """Raised when SIS returns 401/403 and re-login also fails."""


# ---------------------------------------------------------------------------
# SISClient
# ---------------------------------------------------------------------------

class SISClient:
    """
    REST API client for SIS Complete EMR.

    Pattern:
      - A single persistent Playwright browser context is kept alive for the
        whole MCP server lifetime.
      - All API calls are executed inside the live browser via
        page.evaluate(fetch(...)), so Auth0 HttpOnly cookies and in-memory SDK
        state are naturally carried with every request.
      - On 401/403 the client re-triggers login and retries once.
      - Browser state persists across restarts via the persistent context dir.
    """

    def __init__(self, headless: bool = True):
        _load_env()
        self.headless = headless
        self.sis_url = os.environ.get("SIS_URL", SIS_LOGIN_URL)
        self.username = os.environ.get("SIS_USERNAME", "")
        self.password = os.environ.get("SIS_PASSWORD", "")
        self.api_base = SIS_API_BASE
        self.frontend_url = SIS_FRONTEND_URL

        self._pw = None            # playwright instance
        self._browser_ctx = None   # persistent context
        self._page = None          # main page used for both nav and fetches
        self._login_lock = asyncio.Lock()  # prevent concurrent login attempts

        self._bearer_token: Optional[str] = None  # Auth0 JWT captured from SPA's API calls
        self._session_token: Optional[str] = None  # UUID session token from Idp/Login (sent as 'token' header)
        self._logged_in = False
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Launch persistent browser context and verify/perform login. Idempotent."""
        if self._page is not None:
            return

        BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = await _get_pw()().start()
        self._browser_ctx = await self._pw.chromium.launch_persistent_context(
            str(BROWSER_STATE_DIR),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        self._page = (
            self._browser_ctx.pages[0]
            if self._browser_ctx.pages
            else await self._browser_ctx.new_page()
        )

        # Listen for the auth headers the SPA sends. We need two:
        #   1. Authorization: Bearer <JWT>  (Auth0 access token)
        #   2. token: <UUID>                 (SIS session token from Idp/Login)
        def _on_request(req):
            if "e03ws" in req.url or "siscomplete.cloud/mainline/api" in req.url:
                auth = req.headers.get("authorization") or req.headers.get("Authorization")
                if auth and auth.lower().startswith("bearer "):
                    token = auth.split(" ", 1)[1].strip()
                    if token and token != self._bearer_token:
                        self._bearer_token = token
                        logger.debug("Captured Bearer token (len=%d)", len(token))
                sess = req.headers.get("token")
                if sess and sess != self._session_token:
                    self._session_token = sess
                    logger.debug("Captured SIS session token: %s", sess[:8] + "...")
        self._page.on("request", _on_request)

        await self._verify_or_login()

    async def stop(self):
        """Close the browser context and Playwright instance."""
        if self._browser_ctx:
            await self._browser_ctx.close()
        if self._pw:
            await self._pw.stop()
        self._browser_ctx = None
        self._pw = None
        self._page = None
        logger.info("SIS client stopped")

    # ------------------------------------------------------------------
    # Internal navigation helpers
    # ------------------------------------------------------------------

    async def _is_on_dashboard(self) -> bool:
        url = self._page.url
        return "/mainline/" in url and "login" not in url and "auth0" not in url

    async def _verify_or_login(self):
        """Navigate to frontend; if dashboard loads, warm up to capture Bearer token, then verify."""
        await self._page.goto(self.frontend_url, wait_until="networkidle", timeout=30000)
        await self._page.wait_for_timeout(5000)

        if await self._is_on_dashboard():
            await self._warm_up()
            try:
                user = await self._api_call("GET", "User/CurrentUser")
                if isinstance(user, dict) and user.get("UserName"):
                    self._logged_in = True
                    logger.info(
                        "SIS session verified from persistent context (user: %s)",
                        user.get("UserName"),
                    )
                    return
            except Exception as e:
                logger.debug("Session check failed: %s — will re-login", e)

        ok = await self._login()
        if ok:
            await self._warm_up()
            self._logged_in = True
        else:
            logger.warning("SIS login failed — client will operate in disconnected state")

    async def _warm_up(self):
        """
        Capture BOTH tokens needed for SIS API calls:
          - Bearer (Auth0 JWT): captured passively from SPA's outgoing requests.
          - Session token (UUID): minted by POST /api/Idp/Login.
        We wait briefly for the SPA's bootstrap to give us the Bearer, then mint
        the session token explicitly so we don't depend on the SPA firing the
        right requests during warm-up.
        """
        # Wait up to 30s for the Bearer JWT to appear on a request
        for _ in range(30):
            await self._page.wait_for_timeout(1000)
            if self._bearer_token:
                break
        if not self._bearer_token:
            logger.warning("Warm-up: no Bearer token captured — SIS API calls will fail")
            return

        # If session_token wasn't captured passively, mint it via Idp/Login
        if not self._session_token:
            try:
                headers = {
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://e03.siscomplete.cloud",
                    "Referer": "https://e03.siscomplete.cloud/mainline/",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._bearer_token}",
                }
                resp = await self._browser_ctx.request.post(
                    self.api_base + "/Idp/Login",
                    headers=headers,
                    data="{}",
                )
                if resp.status == 200:
                    body = await resp.text()
                    try:
                        token = json.loads(body)
                        if isinstance(token, str) and len(token) >= 32:
                            self._session_token = token
                            logger.info("Session token minted via Idp/Login")
                    except Exception:
                        pass
                else:
                    logger.warning("Idp/Login returned status %d — no session token", resp.status)
            except Exception as e:
                logger.warning("Idp/Login mint failed: %s", e)
        else:
            logger.info("Bearer + session tokens captured passively during warm-up")

        # ---------------------------------------------------------------
        # Nursing/Clinical module setup step
        #
        # SIS v1.8.384.0 has NO programmatic desktop-switch endpoint
        # (discovery confirmed: no SetDesktop/SetMode API exists; the
        # desktop is governed server-side by UserOrgMap.profileLevelId).
        # The closest available action is:
        #   (1) Re-assert the org session for organizationID=3 via
        #       UserSession/UserSessionOrg — confirmed to return
        #       {isEnterprise, isGeminiEnabled, setSessionOrgSuccessful}.
        #   (2) Enumerate the perioperative nursing modules
        #       (Pre-Op 1020, Operative 1030, Recovery 1060) to confirm
        #       they are accessible under the current profile (Clinical,
        #       profileLevelId=2), which already exposes the same API
        #       surface as profileLevelId=7 Pre-Op & Post-Op Nurse.
        # If either call fails, a warning is logged and warm-up continues.
        # ---------------------------------------------------------------
        if self._bearer_token and self._session_token:
            std_headers = {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://e03.siscomplete.cloud",
                "Referer": "https://e03.siscomplete.cloud/mainline/",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._bearer_token}",
                "token": self._session_token,
            }
            # Step 1: assert org session for Main Line Surgical Center (orgId=3)
            try:
                org_resp = await self._browser_ctx.request.post(
                    self.api_base + "/UserSession/UserSessionOrg/3/false/false",
                    headers=std_headers,
                    data="{}",
                )
                if org_resp.status == 200:
                    org_body = await org_resp.json()
                    success = org_body.get("setSessionOrgSuccessful", False)
                    logger.info(
                        "Nursing setup: org session asserted for organizationID=3 "
                        "(setSessionOrgSuccessful=%s, isGeminiEnabled=%s)",
                        success,
                        org_body.get("isGeminiEnabled"),
                    )
                else:
                    logger.warning(
                        "Nursing setup: UserSessionOrg returned status %d — "
                        "continuing without org re-assertion",
                        org_resp.status,
                    )
            except Exception as e:
                logger.warning("Nursing setup: UserSessionOrg call failed: %s", e)

            # Step 2: enumerate perioperative nursing modules
            try:
                mod_resp = await self._browser_ctx.request.get(
                    self.api_base + "/Module/GetModulesForOrganization",
                    headers=std_headers,
                    params={"removeQxModules": "true"},
                )
                if mod_resp.status == 200:
                    modules = await mod_resp.json()
                    names = [m.get("moduleName", m.get("itemName", "?")) for m in modules if isinstance(m, dict)]
                    logger.info(
                        "Nursing setup: %d perioperative module(s) accessible: %s",
                        len(names),
                        ", ".join(names) if names else "(none)",
                    )
                else:
                    logger.warning(
                        "Nursing setup: GetModulesForOrganization returned status %d",
                        mod_resp.status,
                    )
            except Exception as e:
                logger.warning("Nursing setup: module enumeration failed: %s", e)

    # ------------------------------------------------------------------
    # Auth0 login via Playwright (browser-internal)
    # ------------------------------------------------------------------

    async def _login(self) -> bool:
        """
        Internal login helper used by _verify_or_login.
        The browser context is already open; this navigates through Auth0.
        """
        return await self.login()

    async def login(self) -> bool:
        """
        Full Auth0 login using the live browser context. Acquires _login_lock so
        only one login runs at a time across the whole client.

        NOTE: callers that ALREADY hold _login_lock (e.g. _api_call's serialized
        401/403 refresh path) MUST call _login_locked() directly instead —
        asyncio.Lock is NOT reentrant and re-acquiring it here would deadlock.

        Steps:
          1. Navigate to /mainline/login/ — Auth0 redirect occurs automatically.
          2. Fill email field → click Continue.
          3. Fill password field → click Continue.
          4. Handle MFA: wait for code dropped into /tmp/sis_2fa_code.txt.
          5. Poll until on dashboard (up to 60 s).
        Returns True on success, False on failure.
        """
        async with self._login_lock:
            return await self._login_locked()

    async def _login_locked(self) -> bool:
        """
        Lock-free login body. The CALLER must already hold self._login_lock.
        Split out from login() so the serialized 401/403 refresh in _api_call
        (which acquires the lock itself, then double-checks the token) can drive
        the login without the reentrant re-acquire that would deadlock.
        """
        if True:
            if not self.username:
                raise ValueError("SIS_USERNAME must be set in .env")
            if not self.password:
                raise ValueError(
                    "SIS_PASSWORD must be set in .env — "
                    "set the current password and restart"
                )

            logger.info("Navigating to SIS login page: %s", self.sis_url)
            await self._page.goto(self.sis_url, wait_until="networkidle", timeout=30000)

            # Check if already on dashboard (SSO from persistent context)
            if await self._is_on_dashboard():
                logger.info("Already authenticated via persistent browser context")
                return True

            # Click SIS splash "Login" button if present, then wait for URL to
            # actually settle on either Auth0 (login form) or back on the SPA dashboard.
            try:
                await self._page.click('button:has-text("Login")', timeout=5000)
                logger.info("Clicked SIS Login button — waiting for redirect target")
            except Exception:
                logger.debug("No SIS Login button found — proceeding")

            # Wait up to 25s for URL to reach a known terminal state:
            #   - auth0.com (email/password/MFA form)
            #   - /mainline/ dashboard (silent SSO succeeded)
            deadline = asyncio.get_event_loop().time() + 25
            while asyncio.get_event_loop().time() < deadline:
                url = self._page.url
                if "auth0.com" in url or await self._is_on_dashboard():
                    break
                await asyncio.sleep(0.5)

            if await self._is_on_dashboard():
                logger.info("Silent SSO succeeded — on dashboard")
                return True

            # Auth0 email + password
            if "auth0" in self._page.url:
                logger.info("Filling Auth0 email field")
                try:
                    await self._page.fill('input[name="username"]', self.username)
                    await self._page.click('button[type="submit"]')
                    await self._page.wait_for_load_state("networkidle", timeout=10000)
                except Exception as e:
                    logger.error("Could not fill Auth0 email field: %s", e)
                    return False

                logger.info("Filling Auth0 password field")
                try:
                    await self._page.fill('input[type="password"]', self.password)
                    await self._page.click('button[type="submit"]')
                    await self._page.wait_for_timeout(6000)
                except Exception as e:
                    logger.error("Could not fill Auth0 password field: %s", e)
                    return False

            # Handle MFA
            if "mfa" in self._page.url:
                code = await self._wait_for_mfa_code()
                if code is None:
                    logger.error("MFA code not provided within timeout — login failed")
                    return False
                try:
                    cb = await self._page.query_selector('input[name="rememberBrowser"]')
                    if cb and not await cb.is_checked():
                        await cb.check()
                except Exception:
                    pass
                await self._page.fill('input[name="code"]', code)
                await self._page.click('button[type="submit"]')

            # Poll up to 60 s for dashboard
            deadline = asyncio.get_event_loop().time() + 60
            while asyncio.get_event_loop().time() < deadline:
                if await self._is_on_dashboard():
                    await self._page.wait_for_timeout(3000)
                    logger.info("SIS login succeeded — on dashboard: %s", self._page.url)
                    # Clean up MFA code file if left behind
                    try:
                        Path("/tmp/sis_2fa_code.txt").unlink()
                    except FileNotFoundError:
                        pass
                    return True
                try:
                    await self._page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(1)

            logger.error("SIS login failed — still at: %s", self._page.url)
            return False

    # ------------------------------------------------------------------
    # MFA code helper
    # ------------------------------------------------------------------

    async def _wait_for_mfa_code(self, timeout: float = 300) -> Optional[str]:
        """
        Wait for a 6-digit MFA code dropped into /tmp/sis_2fa_code.txt.
        Signals waiting state via /tmp/sis_2fa_pending.txt.
        """
        code_file = Path("/tmp/sis_2fa_code.txt")
        pending_file = Path("/tmp/sis_2fa_pending.txt")
        pending_file.write_text("1")
        logger.info(
            "SIS MFA required — drop the 6-digit code into /tmp/sis_2fa_code.txt within 5 min"
        )
        deadline = asyncio.get_event_loop().time() + timeout
        try:
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(1)
                if code_file.exists():
                    code = code_file.read_text().strip()
                    if code.isdigit() and len(code) >= 4:
                        return code
            return None
        finally:
            try:
                pending_file.unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------
    # Core API call (browser-internal fetch)
    # ------------------------------------------------------------------

    async def _api_call(
        self,
        method: str,
        endpoint: str,
        data: Any = None,
        params: dict = None,
    ) -> Any:
        """
        Execute an API call via Playwright's APIRequestContext.
        Uses the browser context's cookies and storage state automatically,
        but bypasses page-level CORS (the request is not from a page origin).
        On 401/403: triggers a SERIALIZED re-login and retries once.

        Token-refresh concurrency (fixed): _api_call itself takes no lock, so N
        concurrent calls can all see an expired token at once. Without
        coordination each would independently call login() (which drives the
        single shared Playwright _page through Auth0+MFA and rewrites the shared
        _bearer_token/_session_token) — a thundering herd of logins, and some
        retries firing with stale headers. To fix this we serialize the refresh
        through the existing _login_lock with a double-check: the FIRST coroutine
        to grab the lock re-logs-in; every other coroutine, once it acquires the
        lock, sees the token has already changed and skips the redundant login.
        All then retry with freshly-built headers carrying the refreshed token.
        """
        if not self._browser_ctx:
            raise EMRSessionExpired("SIS client not started — call start() first")

        url = self.api_base + "/" + endpoint.lstrip("/")

        def _build_headers() -> dict:
            """Build request headers from the CURRENT shared tokens.

            Rebuilt on each (re)try so a retry after refresh picks up the new
            _bearer_token/_session_token rather than reusing stale headers.
            """
            h = {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://e03.siscomplete.cloud",
                "Referer": "https://e03.siscomplete.cloud/mainline/",
            }
            if data is not None:
                h["Content-Type"] = "application/json"
            if self._bearer_token:
                h["Authorization"] = f"Bearer {self._bearer_token}"
            if self._session_token:
                h["token"] = self._session_token
            return h

        async def _do_call():
            req_kwargs = {"headers": _build_headers(), "params": params or {}}
            if data is not None:
                req_kwargs["data"] = json.dumps(data)
            m = method.upper()
            if m == "GET":
                return await self._browser_ctx.request.get(url, **req_kwargs)
            if m == "POST":
                return await self._browser_ctx.request.post(url, **req_kwargs)
            if m == "PUT":
                return await self._browser_ctx.request.put(url, **req_kwargs)
            if m == "DELETE":
                return await self._browser_ctx.request.delete(url, **req_kwargs)
            raise ValueError(f"Unsupported HTTP method: {method}")

        # Remember the bearer token this call was built with, so that after we
        # win/lose the race for the login lock we can tell whether a refresh has
        # already happened (double-checked locking on the token value).
        token_before = self._bearer_token
        resp = await _do_call()
        status = resp.status

        if status in (401, 403):
            logger.info("SIS API returned %d — serializing re-login", status)
            async with self._login_lock:
                # Double-check: another coroutine may have already refreshed the
                # token while we waited for the lock. Only re-login if the token
                # is unchanged (still the stale one this call started with).
                if self._bearer_token == token_before:
                    self._logged_in = False
                    # We already hold _login_lock — call the lock-free body
                    # directly. Calling login() here would re-acquire the same
                    # non-reentrant lock and deadlock.
                    if not await self._login_locked():
                        raise EMRSessionExpired(f"Re-login failed after {status}")
                else:
                    logger.info(
                        "SIS token already refreshed by a concurrent call — "
                        "skipping redundant re-login"
                    )
            # Retry once with freshly-built headers (new token).
            resp = await _do_call()
            status = resp.status
            if status in (401, 403):
                raise EMRSessionExpired(f"Still {status} after re-login")

        if status >= 400:
            body = ""
            try:
                body = await resp.text()
            except Exception:
                pass
            raise Exception(f"HTTP {status}: {body[:300]}")

        try:
            body = await resp.text()
        except Exception:
            body = ""
        if not body:
            return {}
        try:
            return json.loads(body)
        except Exception:
            return {"_raw": body}

    async def _api_call_pdf(
        self,
        method: str,
        endpoint: str,
        data: Any = None,
        params: dict = None,
    ) -> tuple[bytes, str]:
        """
        Sibling of _api_call for BINARY (PDF) responses.

        Same header builder and serialized 401/403 re-login + single-retry
        discipline as _api_call, but returns the raw response bytes plus the
        Content-Type header instead of decoding JSON. Non-2xx statuses raise
        exactly like _api_call (Exception with body text truncated to 300
        chars); 401/403 after re-login raises EMRSessionExpired.

        Returns:
            (body_bytes, content_type)
        """
        if not self._browser_ctx:
            raise EMRSessionExpired("SIS client not started — call start() first")

        url = self.api_base + "/" + endpoint.lstrip("/")

        def _build_headers() -> dict:
            """Same per-(re)try header builder as _api_call — rebuilt on each
            attempt so a post-refresh retry picks up the new tokens."""
            h = {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://e03.siscomplete.cloud",
                "Referer": "https://e03.siscomplete.cloud/mainline/",
            }
            if data is not None:
                h["Content-Type"] = "application/json"
            if self._bearer_token:
                h["Authorization"] = f"Bearer {self._bearer_token}"
            if self._session_token:
                h["token"] = self._session_token
            return h

        async def _do_call():
            req_kwargs = {"headers": _build_headers(), "params": params or {}}
            if data is not None:
                req_kwargs["data"] = json.dumps(data)
            m = method.upper()
            if m == "GET":
                return await self._browser_ctx.request.get(url, **req_kwargs)
            if m == "POST":
                return await self._browser_ctx.request.post(url, **req_kwargs)
            raise ValueError(f"Unsupported HTTP method for PDF call: {method}")

        token_before = self._bearer_token
        resp = await _do_call()
        status = resp.status

        if status in (401, 403):
            logger.info("SIS PDF API returned %d — serializing re-login", status)
            async with self._login_lock:
                # Double-checked locking on the token value — see _api_call.
                if self._bearer_token == token_before:
                    self._logged_in = False
                    if not await self._login_locked():
                        raise EMRSessionExpired(f"Re-login failed after {status}")
                else:
                    logger.info(
                        "SIS token already refreshed by a concurrent call — "
                        "skipping redundant re-login"
                    )
            resp = await _do_call()
            status = resp.status
            if status in (401, 403):
                raise EMRSessionExpired(f"Still {status} after re-login")

        if status >= 400:
            body = ""
            try:
                body = await resp.text()
            except Exception:
                pass
            raise Exception(f"HTTP {status}: {body[:300]}")

        body_bytes = await resp.body()
        content_type = (resp.headers or {}).get("content-type", "")
        return body_bytes, content_type

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def get_current_user(self) -> dict:
        """GET /User/V2/CurrentUser — verifies session and returns user info."""
        return await self._api_call("GET", "User/V2/CurrentUser")

    async def search_patient(self, query: str) -> list[dict]:
        """POST /PatientData/Gemini/SearchToken — patient search by name/MRN/DOB."""
        result = await self._api_call(
            "POST",
            "PatientData/Gemini/SearchToken",
            data={
                "organizationId": 1,
                "fetchImages": False,
                "searchString": query,
                "pageCount": 0,
                "lastAdmittedDate": None,
                "DOB": None,
                "fetchPhones": True,
                "sourceIdentifier": "",
                "filterBySourceIdentifier": False,
            },
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_todays_patients(self) -> list[dict]:
        """Today's *scheduled* surgical cases via SchedulingData/GetSchedulingData
        (day-scoped to today's 24h window).

        NOT the unsigned-cases backlog — this previously delegated to the same
        UnsignedCasesTracker endpoint as get_unsigned_cases(), so "today" wrongly
        returned every unsigned case across all dates. Returns a flat list of
        schedule event dicts; an empty list means no cases are scheduled today
        (e.g. a non-OR weekday — Atlantic operates Thu/Fri), which is a valid
        result, not an error. Event shape is the SchedulingData event (keys like
        patient, primaryProcedure, caseAccountNumber, caseStatus, procedureDt),
        NOT the UnsignedCasesTracker row shape.
        """
        return await self.get_schedule_day()  # date=None → today's 24h window

    async def get_rooms(self) -> list[dict]:
        """GET /Room/GetRoomsForOrganization — OR/procedure rooms for current org."""
        result = await self._api_call("GET", "Room/GetRoomsForOrganization")
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_case_packs(self) -> list[dict]:
        """GET /CasePack/GetCasePacks — available case packs/procedure sets."""
        result = await self._api_call("GET", "CasePack/GetCasePacks")
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_unsigned_cases(self) -> list[dict]:
        """POST /UnsignedCasesTracker/GetUnsignedUncancelledTrackerData with dateTime + paging."""
        today = datetime.now().strftime("%m/%d/%Y 00:00:00 -04:00")
        result = await self._api_call(
            "POST",
            "UnsignedCasesTracker/GetUnsignedUncancelledTrackerData",
            data={"dateTime": today, "pageNumber": 1, "columnId": 3, "orderType": 0},
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_modules(self) -> list[dict]:
        """GET /Module/GetModulesForOrganization — enabled modules for the org."""
        result = await self._api_call(
            "GET",
            "Module/GetModulesForOrganization",
            params={"removeQxModules": "true"},
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    @staticmethod
    def _unwrap_list(result: Any, route: str) -> list[dict]:
        """Unwrap a list payload from a SIS response envelope — FAIL CLOSED.

        Accepts a bare JSON list, or a dict envelope carrying the list under
        "Data"/"data" (the two envelope keys observed live on this API).
        ANYTHING else — an unrecognized envelope, a non-list Data value, a
        non-JSON body ({"_raw": ...}) — raises instead of returning []: an
        empty list must only ever mean "the route really returned no rows",
        never "we did not understand the response" (a silent [] from an
        unrecognized envelope would read as an honest no-data result, e.g. a
        fabricated $0 billing picture). Used by the HAR-derived routes whose
        envelopes are NOT yet live-verified; error text carries key NAMES
        only, never payload values.
        """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("Data", "data"):
                if key in result:
                    inner = result[key]
                    if isinstance(inner, list):
                        return inner
                    raise ValueError(
                        f"SIS {route}: envelope key {key!r} holds "
                        f"{type(inner).__name__}, not a list — refusing to "
                        "guess (envelope not yet live-verified)"
                    )
            raise ValueError(
                f"SIS {route}: unrecognized response envelope (keys "
                f"{sorted(result)[:8]}) — refusing to return [] for a "
                "response we did not understand"
            )
        raise ValueError(
            f"SIS {route}: unexpected response type {type(result).__name__} "
            "— refusing to return [] for a response we did not understand"
        )

    async def get_recent_patients(self, from_date: str = None) -> list[dict]:
        """
        POST /RecentPatients/GetRecentPatientsData — recently seen/accessed patients.

        CONFIRMED BROKEN LIVE (2026-07-04): every call returns HTTP 400
        "Value does not fall within the expected range." The HAR only
        captured the body KEY name ("fromDate"), not a real value — 5 candidate
        formats were probed live (ISO with .000Z, ISO without ms, bare
        YYYY-MM-DD, clinic-local-midnight, and a 7-days-ago variant) and ALL
        failed identically, which rules out a date-format problem: either the
        body needs an additional required field the HAR never captured, the
        key name/type itself differs, or the route needs different auth/paging
        context. Do NOT keep guessing formats — this needs a fresh write-HAR
        of a real RecentPatients call (same discipline this codebase already
        applies to Svigg's bk_p: a HAR that only shows the route, not the
        body, blocks the feature until captured live). This method is left in
        place (it may still be useful once the real contract is known) but
        callers should expect it to raise until then.
        """
        if from_date is None:
            # Clinic-local (UTC-4) "now", whose DATE is the local calendar
            # day; local midnight = that date at 04:00Z.
            local_now = datetime.utcnow() - timedelta(hours=4)
            from_date = local_now.strftime("%Y-%m-%dT04:00:00.000Z")
        result = await self._api_call(
            "POST",
            "RecentPatients/GetRecentPatientsData",
            data={"fromDate": from_date},
        )
        return self._unwrap_list(result, "RecentPatients/GetRecentPatientsData")

    async def get_user_permissions(self) -> dict:
        """
        GET /Security/Permissions + GET /Security/UserRole — the current user's
        permission set and role (session/security metadata, no PHI).

        HAR-derived 2026-07-01; NOT yet live-verified. (Both routes returned
        HTTP 200 in the capture under a live SIS session.)

        Returns:
            {"permissions": <raw /Security/Permissions payload>,
             "role": <raw /Security/UserRole payload>}
        Payloads are returned as-is (shapes not yet documented). If either call
        fails, the exception propagates — no half-fabricated result.
        """
        permissions = await self._api_call("GET", "Security/Permissions")
        role = await self._api_call("GET", "Security/UserRole")
        return {"permissions": permissions, "role": role}

    async def get_schedule(self, date: str = None) -> dict:
        """
        POST /SchedulingData/GetSchedulingData with startDate/endDate (ISO 8601 UTC).
        Defaults to today's full day window.
        """
        if date is None:
            day = datetime.utcnow().replace(hour=4, minute=0, second=0, microsecond=0)
        else:
            day = datetime.strptime(date, "%Y-%m-%d").replace(hour=4, minute=0, second=0)
        start = day.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = (day + timedelta(hours=23, minutes=59, seconds=59, microseconds=999000)).strftime("%Y-%m-%dT%H:%M:%S.999Z")
        result = await self._api_call(
            "POST",
            "SchedulingData/GetSchedulingData",
            data={"startDate": start, "endDate": end},
        )
        return result if isinstance(result, dict) else {"data": result}

    async def get_schedule_day(self, date: str = None) -> list[dict]:
        """
        Single-day schedule window.
        POST /SchedulingData/GetSchedulingData for a 24-hour window starting at
        04:00:00.000Z (clinic-local midnight equivalent in UTC-4).
        Returns a flat list of event dicts (verified live: response is a JSON list,
        NOT wrapped in {data: [...]}).

        Args:
            date: ISO date string YYYY-MM-DD (defaults to today).
        """
        if date is None:
            day = datetime.utcnow().replace(hour=4, minute=0, second=0, microsecond=0)
        else:
            day = datetime.strptime(date, "%Y-%m-%d").replace(hour=4, minute=0, second=0)
        start = day.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = (day + timedelta(hours=23, minutes=59, seconds=59, microseconds=999000)).strftime(
            "%Y-%m-%dT%H:%M:%S.999Z"
        )
        result = await self._api_call(
            "POST",
            "SchedulingData/GetSchedulingData",
            data={"startDate": start, "endDate": end},
        )
        if isinstance(result, list):
            return result
        return result.get("data", result.get("Data", []))

    async def get_schedule_week(self, start_date: str = None) -> list[dict]:
        """
        Mon-Sun week window for SchedulingData/GetSchedulingData.
        Discovery (2026-06-28): response is a flat JSON list of event objects
        (~63 keys each, no wrapper envelope).

        Args:
            start_date: Any ISO date YYYY-MM-DD inside the desired week.
                        The method snaps backward to the most recent Monday
                        (<= that date) and queries Mon 04:00:00Z → next Mon 03:59:59.999Z.
                        Defaults to the current week.
        """
        if start_date is None:
            ref = datetime.utcnow().date()
        else:
            ref = datetime.strptime(start_date, "%Y-%m-%d").date()

        # Snap to Monday (weekday 0 = Monday)
        monday = ref - timedelta(days=ref.weekday())
        start_dt = datetime(monday.year, monday.month, monday.day, 4, 0, 0)
        end_dt = start_dt + timedelta(days=7) - timedelta(milliseconds=1)

        start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S.999Z")

        result = await self._api_call(
            "POST",
            "SchedulingData/GetSchedulingData",
            data={"startDate": start_iso, "endDate": end_iso},
        )
        if isinstance(result, list):
            return result
        return result.get("data", result.get("Data", []))

    async def get_schedule_range(self, start_iso: str, end_iso: str) -> list[dict]:
        """
        Arbitrary date range — posts the start/end pair directly and returns
        the list as-is. Both timestamps must be ISO 8601 UTC strings.
        """
        result = await self._api_call(
            "POST",
            "SchedulingData/GetSchedulingData",
            data={"startDate": start_iso, "endDate": end_iso},
        )
        if isinstance(result, list):
            return result
        return result.get("data", result.get("Data", []))

    async def get_block_schedule(self, start_iso: str, end_iso: str) -> list[dict]:
        """
        POST /BlockSchedule/GetBlockScheduleData — OR block-time schedule for a
        date range.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Body:
            {"startDate": <iso>, "endDate": <iso>}
        Same two-key ISO 8601 UTC pair as SchedulingData/GetSchedulingData —
        callers should follow the .000Z/.999Z 04:00Z-anchor convention (see
        get_schedule_day) unless discovery proves a different anchor. An empty
        list is a valid result (no blocks in the window).
        """
        result = await self._api_call(
            "POST",
            "BlockSchedule/GetBlockScheduleData",
            data={"startDate": start_iso, "endDate": end_iso},
        )
        return self._unwrap_list(result, "BlockSchedule/GetBlockScheduleData")

    async def get_anesthesia_schedule(self, start_iso: str, end_iso: str) -> list[dict]:
        """
        GET /AnesthesiaScheduling/ with startDate/endDate query params —
        anesthesia-side schedule for a date range.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Params:
            {"startDate": <iso>, "endDate": <iso>}
        ISO 8601 UTC strings, same convention as the SchedulingData windows.
        The trailing slash on the controller root is the observed capture form.
        An empty list is a valid result.
        """
        result = await self._api_call(
            "GET",
            "AnesthesiaScheduling/",
            params={"startDate": start_iso, "endDate": end_iso},
        )
        return self._unwrap_list(result, "AnesthesiaScheduling/")

    async def get_case_details(self, case_id: int) -> dict:
        """
        GET /CaseSummary/{case_id} — returns a ~54-key case detail dict.

        Discovery (2026-06-28, verified live):
          - Working form: GET /api/CaseSummary/{id} (id as bare last path segment).
          - Alternate that also works: GET /api/CaseSummary/Get?id={id}.
          - ALL POST variants of /api/CaseSummary/Get return HTTP 400 — do not use.
          - Response keys include: caseSummaryId, patientId, organizationId,
            procedureDt, primaryPhysicianId, referringPhysicianId, referringPhysicianName,
            caseAccountNumber, caseCode, primaryProcedure, roomId, roomName, +29 more.
        """
        return await self._api_call("GET", f"CaseSummary/{int(case_id)}")

    async def get_patient_details(self, patient_id: int) -> dict:
        """
        GET /PatientData/Gemini/GetPatient/{patient_id} — returns a ~62-key demographics dict.

        Discovery (2026-06-28, verified live):
          - Lives under the same Gemini namespace as search_patient.
          - Patient ID as the last path segment.
          - Response keys include: patientId, firstName, lastName, middleInitial,
            dateOfBirth, gender, email, primaryAccountNumber, maritalStatus,
            race, genderIdentity, pronouns, ethnicity, +37 more
            (address/phone/insurance/emergency-contact fields).
        """
        return await self._api_call("GET", f"PatientData/Gemini/GetPatient/{int(patient_id)}")

    async def get_patient_demographics(self, patient_id: int) -> dict:
        """
        Full patient demographics (~62-65 key dict). Verified live 2026-06-30 (HTTP 200).

        Alias of get_patient_details — same GET /PatientData/Gemini/GetPatient/{id}
        endpoint (richest patient object: patientId, firstName, lastName,
        dateOfBirth, gender, address, patientPhones, accountNumbers, ...).
        Provided under the 'demographics' name for the patient-record reads API.
        """
        return await self.get_patient_details(int(patient_id))

    async def get_patient_facesheet(self, patient_id: int) -> dict:
        """
        GET /PatientData/GetFaceSheetPatientInfo/{patient_id}/{patient_id}
        Face sheet: demographics + contact + INSURANCE in one call.

        Verified live 2026-06-30 (HTTP 200). Shape:
            {patient, patientContactInfo, patientInsurances}
        Both path args are the patientId (the 2nd segment echoes patientId;
        passing patientId for both is the confirmed working form).
        """
        return await self._api_call(
            "GET",
            f"PatientData/GetFaceSheetPatientInfo/{int(patient_id)}/{int(patient_id)}",
        )

    async def get_patient_cases(self, patient_id: int) -> list[dict]:
        """
        GET /CaseSummary/GetFaceSheetCaseListByPatient/{patient_id}
        Lean per-patient case list (no notification-history clutter).

        Verified live 2026-06-30 (HTTP 200). Returns a JSON list; each row:
            {caseSummaryId, patientId, caseAccountNumber, caseCode, caseStatus,
             caseStatusId, caseType, physicianFirstName, physicianLastName,
             physicianMiddleInitial, physicianTitle, physicianPersonId,
             procedureNameList, procedureStartDt, procedureStopDt, laterality,
             appointmentId, externalCaseId, sourceIdentifier, ...}
        """
        result = await self._api_call(
            "GET",
            f"CaseSummary/GetFaceSheetCaseListByPatient/{int(patient_id)}",
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_case_information(self, case_id: int) -> dict:
        """
        GET /CaseSummary/GetCaseInformation/{case_id}/false
        Per-case detail incl. insurance vs self-pay procedure counts.

        Verified live 2026-06-30 (HTTP 200). Shape:
            {admitTime, dischargeTime, dateOfService, timeZoneSpecificDOS,
             procedureText, numberOfInsuranceProcedures, numberOfSelfPayProcedures,
             patientMRN, caseType, caseStatus, caseSummaryId, patientId}
        The trailing /false path segment is required (confirmed working form).
        """
        return await self._api_call(
            "GET",
            f"CaseSummary/GetCaseInformation/{int(case_id)}/false",
        )

    async def get_case_secondary_physicians(self, case_id: int) -> str:
        """
        GET /CaseSummary/GetSecondaryPhysicians/{case_id}
        Secondary physicians for a case as a bare string (may be empty).

        Verified live 2026-06-30 (HTTP 200, returned a short string). Returns the
        raw string; an empty string is a valid result (no secondary physicians).
        """
        result = await self._api_call(
            "GET",
            f"CaseSummary/GetSecondaryPhysicians/{int(case_id)}",
        )
        if isinstance(result, str):
            return result
        # _api_call wraps non-JSON bodies as {"_raw": <str>}; unwrap to a string.
        if isinstance(result, dict) and "_raw" in result:
            return result["_raw"]
        return "" if result is None else str(result)

    async def get_case_procedures(self, case_id: int) -> list[dict]:
        """
        GET /RecordHeader/{case_id}/GetCaseProcedurebyCase
        Procedures attached to a case (rows carry the caseProcedureId consumed
        by get_case_diagnoses).

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) An empty list is a valid
        result (no procedures on the case).
        """
        result = await self._api_call(
            "GET", f"RecordHeader/{int(case_id)}/GetCaseProcedurebyCase"
        )
        return self._unwrap_list(
            result, "RecordHeader/GetCaseProcedurebyCase")

    async def get_case_record(self, case_id: int) -> dict:
        """
        GET /RecordHeader/{case_id}/GetPatientRecordByCaseSummaryId
        Chart/record header for a case.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Response returned as-is
        (shape not yet documented).
        """
        return await self._api_call(
            "GET", f"RecordHeader/{int(case_id)}/GetPatientRecordByCaseSummaryId"
        )

    async def get_case_diagnoses(self, case_id: int, procedure_id: int) -> list[dict]:
        """
        GET /RecordHeader/{case_id}/{procedure_id}/GetDiagnosisbyCaseAndProcedure
        Diagnoses linked to one procedure on a case.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) EXPERIMENTAL: the two-slot
        path order is ASSUMED to be (caseSummaryId, caseProcedureId) —
        procedure_id should be a caseProcedureId taken from get_case_procedures
        rows, not a catalog procedure id. An empty list is a valid result.
        """
        result = await self._api_call(
            "GET",
            f"RecordHeader/{int(case_id)}/{int(procedure_id)}/GetDiagnosisbyCaseAndProcedure",
        )
        return self._unwrap_list(
            result, "RecordHeader/GetDiagnosisbyCaseAndProcedure")

    async def get_worklist_case_details(self, case_id: int, module_id: int = 1020) -> dict:
        """
        GET /WorklistCaseDetails/GetCaseDetails/{case_id}/{module_id}
        Worklist view of a case for one clinical module.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) The 2nd path slot was ALWAYS
        a module id in the HAR (observed values 1010/1020/1030/1060/1080 —
        Pre-Admission/Pre-Operative/Operative/Recovery/Post-Operative per the
        SSRS_REPORTS map). Default 1020 = Pre-Operative. Response returned
        as-is (shape not yet documented).
        """
        return await self._api_call(
            "GET",
            f"WorklistCaseDetails/GetCaseDetails/{int(case_id)}/{int(module_id)}",
        )

    async def get_facesheet_case_detail(self, patient_id: int, case_id: int) -> dict:
        """
        GET /PatientFacesheet/GetCaseDetailInformation/{patient_id}/{case_id}
        Face-sheet case-detail block for one case.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session; slot order (patientId, caseId)
        was observed directly in the HAR.) Response returned as-is.
        """
        return await self._api_call(
            "GET",
            f"PatientFacesheet/GetCaseDetailInformation/{int(patient_id)}/{int(case_id)}",
        )

    async def get_cover_page(
        self, patient_id: int, case_id: int, module_id: int = 1010
    ) -> dict:
        """
        GET /PatientFacesheet/GetCoverPageInformation/{patient_id}/{case_id}/{module_id}
        Chart cover-page info for a case within one clinical module.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Default module_id=1010 =
        Pre-Admission. Response returned as-is (shape not yet documented).
        """
        return await self._api_call(
            "GET",
            f"PatientFacesheet/GetCoverPageInformation/{int(patient_id)}/{int(case_id)}/{int(module_id)}",
        )

    async def get_primary_physician(self, case_id: int) -> Any:
        """
        GET /Staff/GetPrimaryPhysicianFromCase?caseSummaryId={case_id}
        Primary physician for a case.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Response returned as-is — may
        be a dict or a bare string; a non-JSON body arrives as {"_raw": <str>}
        per _api_call's contract.
        """
        return await self._api_call(
            "GET",
            "Staff/GetPrimaryPhysicianFromCase",
            params={"caseSummaryId": int(case_id)},
        )

    async def get_consent_signed(self, case_id: int) -> Any:
        """
        GET /ConsentClinical/IsAllConsentsSignedOfACase/{case_id}
        Whether every consent on the case is signed.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Expected to be a bare JSON
        boolean, but returned as-is — do not assume bool until live-verified.
        """
        return await self._api_call(
            "GET", f"ConsentClinical/IsAllConsentsSignedOfACase/{int(case_id)}"
        )

    async def get_risk_assessments(self, case_id: int, second_id: int) -> list[dict]:
        """
        GET /v2/CaseSummary/{case_id}/RiskAssessment/{kind}/{second_id}
        for each kind in [DvtPrevention, DvtRisk, Fall, Fire, Ponv, StopBang].

        HAR-derived 2026-07-01; NOT yet live-verified. (Routes returned HTTP 200
        in the capture under a live SIS session.) EXPERIMENTAL: second_id was
        observed in the HAR as a module-id-like value (e.g. 1020) — its exact
        semantics are unconfirmed; pass the value observed for your module
        context.

        HTTP 204 / empty body means the assessment is simply NOT RECORDED for
        the case — a valid result, not an error. (_api_call maps any empty body
        to {}, so an empty dict here is read as "not recorded".)

        Returns one entry per kind, honestly tri-stated:
            {"kind": str, "recorded": True, "data": <payload>}  # something stored
            {"kind": str, "recorded": False}                    # empty/204 — not recorded
            {"kind": str, "recorded": None, "error": str}       # call failed — unknown
        """
        kinds = ["DvtPrevention", "DvtRisk", "Fall", "Fire", "Ponv", "StopBang"]
        out: list[dict] = []
        for kind in kinds:
            try:
                result = await self._api_call(
                    "GET",
                    f"v2/CaseSummary/{int(case_id)}/RiskAssessment/{kind}/{int(second_id)}",
                )
            except Exception as e:
                out.append({"kind": kind, "recorded": None, "error": str(e)})
                continue
            if result == {}:
                out.append({"kind": kind, "recorded": False})
            else:
                out.append({"kind": kind, "recorded": True, "data": result})
        return out

    async def get_case_vitals(self, case_id: int) -> dict:
        """
        Case vitals / basic-module info — two calls combined:
            GET /BasicModuleInfo/GetBasicModuleInfoByCaseSummaryId/{case_id}
            GET /BasicModuleInfo/GetHtWtLastUpdated/{case_id}

        HAR-derived 2026-07-01; NOT yet live-verified. (Both routes returned
        HTTP 200 in the capture under a live SIS session.)

        Returns:
            {"basic_module": <payload>, "ht_wt_last_updated": <payload>}
        Payloads returned as-is; either may legitimately be empty. If either
        call fails, the exception propagates — no half-fabricated result.
        """
        basic = await self._api_call(
            "GET", f"BasicModuleInfo/GetBasicModuleInfoByCaseSummaryId/{int(case_id)}"
        )
        ht_wt = await self._api_call(
            "GET", f"BasicModuleInfo/GetHtWtLastUpdated/{int(case_id)}"
        )
        return {"basic_module": basic, "ht_wt_last_updated": ht_wt}

    async def get_dates_of_service(self, patient_id: int) -> list[dict]:
        """
        GET /PatientNoteCategories/GetAllPatientDOS/{patient_id}
        Dates-of-service per patient (maps each DOS to its caseSummaryId).

        Verified live 2026-06-30 (HTTP 200). Returns a JSON list; each row:
            {caseSummaryId, procedureDt}
        """
        result = await self._api_call(
            "GET",
            f"PatientNoteCategories/GetAllPatientDOS/{int(patient_id)}",
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_note_categories(self) -> list[dict]:
        """
        GET /PatientNoteCategories/List
        Note-category lookup table (no PHI, no args).

        Verified live 2026-06-30 (HTTP 200). Each row:
            {noteCategoryId, name, code, activeTf, modifiableTf, jobId,
             organizationId, sourceIdentifier}
        """
        result = await self._api_call("GET", "PatientNoteCategories/List")
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_staff_roster(self, org_id: int = 3) -> list[dict]:
        """
        GET /StaffList/GetStaffForOrganization/{org_id}/false
        Org staff roster (no PHI — staff directory). org_id=3 = Main Line Surgical Center.

        Verified live 2026-06-30 (HTTP 200, 12 rows for org 3). Each row:
            {staffId, personId, userId, roleId, roleName, roleGroup, firstName,
             middleInitial, lastName, fullName, title, primaryPhone,
             emailAddress, sourceIdentifier}
        """
        result = await self._api_call(
            "GET", f"StaffList/GetStaffForOrganization/{int(org_id)}/false"
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_patient_notes(self, patient_id: int) -> list[dict]:
        """
        GET /PatientNotes/GetPatientNotes?patientId={patient_id}
        Patient clinical/administrative notes.

        Verified live 2026-07-01 (HTTP 200; pid=175 returned 2 rows). Returns a
        JSON list; each row (21 keys):
            {patientNoteId, patientId, note, title, categoryName, categoryCode,
             noteCategoryId, associatedCase, createdDate, createdByAlias, usrId,
             updatedDt, importantFlagTf, importantFlagChangedByOtherUser,
             markedForDelete, firstName, lastName, middleInitial, organizationId,
             sourceIdentifier, value}
        An empty list is a valid result (patient has no notes).
        """
        result = await self._api_call(
            "GET",
            "PatientNotes/GetPatientNotes",
            params={"patientId": int(patient_id)},
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_patient_allergies(self, patient_id: int) -> list[dict]:
        """
        GET /PatientData/{patient_id}/allergyHistory
        Patient allergy history.

        Verified live 2026-07-01 (HTTP 200; pid=1 returned 5 rows). Returns a
        JSON list; each row (34 keys):
            {patientAllergyHistoryId, patientId, allergen, allergenId,
             allergyCategory, reactions, reactionsText, reactionName, severity,
             onsetDt, noKnown, activeTf, caseSummaryId, drugCount, latexCount,
             nonDrugCount, drugAddedMode, fdbPickListConceptType, fdbPickListId,
             isAdded, isUpdated, createdBy, createdUser, createdDate, modifiedBy,
             modifiedDt, moduleId, organizationId, patientNotes,
             patientAllergyAuditHistoryDetails, patientLatexQuestionnaires,
             sourceIdentifier, value}
        An empty list is a valid result. NOTE: the patient demographics object
        also exposes noKnownDrugAllergyTf / noKnownLatexAllergyTf flags; this
        endpoint returns the actual allergy ROWS.
        """
        result = await self._api_call(
            "GET",
            f"PatientData/{int(patient_id)}/allergyHistory",
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_patient_medications(self, patient_id: int) -> list[dict]:
        """
        Patient medication list, resolved CASE-by-CASE.

        SIS medications are case-scoped, not patient-scoped: there is no
        patient-level med list endpoint. This method resolves the patient's
        cases (get_patient_cases → caseSummaryId per case) then queries meds per
        case via two endpoints and merges the results, tagging each row with its
        source case id.

        Per-case endpoints (both verified live 2026-07-01 to return HTTP 200):
            GET /Medication/{caseSummaryId}/0
            GET /Depletion/GetMedicationsByCaseSummaryId/{caseSummaryId}

        ⚠️ MODULE UNUSED BY THIS PRACTICE (concluded 2026-07-02): an exhaustive
        sweep of caseSummaryIds 1–500 against BOTH endpoints (1000 calls, 0
        errors) returned HTTP 200 with an EMPTY list every time, on top of the
        120-real-case scan of 2026-07-01 and the user's own HAR session (the SIS
        app itself received `[]` for every Medication call it made). Svigg's Rx
        page (apps/rx/PtntRx.htm) was likewise empty (204) for the sampled
        patient — the practice does not maintain structured med lists in EITHER
        EMR; meds live in chart notes/case-pack documents instead. The route
        wiring is verified (200, list) but the per-row field shape remains
        unknowable until a populated case exists. Do NOT build clinical features
        on this field; treat a non-empty return as a new discovery to verify.

        Returns a flat JSON list of medication rows (each augmented with a
        `_caseSummaryId` marker), or an empty list. Never fabricates data.
        """
        pid = int(patient_id)
        try:
            cases = await self.get_patient_cases(pid)
        except Exception as e:
            logger.warning("get_patient_medications: case resolution failed: %s", e)
            raise   # surface the failure; do NOT read as 'no medications'

        meds: list[dict] = []
        for case in cases if isinstance(cases, list) else []:
            cs = case.get("caseSummaryId") if isinstance(case, dict) else None
            if cs is None:
                continue
            for ep in (
                f"Medication/{int(cs)}/0",
                f"Depletion/GetMedicationsByCaseSummaryId/{int(cs)}",
            ):
                try:
                    rows = await self._api_call("GET", ep)
                except Exception as e:
                    logger.debug("get_patient_medications: %s failed: %s", ep, e)
                    continue
                if isinstance(rows, dict):
                    rows = rows.get("Data", rows.get("data", []))
                if isinstance(rows, list):
                    for r in rows:
                        if isinstance(r, dict):
                            r.setdefault("_caseSummaryId", int(cs))
                            meds.append(r)
        return meds

    async def get_hp_previously_signed(self, patient_id: int) -> Any:
        """
        GET /HistoryPhysicalClinical/PreviouslySignedForPatient/{patient_id}
        Previously signed H&P (History & Physical) documents for a patient.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Response returned as-is; an
        empty result is valid (no previously signed H&P on file).
        """
        return await self._api_call(
            "GET",
            f"HistoryPhysicalClinical/PreviouslySignedForPatient/{int(patient_id)}",
        )

    async def get_chart_attachments_meta(self, patient_id: int, case_id: int) -> list[dict]:
        """
        GET /Attachments/v2/AllAttachmentsMetaData/{patient_id}/{case_id}
        Metadata for chart attachments (documents/scans) — metadata only, this
        does NOT download file contents.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) EXPERIMENTAL: the
        (patientId, caseId) slot order is ASSUMED from adjacent
        PatientFacesheet routes, not confirmed from the HAR. An empty list is a
        valid result (no attachments).
        """
        result = await self._api_call(
            "GET",
            f"Attachments/v2/AllAttachmentsMetaData/{int(patient_id)}/{int(case_id)}",
        )
        return self._unwrap_list(
            result, "Attachments/v2/AllAttachmentsMetaData")

    async def get_attachment_types(self) -> list[dict]:
        """
        GET /ConfigAttachmentType/GetAllAttachmentTypes
        Attachment-type lookup table (reference data, no PHI, no args).

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.)
        """
        result = await self._api_call(
            "GET", "ConfigAttachmentType/GetAllAttachmentTypes"
        )
        return self._unwrap_list(
            result, "ConfigAttachmentType/GetAllAttachmentTypes")

    async def get_chart_attachment_consent_info(self, patient_id: int) -> dict:
        """
        GET /PatientFacesheet/GetPatientChartAttachmentAndConsentInfoV2/{patient_id}
        Combined chart-attachment + consent status block for a patient.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) EXPERIMENTAL: the single path
        slot was observed as a patientId in the HAR, but that reading is
        unconfirmed from this client. Response returned as-is.
        """
        return await self._api_call(
            "GET",
            f"PatientFacesheet/GetPatientChartAttachmentAndConsentInfoV2/{int(patient_id)}",
        )

    # ------------------------------------------------------------------
    # Billing / AR / financials (verified live 2026-06-30, HTTP 200)
    #
    # All endpoints below were called against the live SIS Complete API on
    # 2026-06-30 and the response shapes were confirmed PHI-safely (key names
    # only). Comments document the exact contract each method depends on.
    # ------------------------------------------------------------------

    async def get_patient_billing_ledger(self, patient_id: int) -> dict:
        """
        GET /CaseToChargeComplex/GetFaceSheetLedgerModelByPatient/{patient_id}
        Per-patient billing ledger + aging breakdown (~130KB).

        Verified live 2026-06-30 (HTTP 200). Shape:
            {
              "faceSheetLedgerCharges": [ {...per-charge line item...}, ... ],
              "aging": {
                  "account":   {...},  # sub-dicts, NOT scalars
                  "patient":   {...},
                  "insurance": {...},
                  "total":     {"totalCharges", "totalPayments", "totalWriteOffs",
                                "patRespCoins", "patRespCopay", "patRespDeductible"}
              }
            }
        Returns the raw dict as-is. NOTE: aging.total is a *breakdown* dict of
        numeric floats — a single net balance must be derived (see
        get_patient_balance), it is not a pre-computed scalar here.
        """
        return await self._api_call(
            "GET",
            f"CaseToChargeComplex/GetFaceSheetLedgerModelByPatient/{int(patient_id)}",
        )

    async def get_ledger_aging_by_case(self, case_id: int) -> dict:
        """
        GET /CaseToChargeComplex/GetFaceSheetLedgerAgingByCase/{case_id}
        Aging summary for a single case.

        Verified live 2026-06-30 (HTTP 200). Shape:
            { "account": {...}, "patient": {...}, "insurance": {...},
              "total": {"totalCharges", "totalPayments", "totalWriteOffs",
                        "patRespCoins", "patRespCopay", "patRespDeductible"} }
        All `total` values are numeric floats.
        """
        return await self._api_call(
            "GET",
            f"CaseToChargeComplex/GetFaceSheetLedgerAgingByCase/{int(case_id)}",
        )

    async def get_patient_procedures(self, patient_id: int) -> list[dict]:
        """
        GET /CaseToCharge/GetAllCaseProcedureDOSbyPatient/{patient_id}
        Procedure / date-of-surgery list for a patient.

        Verified live 2026-06-30 (HTTP 200). Returns a JSON list; each row:
            {caseSummaryId, dateOfSurgery, procedureDescription, procedureId,
             isPosted, hasCharge, physicianId, physicianName, sourceIdentifier}
        """
        result = await self._api_call(
            "GET",
            f"CaseToCharge/GetAllCaseProcedureDOSbyPatient/{int(patient_id)}",
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_statements_tracker(self) -> list[dict]:
        """
        GET /PatientStatementsTracker/GetTrackerData
        Practice-wide patient statement tracker (one row per patient with a
        statement-eligible balance).

        Verified live 2026-06-30 (HTTP 200). Returns a JSON list; each row:
            {patientId, primaryAccountNumber, accountBalance, patientBalance,
             minBalance, billedStatementCount, guarantor, guarantorId,
             organizationId, organizationName, patientName, sourceIdentifier, ...}
        accountBalance and patientBalance are numeric floats. This is the
        primary source for a pre-computed patient-level balance scalar.
        """
        result = await self._api_call("GET", "PatientStatementsTracker/GetTrackerData")
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_payment_types(self) -> list[dict]:
        """
        GET /PaymentType/GetPaymentTypes
        Reference list of payment types (no PHI).

        Verified live 2026-06-30 (HTTP 200). Each row:
            {paymentTypeId, paymentType, sourceIdentifier}
        """
        result = await self._api_call("GET", "PaymentType/GetPaymentTypes")
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_patient_insurance(self, patient_id: int) -> list[dict]:
        """
        GET /Insurance/PatientInsurances/{patient_id}
        Insurance records for a patient.

        Verified live 2026-06-30 (HTTP 200). Returns a JSON list; each row:
            {carrierId, carrier, displayName, planName, groupNumber,
             authorizationNumber, insuredId, effectiveFrom, effectiveTo,
             isActive, patientId, ...}
        """
        result = await self._api_call(
            "GET", f"Insurance/PatientInsurances/{int(patient_id)}"
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_patient_guarantors(self, patient_id: int) -> list[dict]:
        """
        GET /PatientGuarantor/GetPatientGuarantors/{patient_id}
        Guarantor records for a patient. An empty list is a valid result
        (many patients are self-guarantor with no separate row).

        Verified live 2026-06-30 (HTTP 200, returned []).
        """
        result = await self._api_call(
            "GET", f"PatientGuarantor/GetPatientGuarantors/{int(patient_id)}"
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_ar_tracker(
        self,
        organization_id: int = 3,
        page_number: int = 1,
        page_size: int = 25,
        view_by: int = 0,
    ) -> list[dict]:
        """
        POST /RCMTracker/RCMTrackerDetails
        Practice-wide accounts-receivable tracker (revenue-cycle mgmt rows).

        Verified live 2026-06-30 (HTTP 200, ~690 rows for orgId=3). Body:
            {"organizationId": 3, "pageNumber": 1, "pageSize": 25, "viewBy": 0}
        Each row:
            {caseSummaryId, transactionId, patientId, dateOfSurgery, balance,
             postDate, responsibleParty, patientName, primaryAccountNumber,
             followUpDate, status, classification, aging, claimStatus,
             denialStatus, payerRole, selfPay, ...}
        balance is a numeric float. organizationId=3 = Main Line Surgical Center.
        """
        result = await self._api_call(
            "POST",
            "RCMTracker/RCMTrackerDetails",
            data={
                "organizationId": int(organization_id),
                "pageNumber": int(page_number),
                "pageSize": int(page_size),
                "viewBy": int(view_by),
            },
        )
        if isinstance(result, list):
            return result
        return result.get("Data", result.get("data", []))

    async def get_case_charges(self, case_id: int) -> list[dict]:
        """
        GET /RCMTracker/RCMGetAllChargesByCaseSummaryId/{case_id}
        All charge rows for a case (revenue-cycle view).

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) An empty list is a valid
        result (no charges posted on the case).
        """
        result = await self._api_call(
            "GET", f"RCMTracker/RCMGetAllChargesByCaseSummaryId/{int(case_id)}"
        )
        return self._unwrap_list(
            result, "RCMTracker/RCMGetAllChargesByCaseSummaryId")

    async def get_case_responsible_parties(self, case_id: int) -> dict:
        """
        Responsible parties for a case — two calls combined:
            GET /RCMTracker/RCMInsuranceResponsibleParty/{case_id}
            GET /RCMTracker/RCMGuarantorResponsibleParty/{case_id}

        HAR-derived 2026-07-01; NOT yet live-verified. (Both routes returned
        HTTP 200 in the capture under a live SIS session.)

        Returns:
            {"insurance": <payload>, "guarantor": <payload>}
        Payloads returned as-is; either may legitimately be empty (e.g. pure
        self-pay case → empty insurance block). The insurance rows are the
        expected source of the party id consumed by get_charges_for_insurance.
        If either call fails, the exception propagates.
        """
        insurance = await self._api_call(
            "GET", f"RCMTracker/RCMInsuranceResponsibleParty/{int(case_id)}"
        )
        guarantor = await self._api_call(
            "GET", f"RCMTracker/RCMGuarantorResponsibleParty/{int(case_id)}"
        )
        return {"insurance": insurance, "guarantor": guarantor}

    async def get_charges_for_insurance(self, party_id: int, case_id: int) -> list[dict]:
        """
        GET /RCMTracker/RCMChargesForInsurance/{party_id}/{case_id}
        Charges attributed to one insurance responsible party on a case.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) EXPERIMENTAL: the FIRST path
        slot is assumed to be a responsible-party/carrier id (likely taken from
        get_case_responsible_parties' insurance rows) — unconfirmed. An empty
        list is a valid result.
        """
        result = await self._api_call(
            "GET",
            f"RCMTracker/RCMChargesForInsurance/{int(party_id)}/{int(case_id)}",
        )
        return self._unwrap_list(
            result, "RCMTracker/RCMChargesForInsurance")

    async def get_insurance_verification_queue(
        self, dos_from: str, dos_to: str, page: int = 1
    ) -> list[dict]:
        """
        POST /InsuranceTracker/GetTrackerData
        Insurance-verification work queue for a date-of-service window.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) BEST-EFFORT BODY: the HAR
        capture yielded the request KEY NAMES only — the default values below
        (empty filter lists, chargesToPullEnum 0, empty sort fields) are
        best-effort guesses, NOT observed values. If the server rejects them it
        surfaces as a normal HTTP 4xx/5xx exception from _api_call rather than
        being masked. Body:
            {"doSFrom", "doSTo", "physicianIds": [], "specialtyIds": [],
             "appointmentTypeIds": [], "caseFlagIds": [], "chargesToPullEnum": 0,
             "orderByColumn": "", "orderDir": "", "pageNumber": page}
        """
        result = await self._api_call(
            "POST",
            "InsuranceTracker/GetTrackerData",
            data={
                "doSFrom": dos_from,
                "doSTo": dos_to,
                "physicianIds": [],
                "specialtyIds": [],
                "appointmentTypeIds": [],
                "caseFlagIds": [],
                "chargesToPullEnum": 0,
                "orderByColumn": "",
                "orderDir": "",
                "pageNumber": int(page),
            },
        )
        return self._unwrap_list(result, "InsuranceTracker/GetTrackerData")

    async def get_billing_tracker(self) -> list[dict]:
        """
        GET /InsuranceBillingTracker/GetTrackerData
        Practice-wide insurance billing tracker.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) An empty list is a valid
        result.
        """
        result = await self._api_call("GET", "InsuranceBillingTracker/GetTrackerData")
        return self._unwrap_list(
            result, "InsuranceBillingTracker/GetTrackerData")

    async def get_charge_entry_tracker(self, date_time: str = None) -> list[dict]:
        """
        POST /ChargeEntryTracker/GetTrackerData — charge-entry work queue.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Body mirrors the
        UnsignedCasesTracker contract that get_unsigned_cases sends, exactly:
            {"dateTime": <"%m/%d/%Y 00:00:00 -04:00">, "pageNumber": 1,
             "columnId": 3, "orderType": 0}
        date_time defaults to today in that local-midnight/-04:00 string form.
        An empty list is a valid result.
        """
        if date_time is None:
            date_time = datetime.now().strftime("%m/%d/%Y 00:00:00 -04:00")
        result = await self._api_call(
            "POST",
            "ChargeEntryTracker/GetTrackerData",
            data={"dateTime": date_time, "pageNumber": 1, "columnId": 3, "orderType": 0},
        )
        return self._unwrap_list(result, "ChargeEntryTracker/GetTrackerData")

    async def get_clinical_doc_tracker(self, date_time: str = None) -> list[dict]:
        """
        POST /ClinicalDocumentationTracker/GetTrackerData — clinical
        documentation work queue.

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.) Body mirrors the
        UnsignedCasesTracker contract that get_unsigned_cases sends, exactly:
            {"dateTime": <"%m/%d/%Y 00:00:00 -04:00">, "pageNumber": 1,
             "columnId": 3, "orderType": 0}
        date_time defaults to today in that local-midnight/-04:00 string form.
        An empty list is a valid result.
        """
        if date_time is None:
            date_time = datetime.now().strftime("%m/%d/%Y 00:00:00 -04:00")
        result = await self._api_call(
            "POST",
            "ClinicalDocumentationTracker/GetTrackerData",
            data={"dateTime": date_time, "pageNumber": 1, "columnId": 3, "orderType": 0},
        )
        return self._unwrap_list(
            result, "ClinicalDocumentationTracker/GetTrackerData")

    async def get_case_coordination(self, page: int = 1) -> dict:
        """
        POST /CaseCoordinationTracker/GetTrackerData (+ GetTrackerDataCount)
        Case-coordination request queue plus its total count.

        HAR-derived 2026-07-01; NOT yet live-verified. (Both routes returned
        HTTP 200 in the capture under a live SIS session.) Both endpoints
        receive the SAME body (empty filter lists; every status flag true
        except completedTf):
            {"recipientList": [], "requestTypes": [], "requestCategories": [],
             "physicianIdList": [], "patientIdList": [], "acknowledgedTf": true,
             "unacknowledgedTf": true, "requestedTf": true, "respondedTf": true,
             "completedTf": false, "sortBy": "", "sortOrder": "",
             "pageNumber": page}

        Returns:
            {"count": <GetTrackerDataCount payload as-is>,
             "rows": <GetTrackerData rows, envelope-unwrapped>}
        An empty rows list is a valid result. If either call fails, the
        exception propagates.
        """
        body = {
            "recipientList": [],
            "requestTypes": [],
            "requestCategories": [],
            "physicianIdList": [],
            "patientIdList": [],
            "acknowledgedTf": True,
            "unacknowledgedTf": True,
            "requestedTf": True,
            "respondedTf": True,
            "completedTf": False,
            "sortBy": "",
            "sortOrder": "",
            "pageNumber": int(page),
        }
        rows = await self._api_call(
            "POST", "CaseCoordinationTracker/GetTrackerData", data=body
        )
        rows = self._unwrap_list(
            rows, "CaseCoordinationTracker/GetTrackerData")
        count = await self._api_call(
            "POST", "CaseCoordinationTracker/GetTrackerDataCount", data=body
        )
        return {"count": count, "rows": rows}

    async def get_insurance_carriers(self) -> list[dict]:
        """
        GET /InsuranceCarrierObject/GetInsuranceCarriers
        Insurance-carrier lookup table (reference data, no args).

        HAR-derived 2026-07-01; NOT yet live-verified. (Route returned HTTP 200
        in the capture under a live SIS session.)
        """
        result = await self._api_call(
            "GET", "InsuranceCarrierObject/GetInsuranceCarriers"
        )
        return self._unwrap_list(
            result, "InsuranceCarrierObject/GetInsuranceCarriers")

    async def get_transaction_codes(self, type_id: int = None) -> list[dict]:
        """
        GET /TransactionCode/GetTransactionCodeList — transaction-code lookup
        table (reference data). When type_id is given, uses
        GET /TransactionCode/GetTransactionCodeListByType/{type_id} instead.

        HAR-derived 2026-07-01; NOT yet live-verified. (Both route forms
        returned HTTP 200 in the capture under a live SIS session.)
        """
        if type_id is None:
            endpoint = "TransactionCode/GetTransactionCodeList"
        else:
            endpoint = f"TransactionCode/GetTransactionCodeListByType/{int(type_id)}"
        result = await self._api_call("GET", endpoint)
        return self._unwrap_list(result, endpoint)

    async def get_total_transactions(self, patient_id: int) -> dict:
        """
        POST /CaseToCodeComplex/GetTotalTransactionsByPatient
        Patient-level transaction/balance summary.

        CONTRACT NOTE (solved live 2026-06-30): the request body MUST be a BARE
        JSON ARRAY `[patientId]`. Object bodies ({"patientId": id},
        {"patientId": id, "organizationId": 3}, and Pascal-case variants) ALL
        return HTTP 500 — only the array form works. Verified live 2026-06-30
        (HTTP 200). Shape (numeric float totals):
            {caseTransactionSummary, totalChargesByPatient, totalPaymentsByPatient,
             totalWriteOffsByPatient, totalDebitsByPatient, totalBalanceDueByPatient,
             totalBalanceDueNoAllocAmtByPatient, patientResponsibilityBalanceByPatient,
             insuranceResposibilityBalanceByPatient}
        """
        return await self._api_call(
            "POST",
            "CaseToCodeComplex/GetTotalTransactionsByPatient",
            data=[int(patient_id)],
        )

    async def get_patient_balance(self, patient_id: int) -> dict:
        """
        Resolve a single numeric patient-level balance + the source it came from.

        Balance-source decision (grounded in live 2026-06-30 probe of shapes):
          PRIMARY: PatientStatementsTracker/GetTrackerData → the row whose
            patientId matches → `accountBalance`. This is a PRE-COMPUTED numeric
            scalar maintained by SIS for statement generation, so it is the most
            reliable single patient-level balance. (`patientBalance` is also
            available and returned alongside for transparency.)
          FALLBACK: when the patient has no tracker row (no statement-eligible
            balance), derive a net from the per-patient ledger aging:
            net = aging.total.totalCharges - totalPayments - totalWriteOffs.
            The ledger endpoint always returns a `total` block of numeric floats.

        Returns:
            {
              "patient_id": int,
              "balance": float | None,        # the chosen single balance
              "balance_source": str,          # which field/endpoint it came from
              "account_balance": float | None,
              "patient_balance": float | None,
              "in_statements_tracker": bool,
              "note": str,
            }
        Never invents a number: if neither source yields a numeric value,
        balance is None and balance_source explains why.
        """
        pid = int(patient_id)

        # --- PRIMARY: statements tracker (pre-computed scalar) -------------
        try:
            tracker = await self.get_statements_tracker()
        except Exception as e:
            tracker = []
            logger.warning("get_patient_balance: statements tracker failed: %s", e)

        row = None
        if isinstance(tracker, list):
            for r in tracker:
                if isinstance(r, dict) and r.get("patientId") == pid:
                    row = r
                    break

        if row is not None:
            acct_bal = row.get("accountBalance")
            pat_bal = row.get("patientBalance")
            bal = acct_bal if isinstance(acct_bal, (int, float)) and not isinstance(acct_bal, bool) else None
            if bal is not None:
                return {
                    "patient_id": pid,
                    "balance": bal,
                    "balance_source": "PatientStatementsTracker.accountBalance",
                    "account_balance": acct_bal,
                    "patient_balance": pat_bal,
                    "in_statements_tracker": True,
                    "note": "Pre-computed account balance from the SIS statements tracker.",
                }

        # --- FALLBACK: derive net from per-patient ledger aging total -----
        try:
            ledger = await self.get_patient_billing_ledger(pid)
        except Exception as e:
            logger.warning("get_patient_balance: ledger fallback failed: %s", e)
            return {
                "patient_id": pid,
                "balance": None,
                "balance_source": "unavailable",
                "account_balance": None,
                "patient_balance": None,
                "in_statements_tracker": False,
                "note": f"Not in statements tracker and ledger fetch failed: {e}",
            }

        total = {}
        if isinstance(ledger, dict):
            aging = ledger.get("aging") or {}
            if isinstance(aging, dict):
                total = aging.get("total") or {}

        def _num(v):
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        charges = _num(total.get("totalCharges")) if isinstance(total, dict) else None
        payments = _num(total.get("totalPayments")) if isinstance(total, dict) else None
        writeoffs = _num(total.get("totalWriteOffs")) if isinstance(total, dict) else None

        if charges is not None:
            net = charges - (payments or 0.0) - (writeoffs or 0.0)
            return {
                "patient_id": pid,
                "balance": net,
                "balance_source": "ledger.aging.total (totalCharges - totalPayments - totalWriteOffs)",
                "account_balance": None,
                "patient_balance": None,
                "in_statements_tracker": False,
                "note": "Not in statements tracker; net balance derived from ledger aging totals.",
            }

        # Neither source produced a number — be honest, never fabricate.
        return {
            "patient_id": pid,
            "balance": None,
            "balance_source": "unavailable",
            "account_balance": None,
            "patient_balance": None,
            "in_statements_tracker": False,
            "note": "No statements-tracker row and ledger aging had no numeric totals.",
        }

    # ------------------------------------------------------------------
    # SSRS report PDFs (read-only fetch; leaves an access-audit row in SIS)
    # ------------------------------------------------------------------

    #: Known SSRS chart reports: name -> {module_id, audit_label, body_module}.
    #: body_module=False → the HAR capture for that report sent {"CaseID": ...}
    #: ONLY (no ModuleID key in the body); we match each capture exactly.
    SSRS_REPORTS = {
        "Pre-Admission Print Out": {
            "module_id": 1010,
            "audit_label": "Pre-Admission Print Out",
            "body_module": True,
        },
        "Pre-Operative Print Out": {
            "module_id": 1020,
            "audit_label": "Signed Pre-Operative Viewed",
            "body_module": True,
        },
        "Operative Print Out": {
            "module_id": 1030,
            "audit_label": "Operative Print Out",
            "body_module": False,
        },
        "Recovery Print Out": {
            "module_id": 1060,
            "audit_label": "Recovery Print Out",
            "body_module": False,
        },
        "Post-Operative Print Out": {
            "module_id": 1080,
            "audit_label": "Post-Operative Print Out",
            "body_module": True,
        },
    }

    async def get_report_pdf(
        self,
        report_name: str,
        case_id: int,
        module_id: int = None,
        out_dir: str = None,
    ) -> dict:
        """
        POST /Reports/SSRSReport/GetReportAsPdfAndAudit/{report_name}/false/{case_id}/{module_id}/{audit_label}
        Fetch a chart print-out as a PDF and save it to disk.

        HAR-derived 2026-07-01; NOT yet live-verified from this client. (All
        five report routes returned HTTP 200 application/pdf in the capture
        under a live SIS session.) The path-vs-body contract is FULLY RESOLVED
        from the HAR: the case id in the path always EQUALS the CaseID in the
        JSON body {"CaseID": <case_id>, "ModuleID": <module_id>} — except the
        Operative and Recovery captures, whose bodies carried CaseID only; this
        method reproduces each report's captured body exactly (see
        SSRS_REPORTS).

        ⚠️ AUDIT TRAIL (by design): the endpoint name ends in "AndAudit" — SIS
        writes an access-audit row on ITS side every time this is called. It is
        still a READ (no chart data is modified), and leaving that audit trail
        is correct and desirable; just don't call it in tight loops.

        Args:
            report_name: one of SSRS_REPORTS (e.g. "Pre-Operative Print Out").
                Unknown names are accepted ONLY with an explicit module_id
                (audit_label then falls back to the report name and the body
                includes ModuleID) — that combination is unverified.
            case_id:     caseSummaryId.
            module_id:   optional override for the mapped module id.
            out_dir:     save directory; defaults to <repo>/app/data/reports/
                         (resolves to /Users/shubh/n8n-office/app/data/reports/
                         in this checkout; created with parents if missing).
                         PHI CONTAINMENT: must resolve INSIDE <repo>/app/data/
                         (the gitignored PHI tree) — any other path is refused
                         with an error dict BEFORE the network call, so a
                         chart PDF can never be written into a git-tracked
                         (cloud-backed-up) directory.

        The response MUST be genuine PDF bytes (magic prefix "%PDF-"). Anything
        else — a JSON error, an HTML login page, an empty body — is NEVER saved
        to disk; an honest error dict is returned instead:
            {"error", "content_type", "body_preview", "report_name", "case_id"}
        On success:
            {"saved_path", "size_bytes", "report_name", "case_id"}
        Transport/auth/non-2xx errors raise via _api_call_pdf's normal path.
        """
        spec = self.SSRS_REPORTS.get(report_name)
        if spec is None and module_id is None:
            return {
                "error": (
                    f"Unknown report {report_name!r} and no module_id given — "
                    "refusing to guess. Known reports: "
                    + ", ".join(sorted(self.SSRS_REPORTS))
                ),
                "report_name": report_name,
                "case_id": int(case_id),
            }

        # PHI containment — validated BEFORE the network call so a bad path
        # never triggers a live fetch (and a SIS-side access-audit row).
        # <repo>/app/data is the gitignored PHI tree; sis_client.py lives at
        # <repo>/python/integrations/, so parents[2] is the repo root.
        phi_base = (Path(__file__).resolve().parents[2] / "app" / "data").resolve()
        if out_dir:
            target_dir = Path(out_dir).expanduser().resolve()
            if phi_base != target_dir and phi_base not in target_dir.parents:
                return {
                    "error": (
                        f"out_dir {str(target_dir)!r} is outside the "
                        f"allowlisted PHI directory {str(phi_base)!r} — "
                        "refusing to write a chart PDF (PHI) there; nothing "
                        "was fetched or saved."
                    ),
                    "report_name": report_name,
                    "case_id": int(case_id),
                }
        else:
            target_dir = phi_base / "reports"

        if spec is not None:
            eff_module_id = int(module_id) if module_id is not None else int(spec["module_id"])
            audit_label = spec["audit_label"]
            body_module = spec["body_module"]
        else:
            eff_module_id = int(module_id)
            audit_label = report_name
            body_module = True

        body: dict = {"CaseID": int(case_id)}
        if body_module:
            body["ModuleID"] = eff_module_id

        endpoint = (
            "Reports/SSRSReport/GetReportAsPdfAndAudit/"
            f"{quote(report_name, safe='')}/false/{int(case_id)}/"
            f"{eff_module_id}/{quote(audit_label, safe='')}"
        )

        pdf_bytes, content_type = await self._api_call_pdf("POST", endpoint, data=body)

        if not pdf_bytes.startswith(b"%PDF-"):
            # Honest failure: whatever came back is NOT a PDF — never save it.
            preview = pdf_bytes[:200].decode("utf-8", errors="replace")
            return {
                "error": "Response is not a PDF (no %PDF- magic) — nothing saved.",
                "content_type": content_type or "—",
                "body_preview": preview,
                "report_name": report_name,
                "case_id": int(case_id),
            }

        # target_dir was resolved and containment-checked above, before the
        # network call.
        target_dir.mkdir(parents=True, exist_ok=True)

        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in report_name)
        while "__" in slug:
            slug = slug.replace("__", "_")
        slug = slug.strip("_") or "report"

        saved_path = target_dir / f"case{int(case_id)}_{slug}.pdf"
        saved_path.write_bytes(pdf_bytes)
        logger.info(
            "get_report_pdf: saved case %s report (%d bytes)", case_id, len(pdf_bytes)
        )

        return {
            "saved_path": str(saved_path),
            "size_bytes": len(pdf_bytes),
            "report_name": report_name,
            "case_id": int(case_id),
        }

    async def get_patient_record(self, patient_id: int) -> dict:
        """
        Consolidated "pull a patient's whole record in one call" aggregator.

        Calls, each independently and defensively:
          - demographics       (get_patient_demographics)
          - insurance          (get_patient_facesheet → patientInsurances)
          - cases              (get_patient_cases)
          - dates_of_service   (get_dates_of_service)
          - billing_balance    (get_patient_balance — existing resolver)
          - notes              (get_patient_notes)
          - allergies          (get_patient_allergies)
          - medications        (get_patient_medications — case-resolved)

        GRACEFUL DEGRADATION: if any single section raises, it is recorded in
        `_partial` (list of {"section", "error"}) and the rest of the record is
        still returned. The whole call never fails because one piece failed.

        Returns:
            {
              "patient_id": int,
              "demographics": dict | None,
              "insurance": list | None,
              "cases": list | None,
              "dates_of_service": list | None,
              "billing_balance": dict | None,
              "notes": list | None,
              "allergies": list | None,
              "medications": list | None,
              "_partial": [ {"section": str, "error": str}, ... ],   # empty if all ok
            }
        Verified live 2026-06-30 (component endpoints all HTTP 200).
        """
        pid = int(patient_id)
        record: dict = {
            "patient_id": pid,
            "demographics": None,
            "insurance": None,
            "cases": None,
            "dates_of_service": None,
            "billing_balance": None,
            "notes": None,
            "allergies": None,
            "medications": None,
            "_partial": [],
        }

        try:
            record["demographics"] = await self.get_patient_demographics(pid)
        except Exception as e:
            record["_partial"].append({"section": "demographics", "error": str(e)})

        # Prefer the face sheet's patientInsurances block, then fall back to the
        # dedicated insurance endpoint. Some self-pay / edge records have a blank
        # face-sheet block even though Insurance/PatientInsurances returns rows.
        try:
            facesheet = await self.get_patient_facesheet(pid)
            if isinstance(facesheet, dict):
                record["insurance"] = facesheet.get("patientInsurances")
            else:
                record["insurance"] = None
        except Exception as e:
            record["_partial"].append({"section": "insurance", "error": str(e)})

        if not record["insurance"]:
            try:
                insurance_rows = await self.get_patient_insurance(pid)
                if insurance_rows:
                    record["insurance"] = insurance_rows
            except Exception as e:
                record["_partial"].append({"section": "insurance_fallback", "error": str(e)})

        try:
            record["cases"] = await self.get_patient_cases(pid)
        except Exception as e:
            record["_partial"].append({"section": "cases", "error": str(e)})

        try:
            record["dates_of_service"] = await self.get_dates_of_service(pid)
        except Exception as e:
            record["_partial"].append({"section": "dates_of_service", "error": str(e)})

        try:
            record["billing_balance"] = await self.get_patient_balance(pid)
        except Exception as e:
            record["_partial"].append({"section": "billing_balance", "error": str(e)})

        try:
            record["notes"] = await self.get_patient_notes(pid)
        except Exception as e:
            record["_partial"].append({"section": "notes", "error": str(e)})

        try:
            record["allergies"] = await self.get_patient_allergies(pid)
        except Exception as e:
            record["_partial"].append({"section": "allergies", "error": str(e)})

        try:
            record["medications"] = await self.get_patient_medications(pid)
        except Exception as e:
            record["_partial"].append({"section": "medications", "error": str(e)})

        return record

    async def health_check(self) -> dict:
        """
        Returns connection status without raising exceptions.
        {"status": "connected"|"expired"|"error", "user": ..., "checked_at": ...}
        """
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            user = await self.get_current_user()
            return {
                "status": "connected",
                "user": user.get("UserName") or user.get("Email") or "—",
                "checked_at": checked_at,
            }
        except EMRSessionExpired:
            return {"status": "expired", "user": None, "checked_at": checked_at}
        except Exception as e:
            return {"status": "error", "user": None, "error": str(e), "checked_at": checked_at}


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

async def _main():
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 sis_client.py --status")
        print("  python3 sis_client.py search <query>")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Headless by DEFAULT — this manual debug runner must not pop a visible
    # browser window unless a developer explicitly opts in with EMR_HEADED=1.
    client = SISClient(headless=(os.environ.get("EMR_HEADED") != "1"))

    try:
        await client.start()

        cmd = sys.argv[1]

        if cmd == "--status":
            result = await client.health_check()
            print(json.dumps(result, indent=2, default=str))

        elif cmd == "search":
            if len(sys.argv) < 3:
                print("Usage: python3 sis_client.py search <query>")
                sys.exit(1)
            query = " ".join(sys.argv[2:])
            results = await client.search_patient(query)
            # Print count only — do NOT echo PHI to terminal in production
            print(f"Found {len(results)} result(s) for query: {query!r}")
            if "--verbose" in sys.argv:
                print(json.dumps(results, indent=2, default=str))

        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)

    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(_main())

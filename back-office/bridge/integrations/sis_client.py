# VENDORED verbatim from n8n-office/python/integrations/sis_client.py
# Copied into back-office/bridge/integrations/ as a proven module (do not edit lightly).
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
    client = SISClient(headless=False)

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

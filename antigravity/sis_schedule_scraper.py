#!/usr/bin/env python3
"""
sis_schedule_scraper.py — Component B of the referral pipeline.

Produces an HONEST "upcoming appointments" feed for referral reconciliation by
scraping the REAL SIS Complete schedule. This REPLACES the dishonest demo data
that sis_agent.py's scrape_and_sync() writes — it never fabricates appointments.

What it does
------------
1. Reuses the saved SIS Auth0 session (scratch/sis_browser_state.json) inside a
   headless Playwright browser context — the same session sis_agent.py / the
   atlantic-emr MCP establish.
2. Captures the two credentials the SIS REST API requires (verified live):
       - Authorization: Bearer <Auth0 JWT>   (captured passively from the SPA)
       - token: <UUID>                        (minted via POST /api/Idp/Login)
3. Calls the REAL schedule endpoint for each day in the look-ahead window:
       POST /api/SchedulingData/GetSchedulingData
       body: {"startDate": "<ISO UTC>", "endDate": "<ISO UTC>"}
   Each day window is 04:00:00Z .. +24h-1ms (clinic-local midnight in UTC-4).
   The response is a flat JSON list of case/event objects (~63 keys each).
4. Normalizes events into the EXACT shape server.py's /api/upcoming-schedule
   reads, and atomically writes /Users/shubh/Documents/Antigravity/upcoming_schedule.json.

Honesty guarantees
------------------
- NEVER invents appointment rows. If login/2FA blocks the run, or no tokens can
  be captured, it logs the precise reason and EXITS WITHOUT touching the existing
  upcoming_schedule.json (a stale-but-real file beats an empty/garbage one).
- Writes to a temp file then os.replace() — the live file is only ever swapped
  for a validated, populated payload.
- Secrets (SIS_URL/SIS_USERNAME/SIS_PASSWORD) come from env via os.environ only.

CLI
---
    python3 sis_schedule_scraper.py --once            # single pass (default)
    python3 sis_schedule_scraper.py --once --days 14  # look ahead 14 days
    python3 sis_schedule_scraper.py --once --visible  # headful (debugging)

Exit codes: 0 = wrote a real schedule; 2 = blocked (auth/2FA/no data), file left
as-is. Later this goes on a launchd timer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    CLINIC_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo present on py3.9+
    CLINIC_TZ = timezone(timedelta(hours=-4))

# ---------------------------------------------------------------------------
# Paths & constants (mirror sis_agent.py / sis_client.py — single source of truth)
# ---------------------------------------------------------------------------

WORKSPACE_DIR = os.environ.get(
    "ANTIGRAVITY_WORKSPACE_DIR", os.path.dirname(os.path.abspath(__file__))
)
SCRATCH_DIR = os.environ.get(
    "ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch")
)

# The session file the contract specifies (same one sis_agent.py writes/reads).
BROWSER_STATE = os.path.join(SCRATCH_DIR, "sis_browser_state.json")
# The persistent-context dir the atlantic-emr MCP keeps warm — used as a
# fallback transport if the storage_state JSON alone can't bootstrap the SPA.
BROWSER_STATE_DIR = os.path.join(SCRATCH_DIR, "emr_sessions", "sis_browser_state")

OUTPUT_PATH = os.path.join(WORKSPACE_DIR, "upcoming_schedule.json")

# Verified-live SIS Complete endpoints (see sis_client.py discovery notes).
SIS_API_BASE = "https://e03ws.siscomplete.cloud/mainline/api"
SIS_FRONTEND_URL = "https://e03.siscomplete.cloud/mainline/"
SIS_LOGIN_URL = "https://e03.siscomplete.cloud/mainline/login/"

SOURCE_LABEL = "SIS Complete schedule (live API: SchedulingData/GetSchedulingData)"


def log(msg: str) -> None:
    ts = datetime.now(CLINIC_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_dotenv(path: str = None) -> None:
    """Minimal .env loader — never overrides an already-set env var."""
    path = path or os.path.join(WORKSPACE_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] could not read .env: {e}")


# ---------------------------------------------------------------------------
# Value placeholder detection (don't authenticate with a placeholder secret)
# ---------------------------------------------------------------------------

_PLACEHOLDER_HINTS = (
    "your_", "changeme", "change_me", "xxxx", "placeholder", "<", "example",
    "todo", "fill_in", "fillin",
)


def _is_placeholder(val: str) -> bool:
    if not val:
        return True
    low = val.strip().lower()
    return any(h in low for h in _PLACEHOLDER_HINTS)


# ---------------------------------------------------------------------------
# Normalization: real SIS event -> server appointment shape
# ---------------------------------------------------------------------------

def _to_clinic_dt(iso_str: str):
    """Parse an ISO timestamp (with offset/Z) to a clinic-local datetime."""
    if not iso_str:
        return None
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CLINIC_TZ)


def _fmt_time(local_dt) -> str:
    if not local_dt:
        return ""
    # e.g. "10:30 AM" — strip a leading zero for readability.
    return local_dt.strftime("%I:%M %p").lstrip("0")


def _patient_full_name(patient: dict) -> str:
    if not isinstance(patient, dict):
        return ""
    name = (patient.get("fullName") or "").strip()
    if name:
        return name
    first = (patient.get("firstName") or "").strip()
    last = (patient.get("lastName") or "").strip()
    return (f"{first} {last}").strip()


def _provider_name(event: dict) -> str:
    name = (event.get("caseProcedurePhysName") or "").strip()
    if name:
        # SIS gives "Last, First " — normalize to "First Last".
        if "," in name:
            last, first = [p.strip() for p in name.split(",", 1)]
            name = (f"{first} {last}").strip()
        return name.strip()
    surgeon = event.get("primarySurgeon") or {}
    if isinstance(surgeon, dict):
        fn = (surgeon.get("firstName") or "").strip()
        ln = (surgeon.get("lastName") or "").strip()
        if fn or ln:
            return (f"{fn} {ln}").strip()
    return ""


def _accepted(event: dict) -> bool:
    """Honest 'on the books' flag: active and not cancelled."""
    status = (event.get("caseStatus") or "").strip().lower()
    if status in {"cancelled", "canceled"}:
        return False
    if event.get("cancelDate"):
        return False
    return bool(event.get("isActiveCase", True))


def normalize_event(event: dict) -> dict | None:
    """
    Map one raw SIS schedule event to the appointment shape server.py reads:
        time, name, appointmentName, visitType, insurance, accepted, status, note
    Plus per-appointment date fields so the feed is honestly multi-day
    (server applies its top-level scheduleDate; per-appt date keeps provenance).

    Returns None for events we cannot place on a real date (no usable timestamp).
    """
    if not isinstance(event, dict):
        return None

    patient = event.get("patient") or {}
    name = _patient_full_name(patient)
    if not name:
        # No identifiable patient → skip rather than invent a row.
        return None

    start_local = _to_clinic_dt(event.get("startTime") or event.get("procedureDt"))
    if start_local is None:
        return None

    procedure = (event.get("primaryProcedure") or "").strip()
    appt_note = (event.get("appointmentNote") or event.get("note") or "")
    appt_note = appt_note.strip() if isinstance(appt_note, str) else ""
    room = ""
    sched_room = event.get("scheduledRoom") or {}
    if isinstance(sched_room, dict):
        room = (sched_room.get("name") or sched_room.get("quickCode") or "").strip()

    status_raw = (event.get("caseStatus") or "Scheduled").strip() or "Scheduled"

    return {
        # --- fields server.py reads directly ---
        "time": _fmt_time(start_local),
        "name": name,
        "appointmentName": procedure or "Surgical case",
        # Surgical-center cases aren't NP/EST office visits; label honestly.
        "visitType": "Procedure",
        # SIS schedule events carry no payer; leave blank (server shows
        # "Not listed in scheduler" and pulls insurance from the local chart).
        "insurance": "",
        "accepted": _accepted(event),
        "status": status_raw,
        "note": appt_note,
        # --- extra provenance (multi-day honesty; ignored by current server) ---
        "date": start_local.strftime("%Y-%m-%d"),
        "dateDisplay": start_local.strftime("%m/%d/%Y"),
        "startTimeUtc": event.get("startTime"),
        "provider": _provider_name(event),
        "room": room,
        "caseSummaryId": event.get("caseSummaryID") or event.get("caseSummaryId"),
        "patientId": patient.get("patientId"),
    }


# ---------------------------------------------------------------------------
# SIS live fetch (Playwright browser-context transport)
# ---------------------------------------------------------------------------

class SISScheduleFetcher:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.frontend_url = os.environ.get("SIS_URL", SIS_FRONTEND_URL)
        # If SIS_URL points at the login page, use the frontend for warm-up nav.
        if self.frontend_url.rstrip("/").endswith("login"):
            self.frontend_url = SIS_FRONTEND_URL
        self.username = os.environ.get("SIS_USERNAME", "")
        self.password = os.environ.get("SIS_PASSWORD", "")

        self._pw = None
        self._ctx = None
        self._page = None
        self._using_persistent = False

        self._bearer = None
        self._session_token = None

    # ---- lifecycle ----------------------------------------------------

    async def start(self, transport: str = "storage_state"):
        """
        Open a Playwright context using one of two reusable SIS sessions:
          - "storage_state": cold context seeded from scratch/sis_browser_state.json
            (the file the contract specifies; written by sis_agent.py).
          - "persistent": the warmed persistent-context dir the atlantic-emr MCP
            maintains — carries the live Auth0 SDK in-memory state that a cold
            storage_state load cannot, so it survives when the JSON has gone stale.
        Returns True if a context was opened, False if that transport's session
        artifact is unavailable.
        """
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()

        if transport == "storage_state":
            if not os.path.exists(BROWSER_STATE):
                log(f"[INFO] No storage_state JSON at {BROWSER_STATE}.")
                return False
            log(f"[INFO] Loading saved SIS session (storage_state): {BROWSER_STATE}")
            browser = await self._pw.chromium.launch(headless=self.headless)
            self._ctx = await browser.new_context(
                storage_state=BROWSER_STATE,
                viewport={"width": 1280, "height": 900},
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
        elif transport == "persistent":
            if not (os.path.isdir(BROWSER_STATE_DIR) and os.listdir(BROWSER_STATE_DIR)):
                log(f"[INFO] No persistent context dir at {BROWSER_STATE_DIR}.")
                return False
            log(f"[INFO] Loading warmed SIS session (persistent context): {BROWSER_STATE_DIR}")
            self._ctx = await self._pw.chromium.launch_persistent_context(
                BROWSER_STATE_DIR,
                headless=self.headless,
                viewport={"width": 1280, "height": 900},
                ignore_https_errors=True,
            )
            self._using_persistent = True
        else:
            raise ValueError(f"Unknown transport: {transport}")

        self._page = (
            self._ctx.pages[0] if getattr(self._ctx, "pages", None)
            else await self._ctx.new_page()
        )

        # Passively capture the Auth0 Bearer + SIS session token from SPA traffic.
        def _on_request(req):
            try:
                url = req.url
                if "e03ws" in url or "siscomplete.cloud/mainline/api" in url:
                    auth = req.headers.get("authorization") or req.headers.get("Authorization")
                    if auth and auth.lower().startswith("bearer "):
                        tok = auth.split(" ", 1)[1].strip()
                        if tok:
                            self._bearer = tok
                    sess = req.headers.get("token")
                    if sess:
                        self._session_token = sess
            except Exception:
                pass

        self._page.on("request", _on_request)
        return True

    async def close(self):
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ---- auth / token capture ----------------------------------------

    async def warm_up(self) -> bool:
        """
        Navigate to the SIS frontend so the SPA bootstraps and emits the Bearer
        JWT, then mint the session token via Idp/Login. Returns True iff we end
        up with BOTH tokens. Does NOT attempt a fresh password/2FA login — if the
        saved session is dead we report that and let the caller bail honestly.
        """
        log(f"[INFO] Warming up SPA at {self.frontend_url} ...")
        try:
            await self._page.goto(self.frontend_url, wait_until="networkidle", timeout=35000)
        except Exception as e:  # noqa: BLE001
            log(f"[WARN] frontend navigation issue: {e}")

        # Give Auth0's silent-SSO iframe time to complete a token refresh from the
        # saved session before judging whether we're logged in. Bail out early
        # only once the Bearer arrives (success) — don't fail on a transient
        # login-URL bounce during the SSO redirect dance.
        for _ in range(30):
            if self._bearer:
                break
            await self._page.wait_for_timeout(1000)

        if not self._bearer:
            cur = (self._page.url or "").lower()
            if "login" in cur or "auth0" in cur:
                log("[ERROR] Redirected to login/Auth0 and no Auth0 Bearer token "
                    "was issued — the saved SIS session is expired. A live SIS "
                    "login (+ SMS 2FA) is required to refresh the on-disk session "
                    f"({BROWSER_STATE} / persistent context) — run sis_agent.py.")
            else:
                log("[ERROR] No Auth0 Bearer token observed from SPA traffic — "
                    "cannot call the SIS API. Session may be stale; refresh via "
                    "sis_agent.py.")
            return False

        if not self._session_token:
            self._session_token = await self._mint_session_token()

        if not self._session_token:
            log("[ERROR] Could not obtain SIS session token (Idp/Login). Aborting.")
            return False

        log("[INFO] Auth ready: Bearer + session token captured.")
        return True

    async def _mint_session_token(self):
        try:
            resp = await self._ctx.request.post(
                SIS_API_BASE + "/Idp/Login",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://e03.siscomplete.cloud",
                    "Referer": "https://e03.siscomplete.cloud/mainline/",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._bearer}",
                },
                data="{}",
            )
            if resp.status == 200:
                body = await resp.text()
                try:
                    tok = json.loads(body)
                    if isinstance(tok, str) and len(tok) >= 32:
                        log("[INFO] Session token minted via Idp/Login.")
                        return tok
                except Exception:
                    pass
                log("[WARN] Idp/Login 200 but no usable token in body.")
            else:
                log(f"[WARN] Idp/Login returned HTTP {resp.status}.")
        except Exception as e:  # noqa: BLE001
            log(f"[WARN] Idp/Login mint failed: {e}")
        return None

    # ---- schedule fetch ----------------------------------------------

    def _api_headers(self) -> dict:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://e03.siscomplete.cloud",
            "Referer": "https://e03.siscomplete.cloud/mainline/",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._bearer}",
        }
        if self._session_token:
            h["token"] = self._session_token
        return h

    async def fetch_day(self, day_date) -> list:
        """
        POST SchedulingData/GetSchedulingData for one clinic-local day.
        Window: that day 04:00:00.000Z .. +24h-1ms (== clinic midnight in UTC-4).
        Returns the raw list of events (or [] on a clean empty day).
        Raises on auth failure so the caller can stop without writing garbage.
        """
        day = datetime(day_date.year, day_date.month, day_date.day, 4, 0, 0)
        start = day.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = (day + timedelta(hours=23, minutes=59, seconds=59, microseconds=999000)).strftime(
            "%Y-%m-%dT%H:%M:%S.999Z"
        )
        resp = await self._ctx.request.post(
            SIS_API_BASE + "/SchedulingData/GetSchedulingData",
            headers=self._api_headers(),
            data=json.dumps({"startDate": start, "endDate": end}),
        )
        if resp.status in (401, 403):
            raise PermissionError(
                f"SIS API {resp.status} for {day_date} — session/token rejected."
            )
        if resp.status >= 400:
            body = ""
            try:
                body = await resp.text()
            except Exception:
                pass
            raise RuntimeError(f"SIS API HTTP {resp.status} for {day_date}: {body[:200]}")
        body = await resp.text()
        if not body:
            return []
        data = json.loads(body)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data") or data.get("Data") or []
        return []


# ---------------------------------------------------------------------------
# Output assembly + atomic write
# ---------------------------------------------------------------------------

def build_payload(appointments: list, days_scanned: list) -> dict:
    """Assemble the upcoming_schedule.json payload in server-compatible shape."""
    now = datetime.now(CLINIC_TZ)
    captured_at = now.isoformat(timespec="seconds")

    # Sort by (date, raw UTC start) so the feed reads chronologically.
    def _sort_key(a):
        return (a.get("date") or "", a.get("startTimeUtc") or "", a.get("time") or "")

    appointments = sorted(appointments, key=_sort_key)

    # Top-level scheduleDate = earliest day with appointments (server applies
    # this to its derived payloads); display mirrors it. Per-appt 'date' keeps
    # the honest multi-day breakdown.
    dates_with_appts = sorted({a.get("date") for a in appointments if a.get("date")})
    primary_date = dates_with_appts[0] if dates_with_appts else (
        days_scanned[0].strftime("%Y-%m-%d") if days_scanned else now.strftime("%Y-%m-%d")
    )
    primary_display = datetime.strptime(primary_date, "%Y-%m-%d").strftime("%m/%d/%Y")

    providers = sorted({a.get("provider") for a in appointments if a.get("provider")})
    provider_label = (
        providers[0] if len(providers) == 1
        else (f"{len(providers)} providers" if providers else "")
    )

    scan_start = days_scanned[0].strftime("%Y-%m-%d") if days_scanned else primary_date
    scan_end = days_scanned[-1].strftime("%Y-%m-%d") if days_scanned else primary_date

    return {
        "source": SOURCE_LABEL,
        "capturedAt": captured_at,
        "scheduleDate": primary_date,
        "scheduleDateDisplay": primary_display,
        "office": "Main Line Surgical Center",
        "provider": provider_label,
        # Honest multi-day metadata (additive; existing server ignores these).
        "scanWindow": {
            "start": scan_start,
            "end": scan_end,
            "daysScanned": len(days_scanned),
            "datesWithAppointments": dates_with_appts,
        },
        "appointmentCount": len(appointments),
        "appointments": appointments,
    }


def atomic_write(payload: dict, path: str) -> None:
    """Write to a temp file in the same dir, then os.replace() — never partial."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".upcoming_schedule.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _authenticate_via(transport: str, headless: bool):
    """
    Try one transport. Returns an authenticated SISScheduleFetcher on success,
    or None (after cleaning up) if that transport's session is missing/stale.
    """
    fetcher = SISScheduleFetcher(headless=headless)
    try:
        opened = await fetcher.start(transport=transport)
        if not opened:
            await fetcher.close()
            return None
        if await fetcher.warm_up():
            return fetcher
        await fetcher.close()
        return None
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] transport '{transport}' failed: {e}")
        try:
            await fetcher.close()
        except Exception:
            pass
        return None


async def _fetch_and_write(fetcher: SISScheduleFetcher, days_ahead: int) -> int:
    """Scan the window with an authenticated fetcher and atomically write."""
    # Scan today .. today+N (clinic-local). Empty days are honestly empty.
    today = datetime.now(CLINIC_TZ).date()
    days = [today + timedelta(days=i) for i in range(max(1, days_ahead) + 1)]

    all_appts = []
    scanned = []
    fetch_errors = []
    for d in days:
        try:
            events = await fetcher.fetch_day(d)
        except PermissionError as e:
            log(f"[ERROR] {e}")
            log("[ABORT] SIS rejected the session mid-scan — NOT overwriting "
                "the existing file (it would be incomplete/garbage).")
            return 2
        except Exception as e:  # noqa: BLE001
            log(f"[WARN] day {d} fetch failed: {e}")
            fetch_errors.append(str(d))
            continue
        scanned.append(d)
        day_appts = [a for a in (normalize_event(ev) for ev in events) if a]
        if day_appts:
            log(f"[INFO] {d}: {len(day_appts)} appointment(s).")
        all_appts.extend(day_appts)

    if not scanned:
        log("[ABORT] Every day fetch failed — NOT overwriting the existing file.")
        return 2

    if not all_appts:
        # Scan succeeded but the window is genuinely empty. Do NOT clobber a
        # possibly-useful prior snapshot with an empty feed — report instead.
        log(f"[INFO] Scan succeeded across {len(scanned)} day(s) but found 0 "
            "appointments in the look-ahead window.")
        log("[ABORT] Refusing to overwrite upcoming_schedule.json with an empty "
            "feed. Re-run with a wider --days window if appointments are "
            "expected. Existing file left untouched.")
        return 2

    payload = build_payload(all_appts, scanned)
    atomic_write(payload, OUTPUT_PATH)
    log(f"[SUCCESS] Wrote {len(all_appts)} real appointment(s) across "
        f"{len(payload['scanWindow']['datesWithAppointments'])} day(s) to "
        f"{OUTPUT_PATH}")
    if fetch_errors:
        log(f"[NOTE] {len(fetch_errors)} day(s) failed to fetch and were "
            f"skipped: {', '.join(fetch_errors)}")
    return 0


async def run_once(days_ahead: int, headless: bool) -> int:
    """
    Returns process exit code: 0 on a real write, 2 on an honest block/no-write.

    Transport order:
      1. storage_state JSON (scratch/sis_browser_state.json) — the contract file.
      2. persistent context dir — the warmed session the atlantic-emr MCP keeps,
         which survives when the cold JSON has gone stale.
    """
    load_dotenv()

    sis_user = os.environ.get("SIS_USERNAME", "")
    if _is_placeholder(sis_user):
        log("[WARN] SIS_USERNAME missing/placeholder in env. Continuing on the "
            "saved session, but a session refresh would need real creds.")

    fetcher = None
    for transport in ("storage_state", "persistent"):
        fetcher = await _authenticate_via(transport, headless)
        if fetcher is not None:
            log(f"[INFO] Authenticated via '{transport}' transport.")
            break

    if fetcher is None:
        log("[ABORT] Could not authenticate to SIS via any saved session — NOT "
            "overwriting upcoming_schedule.json. A live SIS login (+ SMS 2FA) is "
            "required to refresh the session (run sis_agent.py) before this can "
            "fetch real appointments.")
        return 2

    try:
        return await _fetch_and_write(fetcher, days_ahead)
    finally:
        await fetcher.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape the REAL SIS Complete schedule into upcoming_schedule.json"
    )
    parser.add_argument("--once", action="store_true",
                        help="Single pass (default behavior).")
    parser.add_argument("--days", type=int, default=7,
                        help="Days to look ahead from today (default 7).")
    parser.add_argument("--visible", action="store_true",
                        help="Run the browser headful (debugging).")
    args = parser.parse_args()

    try:
        return asyncio.run(run_once(days_ahead=args.days, headless=not args.visible))
    except KeyboardInterrupt:
        log("[ABORT] Interrupted.")
        return 2


if __name__ == "__main__":
    sys.exit(main())

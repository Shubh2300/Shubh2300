# VENDORED verbatim from n8n-office/python/integrations/svigg_scraper.py
# Copied into back-office/bridge/integrations/ as a proven module (do not edit lightly).
# Svigg/WEBeDoctor browser RPA client (reads + triple-gated writes).
#!/usr/bin/env python3
"""
svigg_scraper.py - Playwright scraper for Svigg/Dr.Com/WEBeDoctor EMR

Real selectors discovered 2026-06-28 by reading the live portal.
Svigg = billing + appointments EMR for Atlantic Pain & Wellness.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BOOKING COMMIT KILL-SWITCH (safety locks — fail-closed, triple-gated)
# ---------------------------------------------------------------------------
# The Doctor.com/Svigg appointment-COMMIT path (POST to bk_p) writes to the
# live production calendar. The bk_p route + form-field contract were mapped
# from the 2026-07-01 HAR (dates MM/DD/YYYY; times HH:MMAM per the grid labels;
# cpt00/prov are <select>s; slot x/y encodes day+time). It is gated behind
# THREE independent, fail-closed locks:
#   1. BOOKING_EXECUTE_ENABLED — env SVIGG_BOOKING_EXECUTE must be truthy. NOT
#      hard-coded True, so a commit is never left open in source.
#   2. the caller must pass execute=True AND confirm_unverified=True.
#   3. the patient acct must be in BOOKING_ALLOWED_ACCTS, checked AT the POST
#      site — so a real patient can never be committed even if 1+2 are open.
BOOKING_EXECUTE_ENABLED = os.environ.get("SVIGG_BOOKING_EXECUTE", "").lower() in ("1", "true", "yes")

# Hard allowlist: only these accts may ever be committed.
# "22041163" = "Patients 1, Test" — the designated test record.
#
# GO-LIVE: to book real patients, EXTEND this allowlist WITHOUT editing source by
# setting env SVIGG_BOOKING_ALLOWLIST to a comma-separated list of accts, e.g.
#   SVIGG_BOOKING_ALLOWLIST="22041163,10456789"
# or the literal "*" to allow ANY acct (only after the booking path is trusted in
# production — a wildcard removes the per-acct guard entirely). DEFAULTS ARE
# FAIL-CLOSED: with no env set, only the test acct above can ever be committed.
BOOKING_ALLOWED_ACCTS = {"22041163"}
_booking_allowlist_env = os.environ.get("SVIGG_BOOKING_ALLOWLIST", "")
for _entry in _booking_allowlist_env.split(","):
    _entry = _entry.strip()
    if _entry:
        BOOKING_ALLOWED_ACCTS.add(_entry)

# CANCEL is destructive AND locates the appointment by patient NAME on the grid,
# so an allowlisted acct alone does not bind the deletion to the intended record.
# Bind each cancel-allowed acct to a required last-name token: the caller's
# last_name must contain it, so passing an allowlisted acct + a real patient's
# name cannot reach/delete that real patient's appointment.
#
# GO-LIVE: to cancel real patients, EXTEND this map WITHOUT editing source by
# setting env SVIGG_CANCEL_ALLOWLIST to comma-separated "acct:nametoken" pairs,
# e.g. SVIGG_CANCEL_ALLOWLIST="22041163:patient,10456789:smith"
# A literal "*" entry sets CANCEL_ALLOW_ANY=True, which drops the acct→name
# binding entirely and permits cancelling any appointment whose last_name is
# non-empty (only after the cancel path is trusted in production — the name
# binding is the last guard preventing an allowlisted acct from being aimed at a
# real patient's row). DEFAULTS ARE FAIL-CLOSED: with no env set, only the test
# acct/name above can be cancelled and CANCEL_ALLOW_ANY stays False.
CANCEL_ALLOWED_ACCT_NAMES = {"22041163": "patient"}
CANCEL_ALLOW_ANY = False
_cancel_allowlist_env = os.environ.get("SVIGG_CANCEL_ALLOWLIST", "")
for _entry in _cancel_allowlist_env.split(","):
    _entry = _entry.strip()
    if not _entry:
        continue
    if _entry == "*":
        CANCEL_ALLOW_ANY = True
        continue
    if ":" in _entry:
        _acct, _tok = _entry.split(":", 1)
        _acct = _acct.strip()
        _tok = _tok.strip().lower()
        if _acct and _tok:
            CANCEL_ALLOWED_ACCT_NAMES[_acct] = _tok

# ---------------------------------------------------------------------------
# NEW-PATIENT CREATE KILL-SWITCH (safety locks — fail-closed, double-gated)
# ---------------------------------------------------------------------------
# create_patient() writes a brand-new chart to the live Svigg/Dr.Com database.
# The ENTRY path (login -> patientEntry_new.htm -> name-search de-dupe POST ->
# patientEntry_add.htm add form) was mapped from a real 2026-07-03 HAR, BUT the
# add-form's own field set and the final SAVE POST are NOT in that HAR. So:
#   * DRY-RUN (dry_run=True, the DEFAULT) discovers the add form's fields LIVE
#     from the rendered DOM and returns what it WOULD submit — it NEVER clicks
#     Save, creating nothing.
#   * The SAVE contract is UNVERIFIED. Committing is gated behind TWO
#     independent, fail-closed locks so a real Save is never left open in
#     source:
#       1. CREATE_EXECUTE_ENABLED — env SVIGG_CREATE_EXECUTE must be truthy.
#       2. the caller must pass dry_run=False AND confirm_unverified=True.
#     Even with both open the method still refuses unless it can positively
#     identify a single Save/Add submit control on the add form; it never
#     guesses a submit target.
CREATE_EXECUTE_ENABLED = os.environ.get("SVIGG_CREATE_EXECUTE", "").lower() in ("1", "true", "yes")

# Lazy import — Playwright may not be installed in all envs
_pw_module = None

def _get_pw():
    global _pw_module
    if _pw_module is None:
        from playwright.async_api import async_playwright
        _pw_module = async_playwright
    return _pw_module


def _load_env():
    """Load .env from Antigravity dir."""
    env_path = Path(os.environ.get("ANTIGRAVITY_DIR", "/Users/shubh/Documents/Antigravity")) / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


class SviggScraper:
    """Playwright-based scraper for Svigg/Dr.Com/WEBeDoctor."""

    BASE_URL = "https://websrv01.physician-to-go.net"

    def __init__(self, headless: bool = True):
        _load_env()
        self.headless = headless
        self.url = os.environ.get("WEBEDOCTOR_URL", f"{self.BASE_URL}/proxy.cgi/off/home/login.htm")
        self.username = os.environ.get("WEBEDOCTOR_USER", "")
        self.password = os.environ.get("WEBEDOCTOR_PASS", "")
        self._pw = None
        self._browser = None
        self._page = None

    async def start(self):
        """Launch browser.

        Idempotent / safe to re-call: if a browser is already running (e.g. a
        prior session that hit a transient portal error and was marked
        disconnected by emr_session_manager WITHOUT being stopped), tear it down
        first. Without this, each session-expiry reconnect overwrote
        self._pw/_browser/_page and orphaned the previous chromium process +
        context, leaking file descriptors and memory until the server could no
        longer launch browsers — and silently abandoned any half-filled write
        form on the old page (audit finding #8). SISClient.start() already guards
        this way; Svigg now matches.
        """
        if self._browser is not None or self._pw is not None:
            await self.stop()
        pw_factory = _get_pw()
        self._pw = await pw_factory().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        ctx = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
        )
        self._page = await ctx.new_page()

    async def stop(self):
        """Close browser.

        Best-effort: a close/stop on an already-dead browser can itself raise,
        but this is also the teardown path start() uses before a reconnect, so a
        failure here must NOT prevent the fresh launch or leave stale handles.
        We log and press on, always clearing the references.
        """
        if self._browser:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                logger.warning("Svigg browser close failed (ignoring): %s", exc)
        if self._pw:
            try:
                await self._pw.stop()
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                logger.warning("Svigg playwright stop failed (ignoring): %s", exc)
        self._browser = None
        self._pw = None
        self._page = None

    async def _goto(self, url: str, *, wait_until: str = "networkidle",
                    timeout: int = 15000, retries: int = 2):
        """
        Navigate with a bounded retry on transient net::ERR_ABORTED.

        The physician-to-go.net proxy intermittently aborts a navigation when
        requests arrive back-to-back on the shared page (observed ~20% of the
        time under rapid sequential load). ERR_ABORTED is transient — the page
        is still alive and an immediate re-goto succeeds. We retry only that
        specific error so genuine failures (timeout, DNS, auth) still surface.
        """
        last_exc = None
        for attempt in range(retries + 1):
            try:
                return await self._page.goto(url, wait_until=wait_until, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 — narrow on message below
                last_exc = exc
                if "ERR_ABORTED" not in str(exc) or attempt == retries:
                    raise
                logger.warning(
                    "Svigg goto ERR_ABORTED (attempt %d/%d), retrying: %s",
                    attempt + 1, retries, url,
                )
                await asyncio.sleep(0.4 * (attempt + 1))
        raise last_exc  # unreachable, kept for clarity

    async def login(self) -> bool:
        """Log into Svigg. Returns True on success."""
        if not self.username or not self.password:
            raise ValueError("WEBEDOCTOR_USER and WEBEDOCTOR_PASS must be set in .env")

        await self._goto(self.url, wait_until="networkidle", timeout=20000)

        # The login page is a frameset. Check if we're already logged in
        # by looking for the main menu frame.
        title = await self._page.title()
        if "Sunny Vigg" in title or "Atlantic" in title:
            # Check if there's a "Main Menu" or similar logged-in indicator
            # by navigating to a known authenticated page
            await self._goto(
                f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry.htm",
                wait_until="networkidle", timeout=15000,
            )
            test_title = await self._page.title()
            if "Patient Entry" in test_title:
                logger.info("Already logged in to Svigg")
                return True

        # Not logged in — try to find login form in frames
        # The login form is likely in the mainFrame of the frameset
        frames = self._page.frames
        login_frame = None
        for frame in frames:
            try:
                user_input = await frame.query_selector('input[type="text"]')
                pass_input = await frame.query_selector('input[type="password"]')
                if user_input and pass_input:
                    login_frame = frame
                    break
            except Exception:
                continue

        if not login_frame:
            # Try the main page itself
            user_input = await self._page.query_selector('input[type="text"]')
            pass_input = await self._page.query_selector('input[type="password"]')
            if user_input and pass_input:
                login_frame = self._page
            else:
                logger.error("Could not find login form in any frame")
                return False

        # Fill credentials and submit
        await login_frame.fill('input[type="text"]', self.username)
        await login_frame.fill('input[type="password"]', self.password)

        # Find and click submit button
        submit = await login_frame.query_selector(
            'input[type="submit"], input[type="image"], button[type="submit"]'
        )
        if submit:
            await submit.click()
        else:
            await login_frame.press('input[type="password"]', 'Enter')

        # Wait for navigation
        await self._page.wait_for_load_state("networkidle", timeout=15000)

        # Verify login succeeded
        await self._goto(
            f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry.htm",
            wait_until="networkidle", timeout=15000,
        )
        title = await self._page.title()
        if "Patient Entry" in title:
            logger.info("Svigg login successful")
            return True

        logger.error(f"Svigg login failed — page title: {title}")
        return False

    async def search_patient(self, last_name: str, first_name: str = "") -> list[dict]:
        """
        Search for patients by name. Returns list of patient dicts.
        Each dict has: name, acct, rowid, dob, gender, phone, address,
        insurance_class, insurance_carrier, summary_url, display_url.
        """
        page = self._page

        await self._goto(
            f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry.htm",
            wait_until="networkidle", timeout=15000,
        )

        # Fill search form
        await page.fill('input[name="LastName"]', last_name)
        if first_name:
            await page.fill('input[name="FirstName"]', first_name)

        # Click the NameSearch button
        await page.click('input[name="NameSearch"]')
        await page.wait_for_load_state("networkidle", timeout=15000)

        # Parse results
        title = await page.title()
        if "Patient Entry" in title and "View" not in title:
            # No results — still on search form, or form validation error
            return []

        results = []

        # Find all patient name links (they link to pentry.htm with rowid and acct)
        patient_links = await page.query_selector_all('a[href*="pentry.htm?rowid="]')

        for link in patient_links:
            href = await link.get_attribute("href") or ""
            name = (await link.inner_text()).strip()

            # Extract rowid and acct from href
            rowid = ""
            acct = ""
            if "rowid=" in href:
                rowid_match = re.search(r'rowid=([^&|]+)', href)
                if rowid_match:
                    rowid = rowid_match.group(1)
            if "acct=" in href:
                acct_match = re.search(r'acct=([^&|]+)', href)
                if acct_match:
                    acct = acct_match.group(1)

            # Get the parent row to extract more details
            # The result row is in a table structure
            row_el = await link.evaluate_handle(
                """el => {
                    // Walk up to find the containing table row or cell
                    let parent = el.closest('tr') || el.parentElement?.parentElement;
                    return parent;
                }"""
            )

            row_text = ""
            try:
                row_text = await row_el.inner_text()
            except Exception:
                pass

            # Parse DOB from row text
            dob_match = re.search(r'DOB:\s*(\d{2}/\d{2}/\d{4})', row_text)
            dob = dob_match.group(1) if dob_match else ""

            # Parse gender
            gender = ""
            if "Male" in row_text:
                gender = "Male"
            elif "Female" in row_text:
                gender = "Female"

            # Parse phone
            phone_match = re.search(r'(?:Cell|Home|Work):\s*([\d\-\(\)\s]+)', row_text)
            phone = phone_match.group(1).strip() if phone_match else ""

            result = {
                "name": name,
                "acct": acct,
                "rowid": rowid,
                "dob": dob,
                "gender": gender,
                "phone": phone,
                "raw_text": row_text[:500],  # truncated for cache
            }
            results.append(result)

        return results

    async def get_patient_summary(self, rowid: str, acct: str) -> dict:
        """
        Navigate to the Patient Summary page and parse all fields.
        Returns a dict with demographics, insurance, cases, contacts, etc.
        """
        page = self._page

        url = (
            f"{self.BASE_URL}/proxy.cgi/off/maint/pentrySummary.htm?"
            f"srchpg=/proxy.cgi/off/maint/patientEntry.htm"
            f"&title=Patient+Entry"
            f"&rowid={rowid}"
            f"&acct={acct}"
            f"&cb=/proxy.cgi/off/maint/pentry.htm|rowid={rowid}|acct={acct}"
        )

        await self._goto(url, wait_until="networkidle", timeout=15000)

        # The summary page renders as plain text — extract everything
        text = await page.inner_text("body")

        return self._parse_summary_text(text, acct, rowid)

    def _parse_summary_text(self, text: str, acct: str, rowid: str) -> dict:
        """Parse the raw text from the Patient Summary page into structured data."""

        result: dict = {
            "acct": acct,
            "rowid": rowid,
            "source": "svigg_live",
        }

        full_text = text

        # Name — first line after "Patient Summary - "
        name_match = re.search(r'Patient Summary - (.+)', full_text)
        if name_match:
            result["name"] = name_match.group(1).strip()

        # Account
        acct_match = re.search(r'Acct #:\s*(\S+)', full_text)
        if acct_match:
            result["acct"] = acct_match.group(1)

        # SSN
        ssn_match = re.search(r'Social:\s*(\S+)', full_text)
        if ssn_match:
            result["ssn"] = ssn_match.group(1)

        # DOB
        dob_match = re.search(r'DOB:\s*(\d{2}/\d{2}/\d{4})', full_text)
        if dob_match:
            result["dob"] = dob_match.group(1)

        # Age
        age_match = re.search(r'Age:\s*(\d+y\s*\d*m?)', full_text)
        if age_match:
            result["age"] = age_match.group(1).strip()

        # Gender
        gender_match = re.search(r'Gender:\s*(\w+)', full_text)
        if gender_match:
            result["gender"] = gender_match.group(1)

        # Chart number
        chart_match = re.search(r'Chart:\s*(\S+)', full_text)
        if chart_match and chart_match.group(1) != "Sig.":
            result["chart"] = chart_match.group(1)

        # Address — multi-line, after "Address" label
        addr_match = re.search(
            r'Address\s*\n\s*(.+?)\n\s*(.+?,\s*[A-Z]{2}\s*\d{5})',
            full_text
        )
        if addr_match:
            result["address"] = f"{addr_match.group(1).strip()}, {addr_match.group(2).strip()}"

        # Phone numbers
        phones: dict = {}
        for phone_match in re.finditer(r'(Cell|Home|Work|Fax)\s+(\d[\d\-\(\)\s]+)', full_text):
            phones[phone_match.group(1).lower()] = phone_match.group(2).strip()
        if phones:
            result["phones"] = phones

        # Email
        email_match = re.search(r'Email Address\s*\n?\s*(\S+@\S+)', full_text)
        if email_match:
            result["email"] = email_match.group(1)

        # Primary Office
        office_match = re.search(r'Primary Office:\s*(.+)', full_text)
        if office_match:
            result["primary_office"] = office_match.group(1).strip()

        # Referring Provider
        ref_match = re.search(r'Referring Provider:\s*(.+)', full_text)
        if ref_match and ref_match.group(1).strip():
            result["referring_provider"] = ref_match.group(1).strip()

        # Provider
        prov_match = re.search(r'Provider:\s*(.+)', full_text)
        if prov_match:
            result["provider"] = prov_match.group(1).strip().rstrip(",")

        # Class (insurance type)
        class_match = re.search(r'Class:\s*(.+)', full_text)
        if class_match:
            result["insurance_class"] = class_match.group(1).strip()

        # Insurance carrier
        # Look for the insurance section — carrier name is typically after "Insurance" header
        # and before "Accept Assignment"
        carrier_match = re.search(
            r'Insurance Carrier.*?\n(.+?)(?:\n.*?Accept Assignment)',
            full_text, re.DOTALL
        )
        if carrier_match:
            carrier_lines = [l.strip() for l in carrier_match.group(1).strip().split("\n") if l.strip()]
            if carrier_lines:
                result["insurance_carrier"] = carrier_lines[0]

        # Policy number
        policy_match = re.search(r'Policy #:\s*(\S+)', full_text)
        if policy_match:
            result["policy_number"] = policy_match.group(1)

        # Group number
        group_match = re.search(r'Group #:\s*(\S+)', full_text)
        if group_match:
            result["group_number"] = group_match.group(1)

        # Copay
        copay_match = re.search(r'Copay:\s*([\d\.]+)', full_text)
        if copay_match:
            result["copay"] = copay_match.group(1)

        # Case info
        cases = []
        for case_match in re.finditer(r'(WC|Standard|Auto|PIP|Lien)\s+(\d{2}/\d{2}/\d{4})', full_text):
            cases.append({
                "type": case_match.group(1),
                "date": case_match.group(2),
            })
        if cases:
            result["cases"] = cases

        # Responsible party
        resp_match = re.search(r'Responsible:\s*(.+)', full_text)
        if resp_match:
            result["responsible_party"] = resp_match.group(1).strip()

        return result

    async def get_patient_ledger(self, account_number: str, rowid: str = "") -> dict:
        """
        Navigate to the visit-entry ledger for a patient and parse it with BeautifulSoup.

        Discovery (re-verified live 2026-06-29):
          URL: /proxy.cgi/apps/ven/elist.htm?acct={acct}&rowid={rowid}

          *** rowid is REQUIRED to reach the ledger. *** The 2026-06-28 note
          that "Svigg handles a blank rowid" is WRONG: with acct-only the portal
          serves the Visit-Entry SEARCH FORM (header "Visit Entry", no charges
          table), so the parser silently found nothing and returned zero charges.
          That was the 2026-06-29 regression. Pass the rowid harvested from
          search_patient() (pentry.htm?rowid=...&acct=...) to land on the actual
          ledger. If no rowid is supplied we still issue the request (and log a
          warning) — useful for the legacy/diagnostic path — but expect zero
          charges because the portal will show the search form.

        Real ledger structure (acct+rowid, observed 2026-06-29 for a live acct):
          - Section banners are <th> cells: "Visit Entry -<name>", "Batch Review",
            "Bill Review". The CHARGES live under "Batch Review".
          - The charges COLUMN HEADER is a <td> row (NOT <th>) reading:
              Batch # | Visit Date | Tran Date | Bill Office | Treat Office |
              Provider | Skip Vst Tab | Conv? | Expected | Entered | (action)
          - Data rows are <td> rows under that header. A degenerate
            "No Batch Control" row (8 cells) may appear and is skipped.
          - A separate "Bill Review" section follows with its own <td> header
            (Bill | Visit Date | Incident | ...). We STOP at that banner so bill
            rows are not misparsed as charges.

        Parsing is HEADER-DRIVEN: we locate the charges header row by its column
        names, build a {column_name: index} map, and read every field by NAME
        (with synonyms + a legacy positional fallback). The header text is logged
        so a future column-layout change is diagnosable from logs alone.

        Payments + a computed balance come from a SEPARATE two-step report,
        /proxy.cgi/off/reports/ledgerRptcases.htm (NOT the dead /apps/pay/ root,
        which returns Svigg's 'Sorry' page). See _fetch_payments() for the form
        fields and the rendered RetrieveReport table layout. Payments are fetched
        after charges are parsed, so a payments failure never regresses charges.

        Returns:
          {
            "charges": [{"batch": ..., "visit_date": ..., "tran_date": ...,
                         "provider": ..., "expected": ..., "entered": ...}, ...],
            "payments": [...], # patient/copay payments + adjustments (ledgerRptcases.htm)
            "balance": <float>, # total charges - total payments/adjustments (or None)
            "count": N,
            "account_number": "...",
            "source": "svigg_live",
            "note": "...",
          }
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {
                "account_number": account_number,
                "error": "BeautifulSoup (beautifulsoup4) not installed",
                "charges": [],
                "payments": [],
                "balance": None,
                "source": "svigg_live",
            }

        page = self._page
        url = f"{self.BASE_URL}/proxy.cgi/apps/ven/elist.htm?acct={account_number}"
        if rowid:
            url += f"&rowid={rowid}"
        else:
            # acct-only lands on the Visit-Entry search form, not the ledger.
            # We still issue the request, but warn so a caller that forgot to
            # plumb the rowid through can see why charges came back empty.
            logger.warning(
                "Svigg get_patient_ledger called WITHOUT rowid (acct=%s); the "
                "portal will serve the search form, not the charges ledger. "
                "Pass the rowid from search_patient() to get charges.",
                account_number,
            )
        await self._goto(url, wait_until="networkidle", timeout=15000)

        # Check for the 'Sorry' page (Svigg's generic 'not found / no session' page)
        body_text = await page.inner_text("body")
        if len(body_text.strip()) < 100 and "sorry" in body_text.lower():
            return {
                "account_number": account_number,
                "error": "Svigg returned 'Sorry' — account may not exist or session expired",
                "charges": [],
                "payments": [],
                "balance": None,
                "source": "svigg_live",
            }

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        charges = []

        def _norm(text: str) -> str:
            """Lowercase, strip nbsp/punctuation, collapse whitespace."""
            text = text.replace("\xa0", " ")
            return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

        # --- 1. Locate the charges COLUMN-HEADER row --------------------------
        # The header is a <td> row (NOT <th>) whose cells read
        #   Batch # | Visit Date | Tran Date | Bill Office | Treat Office |
        #   Provider | Skip Vst Tab | Conv? | Expected | Entered | (action)
        # We scan every <tr> in the document and pick the row that looks like
        # this header (contains a 'batch' cell AND a 'visit date' cell). That
        # row anchors both the column map and where the data rows begin.
        all_rows = soup.find_all("tr")
        header_row = None
        header_idx_in_doc = -1
        header_cells = []
        for ri, row in enumerate(all_rows):
            cell_texts = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            norm_texts = [_norm(t) for t in cell_texts]
            has_batch = any(t.startswith("batch") for t in norm_texts)
            has_visit = any("visit date" in t for t in norm_texts)
            if has_batch and has_visit:
                header_row = row
                header_idx_in_doc = ri
                header_cells = cell_texts
                break

        # Log the raw header text so a future column-layout change is diagnosable
        # from logs alone (the 2026-06-29 regression returned zero charges silently).
        logger.info(
            "Svigg ledger acct=%s rowid=%s charges-header columns: %r",
            account_number, rowid or "(none)", header_cells,
        )

        # --- 2. Build {normalized_header: index} and resolve fields by NAME ---
        col_index = {}
        for i, h in enumerate(header_cells):
            key = _norm(h)
            if key and key not in col_index:
                col_index[key] = i

        def _find_col(*candidates) -> Optional[int]:
            """Index of the first header matching any candidate.

            Each candidate is matched exactly first, then as a substring, so
            'expected' matches '$ Expected' / 'Amt Expected' / 'Expected'.
            """
            for cand in candidates:
                cn = _norm(cand)
                if cn in col_index:
                    return col_index[cn]
            for cand in candidates:
                cn = _norm(cand)
                for key, idx in col_index.items():
                    if cn and cn in key:
                        return idx
            return None

        idx_batch = _find_col("batch #", "batch", "batch number")
        idx_visit = _find_col("visit date", "visit", "service date", "dos")
        idx_tran = _find_col("tran date", "transaction date", "tran")
        idx_bill_office = _find_col("bill office", "billing office")
        idx_treat_office = _find_col("treat office", "treating office", "treatment office")
        idx_provider = _find_col("provider", "rendering provider")
        idx_expected = _find_col("expected", "$ expected", "amt expected", "amount expected")
        idx_entered = _find_col("entered", "$ entered", "amt entered", "amount entered")

        # Legacy positional fallback (only if a named header is missing), from the
        # layout observed 2026-06-29: Batch#=0, Visit=1, Tran=2, BillOff=3,
        # TreatOff=4, Provider=5, Skip=6, Conv=7, Expected=8, Entered=9.
        FALLBACK = {
            "batch": 0, "visit": 1, "tran": 2, "bill_office": 3,
            "treat_office": 4, "provider": 5, "expected": 8, "entered": 9,
        }
        idx_batch = idx_batch if idx_batch is not None else FALLBACK["batch"]
        idx_visit = idx_visit if idx_visit is not None else FALLBACK["visit"]
        idx_tran = idx_tran if idx_tran is not None else FALLBACK["tran"]
        idx_bill_office = idx_bill_office if idx_bill_office is not None else FALLBACK["bill_office"]
        idx_treat_office = idx_treat_office if idx_treat_office is not None else FALLBACK["treat_office"]
        idx_provider = idx_provider if idx_provider is not None else FALLBACK["provider"]
        idx_expected = idx_expected if idx_expected is not None else FALLBACK["expected"]
        idx_entered = idx_entered if idx_entered is not None else FALLBACK["entered"]

        def _cell(cells, idx) -> str:
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].get_text(strip=True).replace("\xa0", " ").strip()

        # --- 3. Walk data rows AFTER the header, STOP at the next section ------
        # The "Bill Review" section that follows has its own <td> header
        # (Bill | Visit Date | Incident | ...) — we must not parse those rows as
        # charges. We stop as soon as we hit a row that is a new section banner
        # ('bill review' / 'payment') or a different column header ('incident').
        if header_row is not None:
            # The maximum named index we read; a real charge row must be ≥ this.
            min_width = max(idx_batch, idx_visit, idx_tran, idx_provider) + 1
            for row in all_rows[header_idx_in_doc + 1:]:
                row_text = _norm(row.get_text(" ", strip=True))
                # Stop at the start of the next section (bill review / payments).
                if "bill review" in row_text or row_text.startswith("payment"):
                    break
                # A <th> here is a section banner — skip (don't treat as data).
                if row.find("th") and not row.find("td"):
                    continue
                # Another column-header row (e.g. the Bill table header) → stop.
                if "incident" in row_text and "visit date" in row_text:
                    break
                cells = row.find_all("td")
                if len(cells) < min_width:
                    continue
                batch_cell = cells[idx_batch] if idx_batch < len(cells) else None
                if batch_cell is None:
                    continue
                batch_link = batch_cell.find("a")
                batch_num = (
                    batch_link.get_text(strip=True)
                    if batch_link else batch_cell.get_text(strip=True)
                )
                batch_num = batch_num.replace("\xa0", " ").strip()
                # A real charge row carries a numeric batch number. The
                # degenerate "No Batch Control" placeholder row has batch '0'
                # with a 'No Batch Control' provider — drop non-numeric and the
                # zero placeholder, keep real batches.
                if not batch_num or not batch_num.isdigit() or batch_num == "0":
                    continue
                charges.append({
                    "batch": batch_num,
                    "visit_date": _cell(cells, idx_visit),
                    "tran_date": _cell(cells, idx_tran),
                    "bill_office": _cell(cells, idx_bill_office),
                    "treat_office": _cell(cells, idx_treat_office),
                    "provider": _cell(cells, idx_provider),
                    "expected": _cell(cells, idx_expected),
                    "entered": _cell(cells, idx_entered),
                })

        # --- 4. Payments + computed balance via the ledgerRptcases.htm report --
        # The charges above came from apps/ven/elist.htm. Payments live in a
        # separate two-step report (ledgerRptcases.htm → Execute Report). We
        # fetch them AFTER charges are already in hand, so a payments failure
        # can never regress the charges output.
        pay = await self._fetch_payments(account_number)

        return {
            "account_number": account_number,
            "charges": charges,
            "payments": pay.get("payments", []),
            "balance": pay.get("balance"),
            "count": len(charges),
            "source": "svigg_live",
            "columns": header_cells,
            "payments_columns": pay.get("payments_header", []),
            "note": (
                "Charges from apps/ven/elist.htm (acct+rowid). Payments + computed "
                "balance from the ledgerRptcases.htm report "
                "(/proxy.cgi/off/reports/ledgerRptcases.htm?acct=...): check "
                "InclFP+ShowCopayAdj+AllVisits, Execute Report, then parse the "
                "RetrieveReport.htm ReportBody table (Bill|Service|Procedure|"
                "Description|Diag|Charge). balance = total charges - total "
                "payments/adjustments. The old /apps/pay/ path is a dead app root "
                "(returns Svigg's 'Sorry' page) and is NOT used."
            ),
        }

    async def _fetch_payments(self, account_number: str) -> dict:
        """Fetch payments + computed balance via the ledgerRptcases.htm report.

        Two-step report (discovered live 2026-06-29):
          1. GET  /proxy.cgi/off/reports/ledgerRptcases.htm?acct={acct}
             — a parameter FORM. Check InclFP (Include Fully Paid — required to
             print copays/payments), ShowCopayAdj, AllVisits, selCase1, selCase2.
             Do NOT check ShowPtntPaym / ShowCopayOnly together: they are
             mutually-exclusive restrictive filters ("Please Check Off Only One").
          2. Submit input[name="Submit"] ("Execute Report"). The response is a
             FRAMESET (<title>Navigator</title>) whose <frame name="ReportBody">
             src is /proxy.cgi/apps/event/RetrieveReport.htm?dt=..&tm=..&pg=0
             (dt/tm are a server-generated report id). Navigate directly to that
             src to read the rendered transaction table.

        Results table (pg=0) — <th> header row:
          "" | Bill | Service | Procedure | Description | Diag | Charge |
          OrigPlan/A | LastPlanBilled/A
        Data <td> rows are charge lines; "Bill Balance" rows are per-bill
        subtotals (skipped). Payment/copay/adjustment rows carry a payment
        keyword in Description and/or a negative/parenthesized amount.

        Returns {"payments": [...], "balance": float|None,
                 "payments_header": [...], "payment_total": float,
                 "charge_total_from_report": float}. Never raises — on an
        unexpected shape it logs a warning and returns empty payments / None
        balance so the charges output is never put at risk.
        """
        EMPTY = {"payments": [], "balance": None, "payments_header": []}
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return EMPTY

        page = self._page

        def _norm(text: str) -> str:
            text = (text or "").replace("\xa0", " ")
            return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

        def _money_or_none(text: str):
            """Strict money parse: returns a signed float, or None if the cell is
            not a single money number. Used to locate the amount column robustly
            (see _amount_of) because on this report the amount cell SHIFTS left by
            one on adjustment/writeoff rows (8 cells) vs charge rows (9 cells), so
            a fixed column index reads the wrong cell. Multi-token cells (e.g. a
            Diag list "722.52 724.8 ...") and plan cells ("Aetna/Y") return None."""
            t = (text or "").replace("\xa0", " ").strip()
            if not t:
                return None
            neg = t.startswith("(") and t.endswith(")")
            t = t.strip("()").replace(",", "").replace("$", "").strip()
            if t.startswith("-"):
                neg = True
                t = t[1:]
            if not re.fullmatch(r"\d+(?:\.\d+)?", t):
                return None
            val = float(t)
            return -val if neg else val

        def _money(text: str) -> float:
            val = _money_or_none(text)
            return val if val is not None else 0.0

        def _amount_of(cell_texts):
            """Return (signed_amount, raw_str) reading the RIGHTMOST cleanly
            money-parseable cell of the row. The transaction amount is always the
            last numeric field before the (non-numeric) plan/blank trailing cells,
            so this is stable across the 8-cell and 9-cell row shapes that a fixed
            idx_charge gets wrong."""
            for c in reversed(cell_texts):
                v = _money_or_none(c)
                if v is not None:
                    return v, c
            return 0.0, ""

        try:
            form_url = (
                f"{self.BASE_URL}/proxy.cgi/off/reports/ledgerRptcases.htm"
                f"?acct={account_number}"
            )
            await self._goto(form_url, wait_until="networkidle", timeout=20000)

            # Set filters to include payments + copays + fully-paid bills.
            for box in ("InclFP", "ShowCopayAdj", "AllVisits", "selCase1", "selCase2"):
                try:
                    el = await page.query_selector(f'input[name="{box}"]')
                    if el and not await el.is_checked():
                        await el.check()
                except Exception:  # noqa: BLE001 — best-effort filter set
                    pass

            submit = await page.query_selector('input[name="Submit"], input[type="submit"]')
            if submit is None:
                logger.warning(
                    "Svigg payments: Execute-Report submit not found (acct=%s)",
                    account_number,
                )
                return EMPTY
            try:
                async with page.expect_navigation(wait_until="networkidle", timeout=25000):
                    await submit.click()
            except Exception as exc:  # noqa: BLE001 — navigation may already be done
                logger.warning("Svigg payments: submit navigation note: %s", exc)

            # The Execute returns a frameset; the real table is in ReportBody.
            frameset_html = await page.content()
            m = re.search(r'name="ReportBody"\s+src="([^"]+)"', frameset_html)
            if not m:
                logger.warning(
                    "Svigg payments: ReportBody frame not found after Execute "
                    "(acct=%s) — report may have bounced to the form (filter "
                    "validation). No payments parsed.",
                    account_number,
                )
                return EMPTY
            body_src = m.group(1).replace("&amp;", "&")
            body_url = body_src if body_src.startswith("http") else f"{self.BASE_URL}{body_src}"
            await self._goto(body_url, wait_until="networkidle", timeout=20000)

            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            # --- Locate the transaction header row (Bill + Procedure + Charge) --
            all_rows = soup.find_all("tr")
            header_idx = -1
            header_cells = []
            for ri, row in enumerate(all_rows):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                norm = [_norm(c) for c in cells]
                if (any(c == "bill" for c in norm)
                        and any("procedure" in c for c in norm)
                        and any("charge" in c for c in norm)):
                    header_idx = ri
                    header_cells = cells
                    break

            logger.info(
                "Svigg payments acct=%s report-header columns: %r",
                account_number, header_cells,
            )
            if header_idx < 0:
                logger.warning(
                    "Svigg payments: transaction header row not found (acct=%s); "
                    "table shape unexpected — returning no payments.",
                    account_number,
                )
                return EMPTY

            col_index = {}
            for i, h in enumerate(header_cells):
                key = _norm(h)
                if key and key not in col_index:
                    col_index[key] = i

            def _find_col(*cands):
                for cand in cands:
                    cn = _norm(cand)
                    if cn in col_index:
                        return col_index[cn]
                for cand in cands:
                    cn = _norm(cand)
                    for key, idx in col_index.items():
                        if cn and cn in key:
                            return idx
                return None

            idx_bill = _find_col("bill")
            idx_service = _find_col("service", "service date", "dos")
            idx_proc = _find_col("procedure", "cpt")
            idx_desc = _find_col("description", "desc")
            idx_diag = _find_col("diag", "diagnosis")
            idx_charge = _find_col("charge", "amount", "amt")

            FB = {"bill": 1, "service": 2, "proc": 3, "desc": 4, "diag": 5, "charge": 6}
            idx_bill = idx_bill if idx_bill is not None else FB["bill"]
            idx_service = idx_service if idx_service is not None else FB["service"]
            idx_proc = idx_proc if idx_proc is not None else FB["proc"]
            idx_desc = idx_desc if idx_desc is not None else FB["desc"]
            idx_diag = idx_diag if idx_diag is not None else FB["diag"]
            idx_charge = idx_charge if idx_charge is not None else FB["charge"]

            def _cell(cells, idx):
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx].get_text(strip=True).replace("\xa0", " ").strip()

            PAY_KEYWORDS = (
                "payment", "copay", "adjustment", "adjust", "write off",
                "writeoff", "refund", "credit", "insurance pmt", "pt pmt",
            )

            payments = []
            total_charges = 0.0
            total_payments = 0.0
            for row in all_rows[header_idx + 1:]:
                cells = row.find_all("td")
                if not cells:
                    continue
                texts = [
                    c.get_text(strip=True).replace("\xa0", " ").strip()
                    for c in cells
                ]
                desc = _cell(cells, idx_desc)
                ndesc = _norm(desc)
                row_norm = _norm(" ".join(texts))
                # Skip per-bill subtotal rows and fully blank rows.
                if "bill balance" in row_norm:
                    continue
                proc = _cell(cells, idx_proc)
                # Amount is the rightmost money cell — NOT a fixed idx_charge,
                # which mis-reads the shifted adjustment/writeoff rows (was
                # returning $0 for every deduction → balance never net of
                # payments). amt is SIGNED: charges +, deductions -.
                amt, amount_str = _amount_of(texts)
                if not desc and not proc and amt == 0.0:
                    continue
                # A row is a payment/adjustment/writeoff (a deduction) if it
                # carries a pay keyword OR its amount is negative. Everything
                # else with a positive amount is a charge.
                is_payment = (
                    any(k in ndesc for k in PAY_KEYWORDS) or amt < 0
                )
                if is_payment:
                    total_payments += abs(amt)
                    payments.append({
                        "date": _cell(cells, idx_service),
                        "type": desc,
                        "code": proc,
                        "amount": amount_str,
                        "bill": _cell(cells, idx_bill),
                    })
                elif amt > 0:
                    total_charges += amt

            # Balance = signed net of every transaction amount = charges minus
            # the absolute value of all deductions.
            balance = round(total_charges - total_payments, 2)
            return {
                "payments": payments,
                "balance": balance,
                "payments_header": header_cells,
                "payment_total": round(total_payments, 2),
                "charge_total_from_report": round(total_charges, 2),
            }
        except Exception as exc:  # noqa: BLE001 — never put charges at risk
            logger.warning(
                "Svigg payments fetch failed (acct=%s): %s — returning no "
                "payments / null balance.",
                account_number, exc,
            )
            return EMPTY

    async def get_patient_bills_fast(self, account_number: str, rowid: str) -> dict:
        """FAST, ADD-ONLY billing read via the pdisplay* endpoint family.

        *** NOT the system-of-record. *** The authoritative net balance remains
        get_patient_ledger()/_fetch_payments() (the ledgerRptcases.htm report),
        which verify_all_v2.py checks. This method reads the cleaner
        pdisplayBilling1.htm data frame directly (no frameset, no two-step report)
        and is ~7-8x faster (measured 2026-07-01: ~1.0s vs ~8.4s wall-clock on the
        three baseline accts). Use it for a quick balance/aging glance or as an
        A/B/diagnostic cross-check — NOT to replace the verified ledger.

        DISCREPANCY (measured live 2026-07-01, A/B on the 3 baseline accts):
          acct 2086507  -> pdisplay Balance col = 0.00   ; ledgerRptcases net = 324.0  (MISMATCH)
          acct 21832702 -> pdisplay Balance col = 8447.00 ; ledgerRptcases net = 8447.0 (match)
          acct 18866600 -> pdisplay Balance col = 0.00   ; ledgerRptcases net = 0.0    (match)
        The two Svigg reports scope charges differently (pdisplay shows full
        account history with an EMR-computed Balance column; ledgerRptcases sums a
        filtered case view). Because they DISAGREE on 2086507, this fast source is
        NOT promoted to system-of-record. Callers must treat `balance` here as the
        EMR's own Balance-column figure, distinct from the verified ledger net.

        Flow (needs rowid — reuse the one from search_patient(); no session token
        needed, the plain /proxy.cgi/apps/pdisplay/... path is cookie-authed):
          GET /proxy.cgi/apps/pdisplay/pdisplayBilling1.htm
              ?rowid={rowid}&acct={acct}&caseno=0&sortcol=Date&direction=down
              &billFilterFrom=&billFilterTo=&ProvFilter=
          The response is a single data table. Its column header row is:
            Date | Description | Office | Provider | TrOffice | Balance | PtntBal |
            InsBal | Charges | InsPaid | GuarPaid | Collections | Adjusted | Incident
          A final <tr> whose first non-empty cell is 'TOTALS' carries the column
          sums. We read that TOTALS row, header-driven (map column name -> index),
          and expose Balance/Charges/InsPaid/GuarPaid/Collections/Adjusted.

        Returns (never raises — on any unexpected shape returns error + null figures
        so it can never regress the authoritative path):
          {
            "account_number": ..., "rowid": ...,
            "balance": <float or None>,          # pdisplay Balance column (NOT the verified net)
            "charges_total": <float or None>,
            "ins_paid": ..., "guar_paid": ..., "collections": ..., "adjusted": ...,
            "totals_columns": {name: value, ...},
            "source": "svigg_pdisplay_fast",
            "authoritative": False,
            "note": "...",
          }
        """
        NULL = {
            "account_number": account_number,
            "rowid": rowid,
            "balance": None,
            "charges_total": None,
            "ins_paid": None,
            "guar_paid": None,
            "collections": None,
            "adjusted": None,
            "totals_columns": {},
            "source": "svigg_pdisplay_fast",
            "authoritative": False,
        }
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {**NULL, "error": "BeautifulSoup (beautifulsoup4) not installed"}

        if not rowid:
            return {**NULL, "error": "rowid is required for the pdisplay fast path"}

        def _money(text):
            t = (text or "").replace("\xa0", " ").strip()
            if not t:
                return None
            neg = t.startswith("(") and t.endswith(")")
            t = t.strip("()").replace(",", "").replace("$", "").strip()
            if t.startswith("-"):
                neg = True
                t = t[1:]
            if not re.fullmatch(r"\d+(?:\.\d+)?", t):
                return None
            val = float(t)
            return -val if neg else val

        def _norm(text):
            text = (text or "").replace("\xa0", " ")
            return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

        page = self._page
        url = (
            f"{self.BASE_URL}/proxy.cgi/apps/pdisplay/pdisplayBilling1.htm"
            f"?rowid={rowid}&acct={account_number}&caseno=0&sortcol=Date"
            f"&direction=down&billFilterFrom=&billFilterTo=&ProvFilter="
        )
        try:
            await self._goto(url, wait_until="networkidle", timeout=20000)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Locate the column-header row (has 'balance' AND 'charges' AND
            # 'adjusted' cells). Skip the degenerate mega-row where the whole
            # table collapses into one cell (its cell count is huge but the
            # individual header cells are still separately present in a normal
            # sibling row — we require a plausible width 10..20).
            header_cells = []
            for row in soup.find_all("tr"):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not (10 <= len(cells) <= 20):
                    continue
                norm = [_norm(c) for c in cells]
                if ("balance" in norm and "charges" in norm and "adjusted" in norm):
                    header_cells = cells
                    break
            if not header_cells:
                logger.warning(
                    "Svigg fast bills: header row not found (acct=%s) — pdisplay "
                    "layout unexpected; returning null figures.", account_number)
                return {**NULL, "error": "pdisplay header row not found"}

            col_index = {}
            for i, h in enumerate(header_cells):
                key = _norm(h)
                if key and key not in col_index:
                    col_index[key] = i

            # Locate the TOTALS row: a plausibly-wide row containing a 'TOTALS'
            # cell (skip the collapsed mega-row via the same width guard).
            totals_cells = []
            for row in soup.find_all("tr"):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not (10 <= len(cells) <= 20):
                    continue
                if any(c.strip().upper() == "TOTALS" for c in cells):
                    totals_cells = cells
                    break
            if not totals_cells:
                logger.warning(
                    "Svigg fast bills: TOTALS row not found (acct=%s); returning "
                    "null figures.", account_number)
                return {**NULL, "error": "pdisplay TOTALS row not found"}

            logger.info(
                "Svigg fast bills acct=%s pdisplay header=%r totals=%r",
                account_number, header_cells, totals_cells)

            def _col(name):
                idx = col_index.get(_norm(name))
                if idx is None or idx >= len(totals_cells):
                    return None
                return _money(totals_cells[idx])

            balance = _col("balance")
            charges_total = _col("charges")
            ins_paid = _col("inspaid")
            guar_paid = _col("guarpaid")
            collections = _col("collections")
            adjusted = _col("adjusted")

            totals_columns = {}
            for name, idx in col_index.items():
                if idx < len(totals_cells):
                    v = _money(totals_cells[idx])
                    if v is not None:
                        totals_columns[name] = v

            return {
                "account_number": account_number,
                "rowid": rowid,
                "balance": balance,
                "charges_total": charges_total,
                "ins_paid": ins_paid,
                "guar_paid": guar_paid,
                "collections": collections,
                "adjusted": adjusted,
                "totals_columns": totals_columns,
                "source": "svigg_pdisplay_fast",
                "authoritative": False,
                "note": (
                    "pdisplayBilling1.htm TOTALS row. 'balance' is the EMR's own "
                    "Balance column, NOT the verified ledgerRptcases net (which is "
                    "the system-of-record via get_patient_ledger). ~7-8x faster but "
                    "disagreed with the verified net on acct 2086507 (0.00 vs 324.0) "
                    "as of 2026-07-01, so it is not authoritative."
                ),
            }
        except Exception as exc:  # noqa: BLE001 — never put the authoritative path at risk
            logger.warning(
                "Svigg fast bills failed (acct=%s): %s — returning null figures.",
                account_number, exc)
            return {**NULL, "error": f"pdisplay fast fetch failed: {exc}"}

    async def get_patient_appointments(self, account_number: str) -> list[dict]:
        """
        Get appointment history for a patient inferred from visit-ledger dates.

        Discovery (2026-06-28): No dedicated per-patient appointment endpoint
        exists in Svigg. Probed ~15 candidate URL patterns — all returned 'Sorry'.
        The patient pentry.htm frameset has ZERO appt/enc/schedule/sched launcher links.

        WORKAROUND: extract past visit dates from the ledger (ven/elist.htm) as a proxy
        for appointment history. Each ledger batch represents a booked visit with a
        visit_date, provider, and billing type.

        Returns list of {visit_date, provider, batch, source} dicts.
        """
        ledger = await self.get_patient_ledger(account_number)
        if "error" in ledger:
            return []

        appointments = []
        for charge in ledger.get("charges", []):
            visit_date = charge.get("visit_date", "")
            if not visit_date:
                continue
            appointments.append({
                "visit_date": visit_date,
                "provider": charge.get("provider", "—"),
                "batch": charge.get("batch", ""),
                "bill_office": charge.get("bill_office", ""),
                "source": "svigg_live",
                "note": "Inferred from visit ledger — Svigg has no standalone appointments endpoint",
            })
        return appointments

    async def get_schedule_day(self, day_offset: int = 0) -> dict:
        """READ-ONLY: scrape the Svigg/Doctor.com per-day 'Appointments' schedule
        report for a single day, selected by DAY OFFSET from today.

        Endpoint (verified live 2026-07-03):
          GET {BASE_URL}/proxy.cgi/off/home/appt_b.htm?todayonly=N
          where N is the integer day offset (0=today, 1=tomorrow, ...). The server
          302-redirects to the office-prefixed .../01/appt_b.htm?todayonly=N which
          returns the day's schedule table. The page shows exactly ONE calendar
          date; every appointment row carries an <a href> to appt_e.htm whose query
          holds date=MM/DD/YYYY, time=HH:MMAM|Noon, enc=NNNN, prov=SGUPTA.

        This is the SCHEDULE-VIEW report (the 'Schedule All Offices/All' the front
        desk sees), NOT the book.htm booking grid — it has a reliable per-day date
        and one row per appointment, so day counts are trustworthy. READ-ONLY: it
        only GETs appt_b.htm; it never posts to any book/cancel/commit route.

        Returns:
          {date: 'YYYY-MM-DD'|'', day_offset, count, appointments:[{...}], source}
          appointments rows: {date, start_time, atime, patient_name, provider,
                              visit_type, cpt, insurance, note, encounter_id, status}
          On failure returns {..., error, appointments: []} (never raises).
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {"date": "", "day_offset": day_offset, "count": 0,
                    "appointments": [], "error": "beautifulsoup4 not installed",
                    "source": "svigg_live"}

        n = int(day_offset)
        url = f"{self.BASE_URL}/proxy.cgi/off/home/appt_b.htm?todayonly={n}"
        try:
            await self._goto(url, wait_until="networkidle", timeout=25000)
            await self._page.wait_for_timeout(1200)
            html = await self._page.content()
        except Exception as exc:  # noqa: BLE001
            return {"date": "", "day_offset": n, "count": 0, "appointments": [],
                    "error": f"schedule fetch failed: {exc}", "source": "svigg_live"}

        soup = BeautifulSoup(html, "html.parser")

        def _cell(cells, idx):
            if 0 <= idx < len(cells):
                return cells[idx].get_text(" ", strip=True)
            return ""

        def _iso_from_mdy(mdy):
            m = re.match(r"(\d{2})/(\d{2})/(\d{4})", mdy or "")
            if m:
                return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
            return ""

        page_date_iso = ""
        appointments = []
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "appt_e.htm" not in href:
                continue
            q = parse_qs(urlparse(href.replace("&amp;", "&")).query)
            row_mdy = (q.get("date") or [""])[0]
            row_date_iso = _iso_from_mdy(row_mdy)
            if row_date_iso and not page_date_iso:
                page_date_iso = row_date_iso
            enc = (q.get("enc") or [""])[0]
            link_time = (q.get("time") or [""])[0]
            prov_code = (q.get("prov") or [""])[0]

            tr = link.find_parent("tr")
            cells = tr.find_all(["td", "th"]) if tr else []
            date_stime = _cell(cells, 1)
            atime = _cell(cells, 2)
            patient_name = _cell(cells, 3)
            provider = _cell(cells, 6) or prov_code
            visit_type = _cell(cells, 9)
            cpt = _cell(cells, 10)
            insurance = _cell(cells, 11)
            note = _cell(cells, 15)

            # Prefer the scheduled start time from the link (HH:MMAM/Noon); fall
            # back to the Date/STime cell's time portion.
            start_time = link_time or (date_stime.split(None, 1)[1]
                                       if " " in date_stime else date_stime)
            # Status: the leftmost cell wraps a check-in icon/link; treat presence
            # of a 'checkin=' link as scheduled/booked, else blank. We do NOT invent
            # a status the portal doesn't show.
            status = ""
            sts_cell = cells[0] if cells else None
            if sts_cell is not None:
                a0 = sts_cell.find("a", href=True)
                if a0 and "checkin=" in a0.get("href", ""):
                    status = "scheduled"

            appointments.append({
                "date": row_date_iso,
                "start_time": start_time,
                "atime": atime,
                "patient_name": patient_name,   # PHI — kept in row, never logged
                "provider": provider,
                "visit_type": visit_type,
                "cpt": cpt,
                "insurance": insurance,
                "note": note,
                "encounter_id": enc,
                "status": status or "scheduled",
            })

        # Fallback page date if no rows carried one.
        if not page_date_iso:
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", html)
            if m:
                page_date_iso = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"

        return {"date": page_date_iso, "day_offset": n,
                "count": len(appointments), "appointments": appointments,
                "source": "svigg_live"}

    async def get_appointment_calendar(self, date: str = None) -> list[dict]:
        """
        Scrape the Svigg global appointment calendar (book.htm frame) for a given date.

        Discovery (2026-06-28, verified live):
          - /proxy.cgi/app/enc/cal.htm is a frameset — the outer doc body is empty.
          - Data lives in the BOOK frame: /proxy.cgi/{session_token}/book.htm
          - Session token is a 9-digit number allocated fresh per browser session.
          - The book frame has 23 tables, ~18KB HTML; patient cells are:
              <a href='mre?x=N&y=M&r=R'>(LENGTH) LastName, FirstInit /Type</a>
          - Filter by date: POST to /proxy.cgi/{token}/calfilt_p with
              dt=MM/DD/YYYY, GO=Refresh. The book frame then reloads in place.
          - Appointment status encoded in cell bgcolor:
              #FFCCFF = booked, #CCFFCC = arrived/checked-in, #FFFF99 = confirmed.

        Args:
            date: ISO date string YYYY-MM-DD (defaults to today).

        Returns list of appointment dicts from the calendar page.

        ⚠ The grid renders a multi-day WINDOW around the requested date and
        rows carry no per-row date — a row in this list is NOT proof the
        appointment falls ON `date` (confirmed live 2026-07-02: a 07/13 appt
        appeared in the 07/09 window). Pin dates via the mre edit form.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return [{"error": "BeautifulSoup (beautifulsoup4) not installed"}]

        if date is None:
            from datetime import datetime as _dt
            date = _dt.now().strftime("%Y-%m-%d")

        page = self._page

        # Navigate to the calendar frameset
        cal_url = f"{self.BASE_URL}/proxy.cgi/app/enc/cal.htm"
        await self._goto(cal_url, wait_until="load", timeout=20000)
        await page.wait_for_timeout(3000)  # allow frames to load

        # Locate the book frame (URL contains 'book.htm')
        book_frame = None
        for frame in page.frames:
            if "book.htm" in frame.url:
                book_frame = frame
                break

        if book_frame is None:
            return [{
                "error": "Could not locate book.htm frame in Svigg calendar",
                "notes": (
                    "cal.htm is a frameset; book frame should appear after load. "
                    "Session may have expired or calendar may require re-login."
                ),
            }]

        # Extract session token from the frame URL for the filter POST
        session_token = None
        import re as _re
        m = _re.search(r'/proxy\.cgi/(\d{7,12})/', book_frame.url)
        if m:
            session_token = m.group(1)

        # If a specific date was requested, post to calfilt_p to filter the view
        date_filter_applied = False
        if date and session_token:
            try:
                # Parse the date for MM/DD/YYYY format Svigg expects
                from datetime import datetime as _dt
                d = _dt.strptime(date, "%Y-%m-%d")
                svigg_date = d.strftime("%m/%d/%Y")

                calfilt_frame = None
                for frame in page.frames:
                    if "calfilt.htm" in frame.url:
                        calfilt_frame = frame
                        break

                if calfilt_frame:
                    await calfilt_frame.fill('input[name="dt"]', svigg_date)
                    await calfilt_frame.click('input[name="GO"]')
                    await page.wait_for_timeout(3000)
                    # Re-locate book frame after refresh
                    for frame in page.frames:
                        if "book.htm" in frame.url:
                            book_frame = frame
                            break
                    date_filter_applied = True
                else:
                    # calfilt frame not found; date could not be applied
                    if date:
                        logger.warning(
                            "Svigg calendar date filter: calfilt.htm frame not found "
                            "for date=%s — requested date could not be applied; "
                            "calendar shows default (today) view.",
                            date,
                        )
            except Exception as e:
                logger.warning("Calendar date filter failed: %s", e)
        elif date and not session_token:
            # session_token not present; date filter cannot be applied
            logger.warning(
                "Svigg calendar date filter: session token not found in book.htm URL "
                "for date=%s — requested date could not be applied; "
                "calendar shows default view.",
                date,
            )

        # Read the book frame content
        try:
            html = await book_frame.content()
        except Exception as e:
            return [{"error": f"Could not read book frame content: {e}"}]

        soup = BeautifulSoup(html, "html.parser")

        # Parse appointment cells — each has an <a href='mre?...'> link
        appointments = []
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "mre?" not in href:
                continue
            raw_text = link.get_text(strip=True)
            if not raw_text:
                continue

            # Format: "(LENGTH) LastName, FirstInit [M] /Type"
            duration_match = _re.match(r'\((\d+)\)\s*(.*)', raw_text)
            duration = None
            name_type = raw_text
            if duration_match:
                duration = int(duration_match.group(1))
                name_type = duration_match.group(2)

            appt_type = ""
            if "/" in name_type:
                parts = name_type.rsplit("/", 1)
                name_type = parts[0].strip()
                appt_type = parts[1].strip()

            # Cell background encodes status
            td = link.find_parent("td")
            bgcolor = td.get("bgcolor", "").upper() if td else ""
            status = {
                "#FFCCFF": "booked",
                "#CCFFCC": "arrived",
                "#FFFF99": "confirmed",
            }.get(bgcolor, "unknown")

            # Extract x/y/r coords from href.
            # r= is the Svigg internal rowid for the calendar cell. It is a
            # stable per-appointment reference within a session, but it is NOT
            # the patient acct number. Resolving r -> acct requires a follow-up
            # GET on the mre? link per cell (deferred to Phase 2).
            coord_match = _re.search(r'x=(\d+)&y=(\d+)(?:&r=(\d+))?', href)
            x = coord_match.group(1) if coord_match else None
            y = coord_match.group(2) if coord_match else None
            r = coord_match.group(3) if coord_match else None
            appointment_ref = str(r) if r is not None else ""

            appointments.append({
                "patient_name": name_type,
                "appointment_type": appt_type,
                "duration_minutes": duration,
                "status": status,
                "cell_x": x,
                "cell_y": y,
                # Svigg internal rowid (from href r=); a stable appointment
                # reference, not necessarily the patient acct. acct resolution
                # is a Phase 2 follow-up (one GET per cell).
                "appointment_ref": appointment_ref,
                "date_requested": date,
                "date_filter_applied": date_filter_applied,
                "source": "svigg_live",
            })

        return appointments

    @staticmethod
    def _session_token_from_url(url: str) -> Optional[str]:
        """Extract the rotating 9-digit Svigg session token from a proxy URL.

        Real path is /proxy.cgi/{SESSION}/… where {SESSION} rotates nearly every
        request (see doctorcom-booking-contract.md §2). NEVER cache this — always
        re-read it from the current live page/frame URL at each step.
        """
        m = re.search(r'/proxy\.cgi/(\d{7,12})/', url or "")
        return m.group(1) if m else None

    async def book_appointment(
        self,
        *,
        acct: str = "",
        rowid: str = "",
        last_name: str = "",
        first_name: str = "",
        date: str = "",
        start_time: str = "",
        duration_min: int = 15,
        appt_type: str = "EST",
        provider: str = "SGUPTA",
        note: str = "",
        incident: str = "",
        x: Optional[int] = None,
        y: Optional[int] = None,
        execute: bool = False,
        confirm_unverified: bool = False,
        allow_overbook: bool = False,
    ) -> dict:
        """Walk the Svigg add-appointment flow to the prepared `bk_p` (conf) form.

        SAFETY: By DEFAULT (execute=False) this is a DRY-RUN that prepares the
        booking form and returns WITHOUT submitting — it CREATES NOTHING. The
        commit (POST to bk_p) is gated behind TWO independent locks and is
        UNVERIFIED pending a HAR capture (doctorcom-booking-contract.md §6).

        Flow (all read-only until the final bk_p Submit, per contract §7):
          /proxy.cgi/app/enc/cal.htm  (frameset)
            → /proxy.cgi/{SESSION}/book.htm  (grid; free slot = add.htm?x&y)
            → [read-only] calfilt_p filter to provider+date
            → click add.htm?x&y  (GET — renders patient-search form)
            → form name=psrch  POST addLookup  (read-only patient name search)
            → "Patient View" results
            → click calAddLookup?rowid&acct  (GET — renders the conf form)
            → form name=conf, action POST /proxy.cgi/{SESSION}/bk_p  (STOP HERE)

        Args:
            acct: numeric patient account (e.g. "2574961").
            rowid: Svigg internal patient rowid (e.g. "AAA...@main01").
            last_name/first_name: used to resolve rowid/acct if not supplied,
                AND to verify the patient-select link matches the intended
                patient before clicking it.
            date: appointment date, MM/DD/YYYY.
            start_time: appointment start time string (e.g. "8:00a" / "08:00").
            duration_min: appointment length in minutes.
            appt_type: cpt00 select value — "EST" (established) or "NP" (new).
            provider: prov select value (default "SGUPTA").
            note: free-text note (maps to note00 / Note).
            incident: optional Incident radio value (case to bill). Left blank
                in propose unless explicitly supplied (value→case map UNVERIFIED).
            x, y: explicit free-slot grid coords. If omitted, the first free
                add.htm?x&y on the filtered grid is used.
                ⚠ Date binding (learned live 2026-07-02): the CELL decides the
                appointment's real date+time — FromDate/FromTime do NOT
                override it, and the x column↔date mapping is view-dependent
                (a smoke booking requested 07/09 via x=4,y=40 and landed on
                07/13 10:00AM). Never trust an x from a prior dry-run to pin a
                date; verify the real date afterwards (the cancel path
                day-binds on the mre edit form for exactly this reason).
            execute: if False (DEFAULT) → propose only, never POST. If True →
                still blocked unless BOTH BOOKING_EXECUTE_ENABLED and
                confirm_unverified are also True.
            confirm_unverified: caller's explicit acknowledgement that the
                commit contract is unverified. Required (with the module flag)
                to ever POST.

        Returns one of:
          {status:"prepared", action_url, fields:{…}, slot, acct, rowid, warning}
          {status:"execute_blocked", reason, prepared:{…}}
          {status:"submitted_unverified", response_*, warning}   (only if both
              locks open — NOT exercised in testing)
          {status:"error", error, stage}
        """
        page = self._page

        # ---- Resolve patient identity (rowid/acct) ----------------------
        if (not rowid or not acct) and last_name:
            try:
                matches = await self.search_patient(last_name, first_name)
            except Exception as exc:
                return {"status": "error", "stage": "resolve_patient",
                        "error": f"search_patient failed: {exc}"}
            picked = None
            for mrow in matches:
                if acct and mrow.get("acct") == acct:
                    picked = mrow
                    break
                if not acct and mrow.get("rowid"):
                    picked = mrow
                    break
            if not picked:
                return {"status": "error", "stage": "resolve_patient",
                        "error": "could not resolve patient from name; "
                                 "pass explicit acct+rowid",
                        "match_count": len(matches)}
            rowid = rowid or picked.get("rowid", "")
            acct = acct or picked.get("acct", "")

        if not rowid or not acct:
            return {"status": "error", "stage": "resolve_patient",
                    "error": "acct and rowid are required (or a resolvable "
                             "last_name)"}

        # ---- Navigate to calendar frameset + locate book frame ----------
        try:
            cal_url = f"{self.BASE_URL}/proxy.cgi/app/enc/cal.htm"
            await self._goto(cal_url, wait_until="load", timeout=20000)
            await page.wait_for_timeout(3000)
        except Exception as exc:
            return {"status": "error", "stage": "calendar",
                    "error": f"could not open calendar frameset: {exc}"}

        book_frame = None
        for frame in page.frames:
            if "book.htm" in frame.url:
                book_frame = frame
                break
        if book_frame is None:
            return {"status": "error", "stage": "calendar",
                    "error": "could not locate book.htm frame (session may have "
                             "expired or login required)"}

        # ---- Optional read-only date/provider filter (calfilt_p) --------
        date_filter_applied = False
        if date:
            try:
                calfilt_frame = None
                for frame in page.frames:
                    if "calfilt.htm" in frame.url:
                        calfilt_frame = frame
                        break
                if calfilt_frame:
                    await calfilt_frame.fill('input[name="dt"]', date)
                    # Provider filter is best-effort; the field name is "prov".
                    try:
                        await calfilt_frame.fill('input[name="prov"]', provider)
                    except Exception:
                        pass  # prov may be a select / absent — non-fatal
                    await calfilt_frame.click('input[name="GO"]')
                    await page.wait_for_timeout(3000)
                    for frame in page.frames:
                        if "book.htm" in frame.url:
                            book_frame = frame
                            break
                    date_filter_applied = True
            except Exception as exc:
                logger.warning("Svigg booking date filter failed (date=%s): %s",
                               date, exc)

        # Re-read the rotating token from the CURRENT book frame URL.
        session_token = self._session_token_from_url(book_frame.url)

        # ---- Locate the free slot anchor add.htm?x&y --------------------
        try:
            if x is not None and y is not None:
                slot_x, slot_y = int(x), int(y)
                slot_href = f"add.htm?x={slot_x}&y={slot_y}"
                slot_locator = book_frame.locator(
                    f'a[href*="add.htm?x={slot_x}&y={slot_y}"]'
                )
                if await slot_locator.count() == 0:
                    return {"status": "error", "stage": "slot",
                            "error": f"requested slot x={slot_x},y={slot_y} is "
                                     "not a free add.htm anchor on the grid"}
            else:
                # First FREE slot = first add.htm?x&y anchor on the grid.
                add_links = book_frame.locator('a[href*="add.htm?x="]')
                n_free = await add_links.count()
                if n_free == 0:
                    return {"status": "error", "stage": "slot",
                            "error": "no free add.htm slots on the filtered grid "
                                     "(day full or filter returned nothing)",
                            "date_filter_applied": date_filter_applied}
                slot_locator = add_links.first
                slot_href = await slot_locator.get_attribute("href") or ""
                cm = re.search(r'add\.htm\?x=(\d+)&y=(\d+)', slot_href)
                slot_x = int(cm.group(1)) if cm else None
                slot_y = int(cm.group(2)) if cm else None
        except Exception as exc:
            return {"status": "error", "stage": "slot",
                    "error": f"slot lookup failed: {exc}"}

        # ---- Click the free slot (GET) → patient-search form (psrch) ----
        try:
            await slot_locator.first.click()
            await page.wait_for_timeout(2000)
        except Exception as exc:
            return {"status": "error", "stage": "add_form",
                    "error": f"could not open add.htm slot form: {exc}"}

        # Find the frame that now hosts the psrch search form.
        psrch_frame = None
        for frame in page.frames:
            try:
                if await frame.query_selector('form[name="psrch"]'):
                    psrch_frame = frame
                    break
            except Exception:
                continue
        if psrch_frame is None:
            return {"status": "error", "stage": "add_form",
                    "error": "patient-search form (name=psrch) did not render "
                             "after opening the slot"}

        # ---- Read-only patient name search (POST addLookup) -------------
        # Identical in kind to search_patient — a name lookup, not a write.
        try:
            search_last = last_name or ""
            if not search_last and rowid:
                # We have rowid/acct but no name; a blank-ish lookup may not
                # surface the patient. Prefer a name when available; otherwise
                # fall back to acct-based field if present on the form.
                pass
            if search_last:
                ln_input = await psrch_frame.query_selector('input[name="LastName"]')
                if ln_input:
                    await psrch_frame.fill('input[name="LastName"]', search_last)
                if first_name:
                    fn_input = await psrch_frame.query_selector('input[name="FirstName"]')
                    if fn_input:
                        await psrch_frame.fill('input[name="FirstName"]', first_name)
            else:
                acct_input = await psrch_frame.query_selector('input[name="acct"]')
                if acct_input:
                    await psrch_frame.fill('input[name="acct"]', acct)
            # Submit the search form (read-only). Prefer a named search button.
            submit_btn = (
                await psrch_frame.query_selector('input[name="NameSearch"]')
                or await psrch_frame.query_selector('input[type="submit"]')
            )
            if submit_btn:
                await submit_btn.click()
            else:
                await psrch_frame.evaluate(
                    'document.forms["psrch"] && document.forms["psrch"].submit()'
                )
            await page.wait_for_timeout(2500)
        except Exception as exc:
            return {"status": "error", "stage": "patient_lookup",
                    "error": f"patient lookup (addLookup) failed: {exc}"}

        # ---- Locate + verify the calAddLookup select link for OUR patient
        results_frame = None
        select_link = None
        for frame in page.frames:
            try:
                links = await frame.query_selector_all(
                    'a[href*="calAddLookup?rowid="]'
                )
            except Exception:
                links = []
            for link in links:
                href = await link.get_attribute("href") or ""
                href_rowid = ""
                href_acct = ""
                rm = re.search(r'rowid=([^&|]+)', href)
                am = re.search(r'acct=([^&|]+)', href)
                if rm:
                    href_rowid = rm.group(1)
                if am:
                    href_acct = am.group(1)
                # HARD verification: only the link whose acct (and rowid, when
                # known) matches the intended patient is eligible. This is the
                # same guardrail the recon used to ensure ONLY Test,Patient was
                # selected.
                if href_acct == acct and (not rowid or href_rowid == rowid):
                    results_frame = frame
                    select_link = link
                    break
            if select_link:
                break

        if select_link is None:
            return {"status": "error", "stage": "patient_select",
                    "error": "no calAddLookup link matched the intended "
                             "acct/rowid in the Patient View results — refusing "
                             "to click a non-matching patient",
                    "acct": acct}

        # ---- Click select link (GET) → conf form (action=bk_p) ----------
        try:
            await select_link.click()
            await page.wait_for_timeout(2500)
        except Exception as exc:
            return {"status": "error", "stage": "conf_form",
                    "error": f"could not open conf form via calAddLookup: {exc}"}

        # Find the frame hosting the conf form.
        conf_frame = None
        for frame in page.frames:
            try:
                if await frame.query_selector('form[name="conf"]'):
                    conf_frame = frame
                    break
            except Exception:
                continue
        if conf_frame is None:
            return {"status": "error", "stage": "conf_form",
                    "error": "final booking form (name=conf) did not render"}

        # Re-read the rotating token from the conf frame's own URL.
        conf_token = self._session_token_from_url(conf_frame.url) or session_token

        # Resolve the form action (absolute bk_p URL with the LIVE token).
        try:
            form_action = await conf_frame.eval_on_selector(
                'form[name="conf"]', 'f => f.action'
            )
        except Exception:
            form_action = ""
        if not form_action and conf_token:
            form_action = f"{self.BASE_URL}/proxy.cgi/{conf_token}/bk_p"

        # ---- Build the field dict we WOULD submit (no POST) -------------
        # End time = start + duration. We don't reformat the time string —
        # accepted formats are UNVERIFIED (contract §6) so we pass through
        # what the caller gave for start, and mirror it for end with a note.
        fields = {
            "Incident": incident,              # case to bill (value→case UNVERIFIED)
            "cpt00": appt_type,                # EST | NP
            "Dept00": "",                      # hidden, empty on fresh form
            "Duration00": str(duration_min),   # minutes
            "note00": note,                    # per-row note
            "FromDate": date,                  # MM/DD/YYYY
            "FromTime1": start_time,           # start (format UNVERIFIED)
            "ToDate": date,
            "ToTime1": "",                     # = From + Duration (UNVERIFIED format)
            "prov": provider,                  # SGUPTA + group codes
            "Note": note,                      # appt-level note
            "off": "bal",                      # hidden context flag
            "TFORMCOUNT": "",                  # anti-double-submit counter (read live)
        }
        # Read the live TFORMCOUNT value from the rendered form if present.
        try:
            tfc = await conf_frame.eval_on_selector(
                'form[name="conf"] input[name="TFORMCOUNT"]', 'e => e.value'
            )
            if tfc is not None:
                fields["TFORMCOUNT"] = str(tfc)
        except Exception:
            pass

        prepared = {
            "status": "prepared",
            "action_url": form_action,
            "fields": fields,
            "slot": {"x": slot_x, "y": slot_y, "href": slot_href},
            "acct": acct,
            "rowid": rowid,
            "date_filter_applied": date_filter_applied,
            "warning": ("booking contract UNVERIFIED pending HAR — NOT submitted; "
                        "no appointment created"),
        }

        # ================================================================
        # COMMIT GUARD — two independent locks, fail-closed.
        # ================================================================
        if not execute:
            # DEFAULT propose path: return the prepared form, submit nothing.
            return prepared

        # execute=True requested. Still blocked unless BOTH locks are open.
        if not (BOOKING_EXECUTE_ENABLED and confirm_unverified):
            return {
                "status": "execute_blocked",
                "reason": ("booking commit is unverified (HAR pending) and "
                           "disabled by default; requires "
                           "BOOKING_EXECUTE_ENABLED=True in source AND "
                           "confirm_unverified=True"),
                "flag_enabled": BOOKING_EXECUTE_ENABLED,
                "confirm_unverified": confirm_unverified,
                "prepared": prepared,
            }

        # Lock 3 (allowlist) — enforced AT the POST site: only test accts commit
        # unless the allowlist was widened via SVIGG_BOOKING_ALLOWLIST ("*" = any).
        if not ("*" in BOOKING_ALLOWED_ACCTS or str(acct) in BOOKING_ALLOWED_ACCTS):
            return {
                "status": "execute_blocked",
                "reason": (f"acct {acct!r} is not in BOOKING_ALLOWED_ACCTS — "
                           "commit is restricted to designated test accounts"),
                "allowed": sorted(BOOKING_ALLOWED_ACCTS),
                "prepared": prepared,
            }

        # All three locks open — the ONLY path that POSTs bk_p. The slot cell
        # (x/y) encodes day+time; we also set FromTime1/ToTime1 consistently and
        # explicitly select cpt00/prov in the DOM (they default blank on the form).
        try:
            end_time = ""
            try:
                from datetime import datetime as _dt, timedelta as _td
                _st = _dt.strptime(start_time.strip().upper().replace(" ", ""), "%I:%M%p")
                end_time = (_st + _td(minutes=duration_min)).strftime("%I:%M%p")
            except Exception:
                end_time = ""
            # cpt00 + prov are <select>s — must be selected in the DOM, not just
            # filled. A silent failure here books the WRONG type/provider, so
            # read back the DOM value and abort before Submit on any mismatch.
            select_failures = {}
            try:
                await conf_frame.select_option('select[name="cpt00"]', appt_type)
            except Exception as exc:
                select_failures["cpt00"] = str(exc)
            try:
                await conf_frame.select_option('select[name="prov"]', provider)
            except Exception as exc:
                select_failures["prov"] = str(exc)
            for _fld, _want in (("cpt00", appt_type), ("prov", provider)):
                try:
                    _got = await conf_frame.eval_on_selector(
                        f'select[name="{_fld}"]', 'e => e.value')
                except Exception as exc:
                    _got = None
                    select_failures.setdefault(_fld, f"readback failed: {exc}")
                if _got != _want:
                    try:
                        _opts = await conf_frame.eval_on_selector(
                            f'select[name="{_fld}"]',
                            'e => Array.from(e.options).map(o => o.value)')
                    except Exception:
                        _opts = []
                    return {"status": "error", "stage": "form_fill",
                            "error": (f"{_fld} select reads back {_got!r} != requested "
                                      f"{_want!r} — aborting before Submit (would have "
                                      f"booked the wrong "
                                      f"{'appointment type' if _fld == 'cpt00' else 'provider'})"),
                            "select_failures": select_failures,
                            "available_options": _opts,
                            "prepared": prepared}
            await conf_frame.fill('input[name="Duration00"]', str(duration_min))
            if date:
                await conf_frame.fill('input[name="FromDate"]', date)
                await conf_frame.fill('input[name="ToDate"]', date)   # From==To => single day
            if start_time:
                await conf_frame.fill('input[name="FromTime1"]', start_time)
            if end_time:
                try:
                    await conf_frame.fill('input[name="ToTime1"]', end_time)
                except Exception:
                    pass
            if incident:
                try:
                    await conf_frame.check(f'input[name="Incident"][value="{incident}"]')
                except Exception:
                    pass
            await conf_frame.click('input[name="Submit"]')
            await page.wait_for_timeout(2500)
            import re as _re
            try:
                body = await conf_frame.content()
            except Exception:
                body = ""
            low = (body or "").lower()

            def _clean(h):
                return _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", h or "")).strip()

            # The server may gate on an allocation warning ("Slot is over
            # allocated. Hit Overbook to force appointment.") — a 2nd confirm.
            if "over allocated" in low or "overbook" in low:
                try:
                    controls = await conf_frame.eval_on_selector_all(
                        'input[type="submit"],input[type="button"],input[type="image"],button',
                        'els => els.map(e => ({name:e.name||"", value:e.value||e.alt||e.textContent||""}))'
                    )
                except Exception:
                    controls = []
                if not allow_overbook:
                    return {
                        "status": "overbook_required",
                        "reason": ("server reports the slot is over-allocated for "
                                   "this provider; a second Overbook confirm is "
                                   "required to force the appointment"),
                        "controls": controls,
                        "response_url": conf_frame.url,
                        "response_excerpt": _clean(body)[:1200],
                        "submitted_fields": {**fields, "FromTime1": start_time,
                                             "ToTime1": end_time, "cpt00": appt_type,
                                             "prov": provider},
                        "prepared": prepared,
                    }
                # allow_overbook=True → click the Overbook control to force it.
                clicked = None
                for sel in ('input[value="Overbook" i]', 'input[value*="Overbook" i]',
                            'input[name="Overbook" i]', 'input[name*="Overbook" i]',
                            'button:has-text("Overbook")'):
                    try:
                        await conf_frame.click(sel, timeout=4000)
                        clicked = sel
                        break
                    except Exception:
                        continue
                await page.wait_for_timeout(2500)
                try:
                    body = await conf_frame.content()
                except Exception:
                    pass
                return {
                    "status": "submitted_overbooked" if clicked else "overbook_click_failed",
                    "overbook_control": clicked,
                    "controls": controls,
                    "response_url": conf_frame.url,
                    "response_excerpt": _clean(body)[:1500],
                    "submitted_fields": {**fields, "FromTime1": start_time,
                                         "ToTime1": end_time, "cpt00": appt_type,
                                         "prov": provider, "Duration00": str(duration_min)},
                    "prepared": prepared,
                    "warning": ("forced overbook — VERIFY via calendar re-read; "
                                "cancel if this was a test"),
                }

            return {
                "status": "submitted",
                "response_url": conf_frame.url,
                "response_excerpt": _clean(body)[:1500],
                "submitted_fields": {**fields, "FromTime1": start_time,
                                     "ToTime1": end_time, "cpt00": appt_type,
                                     "prov": provider, "Duration00": str(duration_min)},
                "prepared": prepared,
                "warning": ("appointment submitted — VERIFY by re-reading the "
                            "calendar; cancel if this was a test"),
            }
        except Exception as exc:
            return {"status": "error", "stage": "submit",
                    "error": f"bk_p submit failed: {exc}", "prepared": prepared}

    @staticmethod
    def _time_to_slot_y(t: str) -> int:
        """Convert a start time to the book-grid 15-min row index (y).

        The grid is midnight-anchored in 15-minute rows; anchored live on
        2026-07-01: y=40 == 10:00AM. Accepts "10:00AM" / "10:00 am" / "14:30".
        """
        s = t.strip().upper().replace(" ", "")
        from datetime import datetime as _dt
        parsed = None
        for fmt in ("%I:%M%p", "%H:%M"):
            try:
                parsed = _dt.strptime(s, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"unparseable time {t!r} (want e.g. '10:00AM' or '14:30')")
        minutes = parsed.hour * 60 + parsed.minute
        if minutes % 15:
            raise ValueError(f"time {t!r} is not on a 15-minute grid row")
        return minutes // 15

    async def cancel_appointment(
        self,
        *,
        acct: str,
        date: str,
        last_name: str = "",
        first_name: str = "",
        time: str = "",
        appointment_ref: str = "",
        reason: str = "or",
        confirm: bool = False,
    ) -> dict:
        """Cancel a Svigg appointment for a designated TEST patient. DESTRUCTIVE.

        Encodes the cancel flow proven live on 2026-07-01 (do_cancel3.py):
          calendar grid appt cell = <a href="mre?x=DAY&y=TIMEROW&r=INDEX">
            → click it → form `mre` (buttons Submit / Cancel[=Exit] /
              Reschedule / Delete)
            → click input[name="Delete"]
            → "Confirm Cancellation" page (form action
              /proxy.cgi/{SESSION}/cancel_p) with a REQUIRED
              <select name="CancelReason"> ("or"=Office Requested,
              "pr"=Patient Requested) + input[name="Yes"]
            → the reason MUST be selected BEFORE clicking Yes or the form
              bounces back unchanged.

        SAFETY (fail-closed, checked BEFORE any Delete click):
          returns {status:"execute_blocked"} unless confirm is True AND
          str(acct) is in BOOKING_ALLOWED_ACCTS (currently the designated
          test record only). An identity guard additionally verifies the
          opened mre edit form belongs to the target patient before Delete.
          The identity guard additionally requires the requested date to appear
          on the edit form before Delete.

        TODO(reschedule): the mre form's input[name="Reschedule"] is the
        entry point for rescheduling, but the downstream contract (picking a
        new slot after clicking it) is UNVERIFIED — needs a HAR/live capture
        before any reschedule method is implemented. Do not guess it.

        Args:
            acct: numeric patient account (gate key — must be allowlisted).
            date: appointment date, ISO YYYY-MM-DD (passed to
                get_appointment_calendar for the grid filter).
            last_name/first_name: used to locate the appt cell on the grid
                (cell text is "(LEN) LastName, FirstInit /Type") and for the
                identity guard on the mre form.
            time: optional appointment start time, e.g. "10:00AM" or "14:30",
                used to disambiguate when the patient has multiple appointments
                that day — converted to the book-grid 15-min row index.
            appointment_ref: exact Svigg cell ref (the r= value returned by
                get_appointment_calendar) — takes precedence over time.
            reason: CancelReason select value — "or" (Office Requested,
                default) or "pr" (Patient Requested).
            confirm: must be explicitly True to perform the cancel.

        Returns one of:
          {status:"not_found", ...}         appt not on that day's grid
          {status:"ambiguous", candidates:[...]}  multiple appts match — refuse
          {status:"execute_blocked", ...}   gate closed (confirm/allowlist)
          {status:"cancelled", verified, remaining_test_rows, ...}
          {status:"error", stage, error}
        """
        page = self._page

        if reason not in ("or", "pr"):
            return {"status": "error", "stage": "args",
                    "error": f"reason must be 'or' or 'pr', got {reason!r}"}
        if not last_name:
            return {"status": "error", "stage": "args",
                    "error": "last_name is required to locate the appointment "
                             "cell on the calendar grid"}
        target_y = None
        if time:
            try:
                target_y = self._time_to_slot_y(time)
            except ValueError as exc:
                return {"status": "error", "stage": "args", "error": str(exc)}

        # Accept any stray JS confirm() the portal may raise around Delete.
        def _on_dialog(d):
            asyncio.ensure_future(d.accept())
        page.on("dialog", _on_dialog)

        try:
            # ---- 1. Navigate the calendar to the target date -------------
            cal = await self.get_appointment_calendar(date)
            if cal and isinstance(cal[0], dict) and cal[0].get("error"):
                return {"status": "error", "stage": "calendar",
                        "error": cal[0]["error"]}
            # H1 guard: get_appointment_calendar silently falls back to TODAY's
            # grid if the date filter can't be applied. Refuse to locate/cancel
            # unless the requested date was actually applied.
            if cal and not any(isinstance(a, dict) and a.get("date_filter_applied")
                               for a in cal):
                return {"status": "error", "stage": "calendar_date",
                        "error": (f"date filter for {date} was not applied "
                                  "(calendar fell back to default view) — refusing "
                                  "to cancel against a possibly-wrong day")}

            def _matches(appt: dict) -> bool:
                name = str(appt.get("patient_name", "")).lower()
                if last_name.lower() not in name:
                    return False
                if first_name:
                    # Cell text is "LastName, FirstInit" — anchor the initial to
                    # the comma instead of matching it anywhere in the string.
                    im = re.search(r',\s*([a-z])', name)
                    if im:
                        return im.group(1) == first_name[0].lower()
                    return first_name[0].lower() in name.replace(last_name.lower(), "")
                return True

            before_rows = [a for a in cal if isinstance(a, dict) and _matches(a)]
            if not before_rows:
                return {"status": "not_found", "date": date,
                        "detail": f"no calendar cell matching {last_name!r} "
                                  f"on {date}",
                        "calendar_rows": len(cal)}

            # ---- 1b. Disambiguate to a single appt BEFORE the gate --------
            # A name can match several same-day rows; pin down exactly one via
            # appointment_ref (exact) or time (grid row) — refuse to guess.
            candidates = before_rows
            if appointment_ref:
                candidates = [a for a in candidates
                              if str(a.get("appointment_ref", "")) == str(appointment_ref)]
            elif time:
                candidates = [a for a in candidates
                              if str(a.get("cell_y", "")) == str(target_y)]
            if not candidates:
                return {"status": "not_found", "date": date,
                        "detail": (f"{len(before_rows)} calendar row(s) match "
                                   f"{last_name!r} but none match the requested "
                                   f"slot (time={time!r}, appointment_ref={appointment_ref!r})"),
                        "slots": [{k: a.get(k) for k in ("cell_x", "cell_y", "appointment_ref", "appointment_type", "status")} for a in before_rows]}
            if len(candidates) > 1:
                return {"status": "ambiguous", "date": date,
                        "detail": ("multiple appointments match — pass time=... or "
                                   "appointment_ref=... to pick exactly one; refusing to guess"),
                        "candidates": [{k: a.get(k) for k in ("cell_x", "cell_y", "appointment_ref", "appointment_type", "status", "duration_minutes")} for a in candidates]}
            target = candidates[0]

            # ---- 2. GATE — fail-closed, BEFORE opening/deleting anything --
            # The appt is located by NAME, so binding to the allowlisted acct
            # alone is insufficient (acct is a trust-me token here). ALSO require
            # the caller's last_name to match the acct's bound name, so an
            # allowlisted acct + a real patient's name is rejected.
            bound_name = CANCEL_ALLOWED_ACCT_NAMES.get(str(acct))
            name_ok = (bound_name is not None and bound_name in last_name.lower()) \
                or (CANCEL_ALLOW_ANY and bool(last_name))
            if not (confirm is True and name_ok):
                return {
                    "status": "execute_blocked",
                    "reason": ("cancel is DESTRUCTIVE and fail-closed: requires "
                               "confirm=True AND acct in the cancel allowlist AND "
                               "last_name matching that acct's bound name "
                               f"(confirm={confirm}, acct_allowed="
                               f"{bound_name is not None}, name_ok={name_ok})"),
                    "allowed_accts": sorted(CANCEL_ALLOWED_ACCT_NAMES),
                    "found_rows": len(before_rows),
                    "date": date,
                }

            # ---- 3. Open the appt cell (mre? link) ------------------------
            # Click the EXACT gated candidate. href substring matching collides
            # (r=7 also matches r=71; y=4 matches y=40..49), so enumerate the
            # mre? anchors and compare their PARSED x/y/r for strict equality.
            want_x = str(target.get("cell_x"))
            want_y = str(target.get("cell_y"))
            want_r = str(target.get("appointment_ref") or "")
            cell_handle = None
            exact_hits = 0
            for fr in page.frames:
                try:
                    anchors = await fr.query_selector_all('a[href*="mre?"]')
                except Exception:
                    continue
                if not anchors:
                    continue
                for a_el in anchors:
                    href = (await a_el.get_attribute("href")) or ""
                    am = re.search(r'x=(\d+)&y=(\d+)(?:&r=(\d+))?', href)
                    if not am:
                        continue
                    ax, ay, ar = am.group(1), am.group(2), am.group(3) or ""
                    if ax == want_x and ay == want_y and (not want_r or ar == want_r):
                        exact_hits += 1
                        if cell_handle is None:
                            cell_handle = a_el
                if cell_handle is not None:
                    break  # the grid lives in one frame; stop after the hit frame
            if cell_handle is None:
                return {"status": "error", "stage": "locate_cell",
                        "error": (f"calendar parsed a matching row but no mre? anchor "
                                  f"parses to exactly x={want_x} y={want_y}"
                                  f"{' r=' + want_r if want_r else ''}")}
            if exact_hits > 1:
                return {"status": "error", "stage": "locate_cell",
                        "error": (f"{exact_hits} anchors parse to exactly x={want_x} "
                                  f"y={want_y}{' r=' + want_r if want_r else ''} — "
                                  "true duplicates; refusing to guess")}
            await cell_handle.click()
            await page.wait_for_timeout(1800)

            # ---- 4. Identity guard on the mre edit form -------------------
            # The opened frame must have the Delete button AND identify the
            # target patient (name or acct) before we touch anything.
            # Day-binding: the grid may render several day columns and cell_x
            # cannot be tied to a date, so require the edit form itself to show
            # the requested date before Delete (formats Svigg plausibly renders).
            from datetime import datetime as _dt_guard
            _gd = _dt_guard.strptime(date, "%Y-%m-%d")
            date_forms = {
                _gd.strftime("%m/%d/%Y"),
                f"{_gd.month}/{_gd.day}/{_gd.year}",
                f"{_gd.month:02d}/{_gd.day}/{_gd.year}",
                f"{_gd.month}/{_gd.day:02d}/{_gd.year}",
                _gd.strftime("%Y-%m-%d"),
            }
            edit_frame = None
            name_frame_wrong_day = False
            for fr in page.frames:
                try:
                    hh = await fr.content()
                    # Require the opened edit form to actually SHOW the patient
                    # name (AND, not OR): the acct is often absent from this form,
                    # so name is the reliable binding; the gate already bound
                    # acct -> name, so name-match here confirms the right record.
                    if 'name="Delete"' in hh and last_name.lower() in hh.lower():
                        if any(df in hh for df in date_forms):
                            edit_frame = fr
                            break
                        # Right patient, but the form doesn't show the requested
                        # day — the grid may have opened another day's appt.
                        name_frame_wrong_day = True
                except Exception:
                    pass
            if edit_frame is None and name_frame_wrong_day:
                return {"status": "error", "stage": "identity_guard_date",
                        "error": (f"mre edit form shows the patient but not the "
                                  f"requested date {date} in any expected format "
                                  f"({sorted(date_forms)}) — the grid may have "
                                  f"opened a different day's appointment; ABORTED "
                                  f"before Delete")}
            if edit_frame is None:
                return {"status": "error", "stage": "identity_guard",
                        "error": f"mre edit form did not show the target patient "
                                 f"name {last_name!r} — ABORTED before Delete"}

            # ---- 5. Delete → Confirm Cancellation (cancel_p) --------------
            await edit_frame.click('input[name="Delete"]')
            await page.wait_for_timeout(2500)

            confirm_frame = None
            for fr in page.frames:
                try:
                    if "cancel_p" in (await fr.content()):
                        confirm_frame = fr
                        break
                except Exception:
                    pass
            if confirm_frame is None:
                return {"status": "error", "stage": "confirm_page",
                        "error": "Confirm Cancellation page (cancel_p form) "
                                 "did not appear after Delete"}

            # REQUIRED: select the reason BEFORE Yes, or the form bounces.
            await confirm_frame.select_option(
                'select[name="CancelReason"]', reason)
            await confirm_frame.click('input[name="Yes"]')
            await page.wait_for_timeout(3000)

            # ---- 6. Verify by re-reading the calendar ---------------------
            # Keep the name-match counts (rows_before/remaining_test_rows) for
            # context, but verify against the SAME slot filter we cancelled, so
            # a different same-day appt for this patient can't mask success.
            cal_after = await self.get_appointment_calendar(date)
            # The re-read itself can fail or fall back to an unfiltered grid —
            # never count a broken re-read as proof the cancel worked. An empty
            # day also can't prove the filter applied (no rows carry the marker),
            # so it stays unverified — honest over convenient.
            reread_ok = bool(cal_after) and not (
                isinstance(cal_after[0], dict) and cal_after[0].get("error")
            ) and any(isinstance(a, dict) and a.get("date_filter_applied")
                      for a in cal_after)
            after_rows = [a for a in cal_after
                          if isinstance(a, dict) and _matches(a)]
            candidates_after = after_rows
            if appointment_ref:
                candidates_after = [a for a in candidates_after
                                    if str(a.get("appointment_ref", "")) == str(appointment_ref)]
            elif time:
                candidates_after = [a for a in candidates_after
                                    if str(a.get("cell_y", "")) == str(target_y)]
            verified = reread_ok and len(candidates_after) < len(candidates)

            if not reread_ok:
                warning = ("post-cancel calendar re-read failed or lost the date "
                           "filter — cancel was submitted but could NOT be "
                           "verified; check manually")
            elif not verified:
                warning = ("cancel submitted but calendar re-read still shows "
                           "a matching row — verify manually")
            else:
                warning = ""

            return {
                "status": "cancelled",
                "verified": verified,
                "date": date,
                "reason": reason,
                "rows_before": len(before_rows),
                "remaining_test_rows": len(after_rows),
                "slot": {"cell_x": target.get("cell_x"),
                         "cell_y": target.get("cell_y"),
                         "appointment_ref": target.get("appointment_ref")},
                "warning": warning,
            }
        except Exception as exc:
            return {"status": "error", "stage": "cancel_flow",
                    "error": f"{exc}"}
        finally:
            try:
                page.remove_listener("dialog", _on_dialog)
            except Exception:
                pass

    async def search_and_summarize(self, last_name: str, first_name: str = "") -> list[dict]:
        """
        Search for patients and return full summaries for each match.
        Combines search_patient + get_patient_summary for convenience.
        """
        patients = await self.search_patient(last_name, first_name)

        summaries = []
        for p in patients[:10]:  # Cap at 10 to avoid portal hammering
            if p.get("rowid") and p.get("acct"):
                try:
                    summary = await self.get_patient_summary(p["rowid"], p["acct"])
                    summaries.append(summary)
                except Exception as e:
                    logger.warning(f"Failed to get summary for {p.get('name')}: {e}")
                    summaries.append(p)  # Fall back to search result

        return summaries if summaries else patients

    # ------------------------------------------------------------------
    # NEW-PATIENT CREATE
    # ------------------------------------------------------------------
    # Demographic keys accepted by create_patient(). Only last_name +
    # first_name are required; everything else is optional and rendered as an
    # honest blank (never fabricated) when absent. dob/ssn are the two fields
    # the name-search de-dupe step also consumes.
    _CREATE_DEMOGRAPHIC_KEYS = (
        "last_name", "first_name", "mi", "dob", "ssn",
        "sex", "address", "address2", "city", "state", "zip",
        "home_phone", "cell_phone", "work_phone", "email",
    )

    async def create_patient(
        self,
        demographics: dict,
        *,
        dry_run: bool = True,
        confirm_unverified: bool = False,
    ) -> dict:
        """Create a NEW patient chart in Svigg/Dr.Com — DRY-RUN by default.

        SAFETY: with dry_run=True (the DEFAULT) this walks the live entry flow
        to the ADD form, DISCOVERS the add form's real input fields from the
        rendered DOM, and returns the fields it WOULD submit WITHOUT clicking
        Save. It CREATES NOTHING. The commit path (dry_run=False) is
        double-gated (CREATE_EXECUTE_ENABLED + confirm_unverified) and, because
        the SAVE POST contract is UNVERIFIED (not in the HAR), fail-closed: it
        refuses unless it can positively identify a single Save/Add submit
        control, and always tags the response as unverified.

        Flow (all read-only until the final Save on the commit path):
          login (reused; caller must have called login() OR we verify here)
            -> GET /proxy.cgi/off/maint/patientEntry_new.htm   (new-patient form)
            -> fill LastName/FirstName/MI/BirthDate/SocSecNo/Account, rtype=r
            -> POST patientEntry_new.htm  (name-search DE-DUPE — read-only)
                 * if the de-dupe surfaces an existing pentry.htm?rowid= match
                   for the same name+DOB, we STOP and report it (idempotency:
                   never double-create).
            -> GET /proxy.cgi/off/maint/patientEntry_add.htm   (the ADD form)
            -> DISCOVER add-form fields from the DOM, map demographics onto them
            -> [dry_run] STOP + return discovered fields + would-submit payload
            -> [commit]  gated Save (UNVERIFIED — fail-closed)

        Args:
            demographics: dict with at least ``last_name`` + ``first_name``.
                Optional: mi, dob (MM/DD/YYYY), ssn, sex, address, address2,
                city, state, zip, home_phone, cell_phone, work_phone, email.
                Missing optional values render as "" (honest blank) — never
                invented.
            dry_run: True (DEFAULT) => discover + propose, submit nothing.
                False => attempt the gated, unverified Save.
            confirm_unverified: caller's explicit acknowledgement that the Save
                contract is unverified. Required (with the module flag) to ever
                attempt a Save.

        Returns one of:
          {status:"prepared", add_form_url, discovered_fields, would_submit,
              mapped, unmapped_demographics, dedupe, warning}
          {status:"duplicate_suspected", matches, dedupe, ...}
          {status:"execute_blocked", reason, prepared:{…}}
          {status:"save_unverified", reason, submit_control, prepared:{…}}
          {status:"error", stage, error}
        """
        page = self._page

        # ---- Validate the one thing we will NOT fabricate: a name ---------
        demo = {k: (str(demographics.get(k, "")).strip()
                    if demographics.get(k) is not None else "")
                for k in self._CREATE_DEMOGRAPHIC_KEYS}
        if not demo["last_name"] or not demo["first_name"]:
            return {"status": "error", "stage": "validate",
                    "error": "last_name and first_name are required "
                             "(a blank name is never fabricated)"}

        # ---- STAGE 1: open the new-patient entry form ---------------------
        new_url = f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry_new.htm"
        try:
            await self._goto(new_url, wait_until="networkidle", timeout=20000)
        except Exception as exc:
            return {"status": "error", "stage": "new_form",
                    "error": f"could not open patientEntry_new.htm: {exc}"}

        title = await page.title()
        if "Patient Entry" not in title:
            # Most likely the session lapsed — surface it honestly so the
            # manager wrapper can mark the session expired and reconnect.
            return {"status": "error", "stage": "new_form",
                    "error": f"unexpected page title {title!r} at "
                             "patientEntry_new.htm (session may have expired)"}

        # ---- STAGE 2: name-search DE-DUPE (read-only POST) ----------------
        # The new-patient form doubles as a duplicate check: filling the name
        # (+ DOB/SSN when known) and submitting searches existing charts before
        # letting you add. This is our idempotency guard — search FIRST so we
        # never create a second chart for someone already in the system.
        dedupe = {"performed": False, "match_count": 0, "matches": []}
        try:
            # Fill the de-dupe search fields that exist on the form. Field
            # names come from the HAR: LastName, FirstName, MI, BirthDate,
            # SocSecNo, Account. rtype=r is a hidden mode flag on the form.
            field_map = {
                'input[name="LastName"]': demo["last_name"],
                'input[name="FirstName"]': demo["first_name"],
                'input[name="MI"]': demo["mi"],
                'input[name="BirthDate"]': demo["dob"],
                'input[name="SocSecNo"]': demo["ssn"],
            }
            for sel, val in field_map.items():
                if not val:
                    continue
                el = await page.query_selector(sel)
                if el:
                    await page.fill(sel, val)

            # Submit the de-dupe search. The form's own submit performs the
            # name search (rtype=r). Prefer a named search button, else submit
            # the form element directly — either way this is a READ.
            submit_btn = (
                await page.query_selector('input[name="NameSearch"]')
                or await page.query_selector('input[type="submit"]')
            )
            if submit_btn:
                await submit_btn.click()
            else:
                await page.evaluate(
                    'var f=document.forms["patientEntry_new"]||document.forms[0];'
                    'f && f.submit();'
                )
            await page.wait_for_load_state("networkidle", timeout=15000)
            dedupe["performed"] = True

            # Harvest any existing-chart matches (same link shape as
            # search_patient: pentry.htm?rowid=...&acct=...).
            existing = await page.query_selector_all(
                'a[href*="pentry.htm?rowid="]'
            )
            for link in existing:
                href = await link.get_attribute("href") or ""
                name = (await link.inner_text()).strip()
                rowid = ""
                acct = ""
                rm = re.search(r'rowid=([^&|]+)', href)
                am = re.search(r'acct=([^&|]+)', href)
                if rm:
                    rowid = rm.group(1)
                if am:
                    acct = am.group(1)
                dedupe["matches"].append(
                    {"name": name, "rowid": rowid, "acct": acct}
                )
            dedupe["match_count"] = len(dedupe["matches"])
        except Exception as exc:
            # A de-dupe failure is not fatal to a DRY-RUN discovery, but it IS
            # fatal to a commit — we will not create a chart we could not first
            # check for duplicates. Record it and enforce below.
            dedupe["error"] = str(exc)
            logger.warning("Svigg create de-dupe search failed: %s", exc)

        # Idempotency: if the de-dupe found an existing chart, STOP and report
        # it rather than adding a duplicate. (A human can decide from the card.)
        if dedupe["match_count"] > 0:
            return {
                "status": "duplicate_suspected",
                "reason": ("an existing chart matched the name/DOB in the "
                           "de-dupe search — refusing to create a duplicate; "
                           "review the matches and pass an explicit override "
                           "path if this is genuinely a new person"),
                "matches": dedupe["matches"],
                "dedupe": dedupe,
                "name": f'{demo["last_name"]}, {demo["first_name"]}',
            }

        # ---- STAGE 3: open the ADD form ----------------------------------
        add_url = f"{self.BASE_URL}/proxy.cgi/off/maint/patientEntry_add.htm"
        try:
            await self._goto(add_url, wait_until="networkidle", timeout=20000)
        except Exception as exc:
            return {"status": "error", "stage": "add_form",
                    "error": f"could not open patientEntry_add.htm: {exc}"}

        # The add form may live in the main document or a frame. Find whichever
        # context actually hosts input fields.
        add_ctx = page
        try:
            n_inputs = len(await page.query_selector_all(
                'input, select, textarea'))
        except Exception:
            n_inputs = 0
        if n_inputs == 0:
            for frame in page.frames:
                try:
                    if await frame.query_selector('input, select, textarea'):
                        add_ctx = frame
                        break
                except Exception:
                    continue

        # ---- STAGE 4: DISCOVER the add form's real fields from the DOM ----
        # We do NOT hardcode the add-form field names (they are not in the
        # HAR). We read them live so the mapping below is anchored to reality
        # and the dry-run report documents exactly what the form exposes.
        try:
            discovered = await add_ctx.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    const push = (el) => {
                        const name = el.getAttribute('name') || '';
                        if (!name || seen.has(name)) return;
                        seen.add(name);
                        let type = (el.tagName || '').toLowerCase();
                        if (type === 'input') type = el.getAttribute('type') || 'text';
                        const rec = {name, type};
                        if ((el.tagName||'').toLowerCase() === 'select') {
                            rec.options = Array.from(el.options || [])
                                .map(o => o.value).slice(0, 40);
                        }
                        out.push(rec);
                    };
                    document.querySelectorAll('input,select,textarea')
                        .forEach(push);
                    return out;
                }"""
            )
        except Exception as exc:
            return {"status": "error", "stage": "discover_fields",
                    "error": f"could not read add-form fields from DOM: {exc}"}

        if not discovered:
            return {"status": "error", "stage": "discover_fields",
                    "error": "patientEntry_add.htm exposed no input fields — "
                             "the add form did not render (session/nav issue)"}

        discovered_names = {f["name"] for f in discovered}

        # ---- STAGE 5: map demographics onto discovered fields ------------
        # Candidate field-name aliases per demographic. We match against the
        # names the form ACTUALLY exposes (discovered_names), first exact then
        # case-insensitive, and never invent a target. Anything we can't place
        # is reported under ``unmapped_demographics`` for a human to resolve.
        alias_map = {
            "last_name": ["LastName", "lname", "Last", "PatientLastName"],
            "first_name": ["FirstName", "fname", "First", "PatientFirstName"],
            "mi": ["MI", "MiddleInitial", "Middle"],
            "dob": ["BirthDate", "DOB", "DateOfBirth", "Birthdate"],
            "ssn": ["SocSecNo", "SSN", "SocialSecurityNo", "Social"],
            "sex": ["Sex", "Gender"],
            "address": ["Address", "Address1", "Addr", "Street"],
            "address2": ["Address2", "Addr2"],
            "city": ["City"],
            "state": ["State", "St"],
            "zip": ["Zip", "ZipCode", "PostalCode"],
            "home_phone": ["HomePhone", "Home", "Phone", "PhoneHome"],
            "cell_phone": ["CellPhone", "Cell", "Mobile", "PhoneCell"],
            "work_phone": ["WorkPhone", "Work", "PhoneWork"],
            "email": ["Email", "EmailAddress", "EMail"],
        }

        def _resolve(field_names: list[str]) -> str:
            for cand in field_names:
                if cand in discovered_names:
                    return cand
            lower = {n.lower(): n for n in discovered_names}
            for cand in field_names:
                if cand.lower() in lower:
                    return lower[cand.lower()]
            return ""

        mapped: dict[str, str] = {}       # form_field_name -> value
        mapping_trace: dict[str, str] = {} # demographic_key -> form_field_name
        unmapped: list[str] = []
        for demo_key in self._CREATE_DEMOGRAPHIC_KEYS:
            val = demo.get(demo_key, "")
            if not val:
                continue  # honest blank — nothing to place
            target = _resolve(alias_map.get(demo_key, [demo_key]))
            if target:
                mapped[target] = val
                mapping_trace[demo_key] = target
            else:
                unmapped.append(demo_key)

        prepared = {
            "status": "prepared",
            "add_form_url": add_url,
            "discovered_fields": discovered,
            "would_submit": mapped,
            "mapping": mapping_trace,
            "unmapped_demographics": unmapped,
            "dedupe": dedupe,
            "name": f'{demo["last_name"]}, {demo["first_name"]}',
            "warning": ("SAVE contract UNVERIFIED (not in HAR) — nothing "
                        "submitted; no chart created. Fields above were "
                        "DISCOVERED live from the add form DOM."),
        }

        # ================================================================
        # COMMIT GUARD — dry-run default, then two fail-closed locks.
        # ================================================================
        if dry_run:
            return prepared

        # Commit requested. A commit without a successful de-dupe is refused:
        # we will not create a chart we could not first check for duplicates.
        if not dedupe.get("performed"):
            return {"status": "execute_blocked",
                    "reason": ("de-dupe search did not complete — refusing to "
                               "create a chart without a duplicate check"),
                    "dedupe": dedupe, "prepared": prepared}

        if not (CREATE_EXECUTE_ENABLED and confirm_unverified):
            return {
                "status": "execute_blocked",
                "reason": ("new-patient commit is unverified (Save POST not in "
                           "HAR) and disabled by default; requires "
                           "SVIGG_CREATE_EXECUTE=1 in the environment AND "
                           "confirm_unverified=True"),
                "flag_enabled": CREATE_EXECUTE_ENABLED,
                "confirm_unverified": confirm_unverified,
                "prepared": prepared,
            }

        # Both locks open — attempt to positively identify a SINGLE Save/Add
        # submit control. We NEVER guess a submit target: if we cannot find
        # exactly one, we fail closed with the discovered controls for a human
        # to capture the real Save contract (a live HAR of this click).
        try:
            controls = await add_ctx.evaluate(
                """() => Array.from(document.querySelectorAll(
                        'input[type=submit],input[type=image],button'))
                    .map(e => ({
                        name: e.getAttribute('name') || '',
                        value: e.getAttribute('value') || e.getAttribute('alt')
                               || (e.textContent||'').trim(),
                        type: (e.getAttribute('type')||'').toLowerCase()
                    }))"""
            )
        except Exception as exc:
            controls = []
            logger.warning("Svigg create submit-control scan failed: %s", exc)

        save_like = [
            c for c in controls
            if re.search(r'save|add|submit|create',
                         f'{c.get("name","")} {c.get("value","")}', re.I)
        ]

        # Fail-closed: the Save wire contract is unverified. Even with both
        # locks open we STOP here and hand back the identified control so a
        # supervised operator can capture the real Save POST (HAR) before this
        # path is ever wired to actually click. This keeps create honest — it
        # never claims a chart was created without a verified Save.
        return {
            "status": "save_unverified",
            "reason": ("commit locks are open, but the Save POST contract is "
                       "UNVERIFIED (not captured in any HAR). Refusing to "
                       "click an unverified Save. Capture a live HAR of this "
                       "click under supervision, then wire the verified POST."),
            "submit_controls": controls,
            "save_like_controls": save_like,
            "prepared": prepared,
            "warning": ("NO chart created. This is the fail-closed terminus "
                        "for the create-commit path until the Save step is "
                        "verified."),
        }


# ---------------------------------------------------------------------------
# Standalone CLI for testing
# ---------------------------------------------------------------------------

async def _main():
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 svigg_scraper.py <last_name> [first_name]")
        print("       python3 svigg_scraper.py --status")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO)
    scraper = SviggScraper(headless=False)  # visible for debugging

    try:
        await scraper.start()

        if sys.argv[1] == "--status":
            ok = await scraper.login()
            print(json.dumps({"logged_in": ok}))
            return

        last_name = sys.argv[1]
        first_name = sys.argv[2] if len(sys.argv) > 2 else ""

        ok = await scraper.login()
        if not ok:
            print(json.dumps({"error": "Login failed"}))
            return

        results = await scraper.search_and_summarize(last_name, first_name)
        print(json.dumps(results, indent=2, default=str))

    finally:
        await scraper.stop()


if __name__ == "__main__":
    asyncio.run(_main())

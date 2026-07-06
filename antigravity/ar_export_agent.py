#!/usr/bin/env python3
"""
ar_export_agent.py — pull the full AR reports from SIS Complete and
Sunny Vigg (Physician-to-Go), per the navigation the office uses:

  SIS:   profile menu (top right) -> Business Desktop version
         -> Revenue Cycle Management -> Export
  Svigg: Reporting -> Reports -> run each of:
           - Aged trial balance by patient
           - Open balances summary
           - Summary aging by account
           - Patient summary report
         (click report -> Execute Report -> Submit), wait ~2 min,
         then download the finished files from the home screen.

Downloads land in  ~/.gemini/antigravity/scratch/ar_reports/
Debug screenshots in ........................./ar_reports/debug/
Run import_ar_reports.py afterwards to load them into the app.

Sessions:
  - SIS uses the saved Auth0 session (scratch/sis_browser_state.json).
    If expired, run `python3 sis_agent.py` once to refresh (SMS 2FA).
  - Svigg logs in with WEBEDOCTOR_USER / WEBEDOCTOR_PASS from .env.

Usage:
  python3 ar_export_agent.py            # both portals
  python3 ar_export_agent.py sis        # SIS only
  python3 ar_export_agent.py svigg      # Svigg only
  python3 ar_export_agent.py svigg --headed   # watch it work
"""

import asyncio
import os
import sys
from datetime import datetime

SCRATCH = os.environ.get("ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch"))
AR_DIR = os.path.join(SCRATCH, "ar_reports")
DEBUG_DIR = os.path.join(AR_DIR, "debug")
SIS_STATE = os.path.join(SCRATCH, "sis_browser_state.json")

SVIGG_REPORTS = [
    "Aged trial balance by patient",
    "Open balances summary",
    "Summary aging by account",
    "Patient summary report",
]


def _env():
    d = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def _stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


async def _shot(page, label):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, f"{_stamp()}_{label}.png")
    try:
        await page.screenshot(path=path)
        print(f"   [shot] {path}")
    except Exception:
        pass


async def _click_first(page_or_frame, candidates, label, timeout=8000):
    """Try a list of selectors / text locators until one clicks."""
    for sel in candidates:
        try:
            loc = page_or_frame.locator(sel).first
            if await loc.count():
                await loc.click(timeout=timeout)
                print(f"   clicked {label} via {sel!r}")
                return True
        except Exception:
            continue
    print(f"   COULD NOT FIND: {label} (tried {len(candidates)} selectors)")
    return False


async def _save_downloads(downloads, prefix):
    os.makedirs(AR_DIR, exist_ok=True)
    saved = []
    for dl in downloads:
        name = f"{prefix}_{_stamp()}_{dl.suggested_filename}"
        path = os.path.join(AR_DIR, name)
        await dl.save_as(path)
        saved.append(path)
        print(f"   saved {path}")
    return saved


# ─── SIS Complete ─────────────────────────────────────────────────────────────

async def export_sis(pw, headed=False):
    print("== SIS Complete ==")
    if not os.path.exists(SIS_STATE):
        print("   No saved SIS session. Run `python3 sis_agent.py` first (SMS 2FA).")
        return []
    browser = await pw.chromium.launch(headless=not headed)
    ctx = await browser.new_context(storage_state=SIS_STATE, ignore_https_errors=True,
                                    accept_downloads=True)
    page = await ctx.new_page()
    downloads = []
    page.on("download", lambda d: downloads.append(d))
    try:
        await page.goto("https://e03.siscomplete.cloud/mainline/", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        if "login" in page.url.lower() or "auth0" in page.url.lower():
            print("   Saved SIS session EXPIRED -> run `python3 sis_agent.py` to refresh, then retry.")
            await _shot(page, "sis_session_expired")
            return []
        await _shot(page, "sis_landing")

        # 1. Profile menu, top right -> "Business Desktop" version
        await _click_first(page, [
            "[data-test-id*='profile' i]", "[class*='profile' i]",
            "header [class*='avatar' i]", "[class*='user-menu' i]",
            "header button:last-of-type", ".p-menubar .p-menubar-end button",
        ], "profile menu (top right)")
        await page.wait_for_timeout(1500)
        await _shot(page, "sis_profile_menu")
        await _click_first(page, [
            "text=/business\\s*desktop/i", "a:has-text('Business Desktop')",
            "li:has-text('Business Desktop')", "text=/desktop\\s*version/i",
        ], "Business Desktop version")
        await page.wait_for_timeout(4000)
        await _shot(page, "sis_business_desktop")

        # 2. Revenue Cycle Management
        await _click_first(page, [
            "text=/revenue\\s*cycle\\s*management/i", "a:has-text('Revenue Cycle')",
            "li:has-text('Revenue Cycle')", "[title*='Revenue' i]",
        ], "Revenue Cycle Management")
        await page.wait_for_timeout(4000)
        await _shot(page, "sis_rcm")

        # 3. Export
        await _click_first(page, [
            "text=/^\\s*export\\s*$/i", "button:has-text('Export')",
            "a:has-text('Export')", "[title*='Export' i]",
        ], "Export")
        await page.wait_for_timeout(8000)
        await _shot(page, "sis_after_export")

        saved = await _save_downloads(downloads, "SIS_AR")
        if not saved:
            print("   No download captured — check the debug screenshots to refine selectors.")
        return saved
    finally:
        await browser.close()


# ─── Sunny Vigg / Physician-to-Go ────────────────────────────────────────────

async def _svigg_frame(page, selector):
    """Svigg is frame-heavy; find whichever frame contains the selector."""
    for fr in page.frames:
        try:
            if await fr.locator(selector).count():
                return fr
        except Exception:
            continue
    return None


async def export_svigg(pw, headed=False):
    print("== Sunny Vigg (Physician-to-Go) ==")
    E = _env()
    url = E.get("WEBEDOCTOR_URL", "")
    user, pwd = E.get("WEBEDOCTOR_USER", ""), E.get("WEBEDOCTOR_PASS", "")
    if not (url and user and pwd):
        print("   Missing WEBEDOCTOR_URL/USER/PASS in .env")
        return []
    browser = await pw.chromium.launch(headless=not headed)
    ctx = await browser.new_context(ignore_https_errors=True, accept_downloads=True)
    page = await ctx.new_page()
    downloads = []
    page.on("download", lambda d: downloads.append(d))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        await page.fill("input[name='login']", user)
        await page.fill("input[name='password']", pwd)
        await page.click("input[name='Submit']")
        await page.wait_for_timeout(5000)
        title = (await page.title()).lower()
        if "sorry" in title or "operator not found" in (await page.content()).lower():
            print("   LOGIN REJECTED ('Operator Not Found') — the WEBEDOCTOR_PASS in .env "
                  "is stale or this account lacks portal access. Update .env and retry.")
            await _shot(page, "svigg_login_rejected")
            return []
        await _shot(page, "svigg_home")

        # Reporting -> Reports
        for label, cands in [
            ("Reporting", ["text=/^\\s*reporting\\s*$/i", "a:has-text('Reporting')"]),
            ("Reports", ["text=/^\\s*reports\\s*$/i", "a:has-text('Reports')"]),
        ]:
            fr = await _svigg_frame(page, cands[0]) or page
            await _click_first(fr, cands, label)
            await page.wait_for_timeout(3000)
        await _shot(page, "svigg_reports_list")

        # Queue all four reports
        for rep in SVIGG_REPORTS:
            rep_pattern = rep.replace(" ", "\\s*")
            sel = f"text=/{rep_pattern}/i"
            fr = await _svigg_frame(page, sel) or page
            ok = await _click_first(fr, [sel, f"a:has-text('{rep}')"], rep)
            if not ok:
                continue
            await page.wait_for_timeout(2500)
            fr2 = await _svigg_frame(page, "text=/execute\\s*report/i") or page
            await _click_first(fr2, ["text=/execute\\s*report/i",
                                     "input[value*='Execute' i]",
                                     "button:has-text('Execute')"], f"{rep}: Execute Report")
            await page.wait_for_timeout(2500)
            fr3 = await _svigg_frame(page, "text=/^\\s*submit\\s*$/i") or page
            await _click_first(fr3, ["input[value*='Submit' i]", "button:has-text('Submit')",
                                     "text=/^\\s*submit\\s*$/i"], f"{rep}: Submit")
            await page.wait_for_timeout(2500)
            await _shot(page, f"svigg_queued_{rep[:18].replace(' ', '_')}")
            # back to the reports list for the next one
            fr = await _svigg_frame(page, "text=/^\\s*reports\\s*$/i")
            if fr:
                await _click_first(fr, ["text=/^\\s*reports\\s*$/i"], "back to Reports")
                await page.wait_for_timeout(2000)

        # The portal takes ~2 minutes to generate; wait, then collect from home.
        print("   Waiting 150s for the portal to generate the reports…")
        await page.wait_for_timeout(150000)
        fr = await _svigg_frame(page, "text=/^\\s*home\\s*$/i") or page
        await _click_first(fr, ["text=/^\\s*home\\s*$/i", "a:has-text('Home')"], "Home")
        await page.wait_for_timeout(4000)
        await _shot(page, "svigg_home_after_wait")

        # Download every finished-report link on the home screen
        for rep in SVIGG_REPORTS:
            sel = f"a:has-text('{rep.split()[0]}')"
            fr = await _svigg_frame(page, sel)
            if not fr:
                continue
            links = await fr.locator(sel).all()
            for ln in links[:3]:
                try:
                    async with page.expect_download(timeout=20000) as dl_info:
                        await ln.click()
                    downloads.append(await dl_info.value)
                except Exception:
                    pass

        saved = await _save_downloads(downloads, "SVIGG")
        if not saved:
            print("   No downloads captured — check debug screenshots; the home-screen "
                  "link layout may need a selector tweak.")
        return saved
    finally:
        await browser.close()


async def main():
    from playwright.async_api import async_playwright
    targets = [a for a in sys.argv[1:] if not a.startswith("-")] or ["sis", "svigg"]
    headed = "--headed" in sys.argv
    os.makedirs(AR_DIR, exist_ok=True)
    async with async_playwright() as pw:
        if "sis" in targets:
            await export_sis(pw, headed=headed)
        if "svigg" in targets:
            await export_svigg(pw, headed=headed)
    print(f"\nDone. Files (if any) are in {AR_DIR} — run `python3 import_ar_reports.py` next.")


if __name__ == "__main__":
    asyncio.run(main())

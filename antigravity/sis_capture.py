#!/usr/bin/env python3
"""
sis_capture.py — Capture a SIS Complete Facesheet/Cases page for parser development.

This does NOT log in with a password. It reuses the Auth0 session that
sis_agent.py already saved (scratch/sis_browser_state.json, valid ~30 days).
It opens the given Facesheet URL and saves the raw HTML to scratch/ so the
billing/payment parser can be built accurately against real data.

USAGE:
  python3 sis_capture.py                 # captures the default case (131)
  python3 sis_capture.py 131 145 160     # capture several case IDs

If it lands on a login page, the saved session expired — run sis_agent.py once
(with SIS_USERNAME/SIS_PASSWORD set in .env) to refresh it, then re-run this.
"""
import asyncio
import os
import sys

SCRATCH_DIR = os.environ.get("ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch"))
BROWSER_STATE = os.path.join(SCRATCH_DIR, "sis_browser_state.json")
BASE = "https://e03.siscomplete.cloud/mainline/Gemini/Facesheet"


async def capture(case_ids):
    from playwright.async_api import async_playwright
    if not os.path.exists(BROWSER_STATE):
        sys.exit(
            f"No saved SIS session at {BROWSER_STATE}.\n"
            "Run sis_agent.py once (SIS creds in .env) to establish it, then retry."
        )
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=BROWSER_STATE, ignore_https_errors=True)
        page = await ctx.new_page()
        for cid in case_ids:
            url = f"{BASE}/{cid}/Cases"
            print(f"→ {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(2500)
                if "login" in page.url.lower() or "auth0" in page.url.lower():
                    print("  ⚠ redirected to login — saved session expired. Run sis_agent.py to refresh.")
                    break
                html = await page.content()
                out = os.path.join(SCRATCH_DIR, f"sis_facesheet_{cid}.html")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(html)
                await page.screenshot(path=os.path.join(SCRATCH_DIR, f"sis_facesheet_{cid}.png"))
                print(f"  saved {out} ({len(html):,} bytes)")
            except Exception as e:  # noqa: BLE001
                print(f"  FAILED: {e}")
        await browser.close()
    print("\nDone. Tell me the saved file path(s) and I'll build the exact payment parser.")


if __name__ == "__main__":
    ids = sys.argv[1:] or ["131"]
    asyncio.run(capture(ids))

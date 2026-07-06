#!/usr/bin/env python3
"""Standalone DRY-RUN harness for the Svigg/Dr.Com new-patient create flow.

WHAT THIS DOES
--------------
Runs SviggScraper.create_patient(..., dry_run=True) against the LIVE Svigg
portal for a TEST patient and prints the fields it DISCOVERED on the add form
plus what it WOULD submit. It NEVER clicks Save — dry_run=True is hard-wired
here and there is no flag to flip it. Nothing is written to the EMR.

WHY IT EXISTS
-------------
The 2026-07-03 HAR captured the ENTRY path (new-patient form -> name-search
de-dupe -> add form) but NOT the add-form's own fields or the final Save POST.
This harness lets a human, under supervision, hit the real add form once and
capture:
  * the exact input/select field NAMES the add form exposes, and
  * how our demographic->field mapping lands on them.
Use its output to (a) confirm the mapping and (b) capture the Save POST (open
the browser devtools Network tab, or run with --headful and Save manually) so
the commit path can later be verified and wired.

SAFETY
------
* dry_run is FORCED True; this harness cannot submit.
* It refuses to run without --i-understand-this-is-live (it touches the live
  portal and requires valid WEBEDOCTOR_* creds in .env).
* Default demographics are the DESIGNATED TEST record name (Test, Patient) so a
  supervised run never de-dupes against or touches a real person. Override only
  with care.
* PHI: it prints the demographics YOU pass and the DISCOVERED FIELD NAMES
  (structure, not other patients' data). Do not point it at a real patient.

USAGE
-----
    python3 scripts/dryrun_create_patient.py --i-understand-this-is-live
    python3 scripts/dryrun_create_patient.py --i-understand-this-is-live \
        --last Test --first Patient --dob 01/01/1990 --headful

Exit code 0 = a dry-run result was produced (status prepared /
duplicate_suspected / execute_blocked etc.); 1 = harness/precondition error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# --- import the scraper from the sibling integrations package --------------
_INTEGRATIONS = Path(__file__).resolve().parent.parent / "python" / "integrations"
if str(_INTEGRATIONS) not in sys.path:
    sys.path.insert(0, str(_INTEGRATIONS))


def _redact(fields: dict) -> dict:
    """Never print raw SSN even for a test record; show only presence."""
    out = dict(fields or {})
    for k in list(out):
        if "soc" in k.lower() or k.lower() in ("ssn", "socsecno"):
            out[k] = "***set***" if out[k] else ""
    return out


async def _run(args) -> int:
    try:
        from svigg_scraper import SviggScraper  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - env dependent
        print(json.dumps({"error": f"cannot import SviggScraper: {exc}"}))
        return 1

    demographics = {
        "last_name": args.last,
        "first_name": args.first,
        "mi": args.mi,
        "dob": args.dob,
        # SSN intentionally left blank by default — do not seed a real SSN.
        "ssn": args.ssn,
    }

    scraper = SviggScraper(headless=not args.headful)
    try:
        await scraper.start()
        ok = await scraper.login()
        if not ok:
            print(json.dumps({
                "error": "Svigg login failed — check WEBEDOCTOR_USER/PASS in .env",
            }))
            return 1

        # dry_run is FORCED True. This harness cannot submit, by construction.
        result = await scraper.create_patient(demographics, dry_run=True)
    except Exception as exc:
        print(json.dumps({"error": f"dry-run raised: {exc}"}))
        return 1
    finally:
        await scraper.stop()

    # Print a readable, PHI-conscious summary.
    status = result.get("status") if isinstance(result, dict) else "?"
    print("=" * 70)
    print(f"DRY-RUN create_patient status: {status}")
    print("=" * 70)

    if isinstance(result, dict):
        dedupe = result.get("dedupe") or {}
        print(f"de-dupe performed : {dedupe.get('performed')}")
        print(f"de-dupe matches   : {dedupe.get('match_count')}")

        discovered = result.get("discovered_fields")
        if discovered:
            print(f"\nDISCOVERED add-form fields ({len(discovered)}):")
            for f in discovered:
                extra = ""
                if f.get("options"):
                    extra = f"  options={f['options'][:8]}"
                print(f"  - name={f.get('name'):<24} type={f.get('type')}{extra}")

        if result.get("mapping"):
            print("\nDEMOGRAPHIC -> add-form field mapping:")
            for demo_key, field in result["mapping"].items():
                print(f"  {demo_key:<12} -> {field}")

        if result.get("unmapped_demographics"):
            print("\nUNMAPPED demographics (no add-form field found):")
            print(f"  {result['unmapped_demographics']}")

        if result.get("would_submit"):
            print("\nWOULD SUBMIT (nothing was sent):")
            print(json.dumps(_redact(result["would_submit"]), indent=2))

        if result.get("warning"):
            print(f"\nWARNING: {result['warning']}")

    print("\n--- raw result (SSN redacted) ---")
    safe = dict(result) if isinstance(result, dict) else {"raw": result}
    if isinstance(safe.get("would_submit"), dict):
        safe["would_submit"] = _redact(safe["would_submit"])
    print(json.dumps(safe, indent=2, default=str))

    print("\nNEXT STEP: the Save POST is UNVERIFIED. To capture it, re-run with "
          "--headful, open browser devtools -> Network, and Save the test "
          "record manually; record the POST URL + form fields, then wire the "
          "verified contract into create_patient's commit path.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--i-understand-this-is-live", action="store_true",
                   dest="live",
                   help="required: this hits the LIVE Svigg portal (read-only "
                        "dry-run; no Save)")
    p.add_argument("--last", default="Test", help="test last name (default: Test)")
    p.add_argument("--first", default="Patient",
                   help="test first name (default: Patient)")
    p.add_argument("--mi", default="", help="test middle initial")
    p.add_argument("--dob", default="", help="test DOB MM/DD/YYYY")
    p.add_argument("--ssn", default="",
                   help="test SSN (leave blank; do NOT seed a real SSN)")
    p.add_argument("--headful", action="store_true",
                   help="run with a visible browser (for manual Save capture)")
    args = p.parse_args()

    if not args.live:
        print("Refusing to run: this harness hits the LIVE Svigg portal.\n"
              "Re-run with --i-understand-this-is-live once you are supervised "
              "and WEBEDOCTOR_* creds are set in .env.", file=sys.stderr)
        return 1

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

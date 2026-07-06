#!/usr/bin/env python3
"""
phone_intake_sync.py — Sync the AI Phone Intake portal into patient_database.json

The AI Phone Intake app (live phone agent) is a Basic-Auth HTML portal:
  GET  {PHONE_INTAKE_URL}/patients          -> index listing /patients/{id}
  GET  {PHONE_INTAKE_URL}/patients/{id}     -> profile (name, dob, phone, call timeline, cases)

This agent logs in WITH YOUR credentials (read from .env — never hardcoded),
scrapes each patient profile, and merges the data into the local
patient_database.json that the dashboard reads.

SETUP (you do this — the agent never stores or sees your password except via .env):
  1. Rotate the portal password (the old one was shared in chat).
  2. Add to a local .env file next to this script:
        PHONE_INTAKE_URL=https://ai-phone-intake-aimedicalcoach.azurewebsites.net
        PHONE_INTAKE_USER=officeadmin
        PHONE_INTAKE_PASS=<your-new-password>
     (.env is already gitignored — it will NOT be committed.)
  3. Run once to verify:   python3 phone_intake_sync.py --limit 5 --dry-run
  4. Real run:             python3 phone_intake_sync.py
  5. Schedule (after verifying) with cron/launchd, e.g. every 30 min.

PHI NOTE: this pulls live patient-conversation data into a local file. Confirm your
HIPAA/BAA posture before running on real data. Raw HTML is cached under scratch/
phone_intake_cache/ for debugging — treat it as PHI (it's under the gitignored scratch/).
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

SCRATCH_DIR = os.environ.get("ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch"))
DB_PATH = os.path.join(SCRATCH_DIR, "patient_database.json")
CACHE_DIR = os.path.join(SCRATCH_DIR, "phone_intake_cache")

PLACEHOLDER_VALUES = {"", "your-new-password", "changeme", "replace_me", "password"}


def _load_dotenv(path=".env"):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _config():
    _load_dotenv()
    url = os.environ.get("PHONE_INTAKE_URL", "").strip().rstrip("/")
    user = os.environ.get("PHONE_INTAKE_USER", "").strip()
    pw = os.environ.get("PHONE_INTAKE_PASS", "").strip()
    if not url or not user or pw.lower() in PLACEHOLDER_VALUES:
        sys.exit(
            "ERROR: PHONE_INTAKE_URL / PHONE_INTAKE_USER / PHONE_INTAKE_PASS are not "
            "all set in .env (and PASS must not be a placeholder). See setup notes at "
            "the top of this file. Refusing to run."
        )
    return url, user, pw


def _fetch(url, user, pw, timeout=30):
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "User-Agent": "Antigravity-PhoneIntakeSync/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _val(html, field):
    """Extract <input name="field" value="...">."""
    m = re.search(rf'<input[^>]*name="{re.escape(field)}"[^>]*value="([^"]*)"', html)
    return (m.group(1).strip() if m else "")


def _strip_tags(s):
    return re.sub(r"<[^>]+>", " ", s).replace("&#39;", "'").replace("&amp;", "&")


def parse_profile(html, pid):
    """Parse one /patients/{id} profile page into a record."""
    name = _val(html, "name")
    if not name:
        m = re.search(r"<h1>([^<]+)", html)
        name = m.group(1).strip() if m else f"Patient {pid}"
    dob = _val(html, "date_of_birth")
    phone = _val(html, "phone_number")

    # Call timeline / staff notes — the high-value conversation data
    notes = []
    for m in re.finditer(r"<strong>Staff note:</strong>\s*([^<]{3,400})", html):
        notes.append("Staff note: " + re.sub(r"\s+", " ", m.group(1)).strip())
    for m in re.finditer(r"<h3>(Inbound Call|Outbound Call|Calendar Appointment)[^<]*</h3>", html):
        notes.append(re.sub(r"\s+", " ", _strip_tags(m.group(0))).strip())

    insurance = _val(html, "insurance")
    claim = _val(html, "claim_number")

    rec = {
        "phoneIntakeId": pid,
        "name": name,
        "source": "AI Phone Intake",
        "phoneIntakeSyncedAt": datetime.now().isoformat(timespec="seconds"),
    }
    if dob:
        rec["dob"] = dob
    if phone:
        rec["phone"] = phone
    if insurance:
        rec["insurance"] = insurance
    if claim:
        rec["claimNumber"] = claim
    if notes:
        rec["notes_summaries"] = notes
    return rec


def merge(db, rec):
    """Merge one scraped record into the db list (match by phoneIntakeId, then name)."""
    for p in db:
        if rec.get("phoneIntakeId") and p.get("phoneIntakeId") == rec["phoneIntakeId"]:
            p.update(rec)
            return "updated"
    for p in db:
        if p.get("name", "").strip().lower() == rec["name"].strip().lower():
            p.update(rec)
            return "updated"
    db.append(rec)
    return "added"


def main():
    ap = argparse.ArgumentParser(description="Sync AI Phone Intake portal -> patient_database.json")
    ap.add_argument("--limit", type=int, default=0, help="Only sync the first N patients (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="Parse but do not write patient_database.json")
    ap.add_argument("--no-cache", action="store_true", help="Do not save raw HTML to scratch cache")
    args = ap.parse_args()

    url, user, pw = _config()
    os.makedirs(CACHE_DIR, exist_ok=True)
    print(f"[{datetime.now():%H:%M:%S}] Fetching patient index from {url}/patients ...")
    try:
        index_html = _fetch(f"{url}/patients", user, pw)
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: portal returned HTTP {e.code} (check credentials / VPN).")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: could not reach portal: {e.reason}")

    pids = []
    for m in re.finditer(r"/patients/(\d+)", index_html):
        if m.group(1) not in pids:
            pids.append(m.group(1))
    print(f"  found {len(pids)} patient profiles.")
    if args.limit:
        pids = pids[: args.limit]

    db = json.load(open(DB_PATH, encoding="utf-8")) if os.path.exists(DB_PATH) else []
    added = updated = failed = 0
    for i, pid in enumerate(pids, 1):
        try:
            html = _fetch(f"{url}/patients/{pid}", user, pw)
            if not args.no_cache:
                with open(os.path.join(CACHE_DIR, f"patient_{pid}.html"), "w", encoding="utf-8") as f:
                    f.write(html)
            rec = parse_profile(html, pid)
            result = merge(db, rec)
            added += result == "added"
            updated += result == "updated"
            print(f"  [{i}/{len(pids)}] {rec['name']:<28} {result}")
            time.sleep(0.3)  # be gentle on the portal
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [{i}/{len(pids)}] id={pid} FAILED: {e}")

    print(f"\nSummary: {added} added, {updated} updated, {failed} failed.")
    if args.dry_run:
        print("--dry-run: patient_database.json NOT modified.")
    else:
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2)
        print(f"Wrote {len(db)} patients to {DB_PATH}")


if __name__ == "__main__":
    main()

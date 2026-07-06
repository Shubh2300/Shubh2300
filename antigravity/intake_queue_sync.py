#!/usr/bin/env python3
"""
Intake queue bridge.

Polls the Google Sheet that the Apps Script email-intake pipeline writes to
(patient records/billing requests, booking requests, fax queue, follow-ups,
and the automation run log) and mirrors it to a local JSON file the dashboard
API can serve. Read-only against the Sheet; never invents data — if a tab is
missing or the sheet cannot be read, that is reflected honestly (empty list
or a hard failure with no file written), never guessed.

Usage:
    python3 intake_queue_sync.py

Output:
    ~/.gemini/antigravity/scratch/intake_queue.json
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

SERVICE_ACCOUNT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "service_account.json"
)
SERVICE_ACCOUNT_EMAIL = "antigravity-bot@gmail-filterer-496919.iam.gserviceaccount.com"
SPREADSHEET_ID = "1-d4dEC6mLa0pMEN_hcuDqrN08dAG9ZoG4RTwZmuqheY"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

SCRATCH_DIR = os.environ.get(
    "ANTIGRAVITY_SCRATCH_DIR",
    os.path.expanduser("~/.gemini/antigravity/scratch"),
)
OUTPUT_PATH = os.path.join(SCRATCH_DIR, "intake_queue.json")

TABS = {
    "records": "Records & Billing Requests",
    "bookings": "Booking Requests",
    "faxes": "Fax Queue",
    "followups": "Follow-Ups",
}
RUNS_TAB = "Runs"
ROW_CAP = 200
REVIEW_WINDOW_DAYS = 7


def _get_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _rows_to_dicts(rows):
    """First row = headers; remaining rows -> list of dicts, newest-first, capped."""
    if not rows:
        return []
    headers = [str(h).strip() for h in rows[0]]
    out = []
    for raw in rows[1:]:
        item = {}
        for i, header in enumerate(headers):
            if not header:
                continue
            item[header] = raw[i] if i < len(raw) else ""
        out.append(item)
    out.reverse()  # newest-first (sheet rows are appended chronologically)
    return out[:ROW_CAP]


def _fetch_tab(service, tab_name):
    """Returns list-of-dicts for a tab, or [] if the tab doesn't exist."""
    try:
        resp = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=SPREADSHEET_ID, range=f"'{tab_name}'!A:ZZ")
            .execute()
        )
    except Exception as exc:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError) and getattr(exc.resp, "status", None) in (400, 404):
            # Tab does not exist in this spreadsheet — tolerate gracefully.
            return []
        raise
    return _rows_to_dicts(resp.get("values", []))


def _parse_iso(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            if fmt is None:
                iso = text.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso)
            else:
                dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _fetch_review_queue(service):
    """Rows from the Runs tab with status == 'review' from the last 7 days."""
    rows = _fetch_tab(service, RUNS_TAB)
    if not rows:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=REVIEW_WINDOW_DAYS)
    out = []
    for row in rows:
        status = str(row.get("status", "") or row.get("Status", "")).strip().lower()
        if status != "review":
            continue
        ts_value = None
        for v in row.values():
            ts_value = v
            break
        parsed = _parse_iso(ts_value)
        if parsed is not None and parsed < cutoff:
            continue
        out.append(row)
    return out[:ROW_CAP]


def main():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(
            f"ERROR: service account key not found at {SERVICE_ACCOUNT_FILE}. "
            "No file written.",
            file=sys.stderr,
        )
        return 1

    try:
        service = _get_service()
    except ImportError as exc:
        print(
            f"ERROR: google-auth / google-api-python-client not installed: {exc}. "
            "No file written.",
            file=sys.stderr,
        )
        return 1

    from googleapiclient.errors import HttpError

    try:
        results = {key: _fetch_tab(service, tab_name) for key, tab_name in TABS.items()}
        review = _fetch_review_queue(service)
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        if status in (403, 404):
            print(
                f"Share the log sheet (readonly) with {SERVICE_ACCOUNT_EMAIL} then rerun.",
                file=sys.stderr,
            )
            return 1
        print(f"ERROR: Google Sheets API request failed: {exc}. No file written.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: unexpected failure reading sheet: {exc}. No file written.", file=sys.stderr)
        return 1

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourceSheet": SPREADSHEET_ID,
        "records": results["records"],
        "bookings": results["bookings"],
        "faxes": results["faxes"],
        "followups": results["followups"],
        "review": review,
    }

    counts = ", ".join(
        f"{key}={len(payload[key])}"
        for key in ("records", "bookings", "faxes", "followups", "review")
    )

    os.makedirs(SCRATCH_DIR, exist_ok=True)
    tmp_path = OUTPUT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, OUTPUT_PATH)

    print(f"intake_queue_sync: {counts} -> {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
ringcentral_sync.py — pull RingCentral call logs (+ AI summaries when
available) and file them onto the matching patient record in real time.

Architecture (v1 = polling; webhooks need a public HTTPS endpoint, which a
local machine doesn't have — when the app moves to a server, switch to the
Subscription API for true push):

    RingCentral Call Log API ──┐
    RingSense AI insights ─────┼──> match caller/callee phone number to a
                               │    patient in patient_database.json
                               └──> append to patient["calls"] + calls.json

Auth: OAuth 2.0 JWT credentials flow (server-to-server, no browser login).
Setup steps live in RINGCENTRAL_SETUP.md. Credentials come from .env:

    RC_SERVER_URL=https://platform.ringcentral.com
    RC_CLIENT_ID=...
    RC_CLIENT_SECRET=...
    RC_JWT=...

Usage:
    python3 ringcentral_sync.py            # one sync pass (last 24h)
    python3 ringcentral_sync.py --hours 72 # wider window
    python3 ringcentral_sync.py --loop     # poll every 5 minutes
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = os.path.expanduser("~/.gemini/antigravity/scratch")
PATIENT_DB = os.path.join(SCRATCH_DIR, "patient_database.json")
CALLS_JSON = os.path.join(SCRATCH_DIR, "calls.json")
STATE_PATH = os.path.join(SCRATCH_DIR, "ringcentral_sync_state.json")


def _load_dotenv(path=".env"):
    env_path = os.path.join(WORKSPACE_DIR, path)
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


_load_dotenv()

RC_SERVER = os.environ.get("RC_SERVER_URL", "https://platform.ringcentral.com").rstrip("/")
RC_CLIENT_ID = os.environ.get("RC_CLIENT_ID", "")
RC_CLIENT_SECRET = os.environ.get("RC_CLIENT_SECRET", "")
RC_JWT = os.environ.get("RC_JWT", "")


def _http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_access_token():
    """OAuth 2.0 JWT credentials flow (RFC 7523)."""
    if not (RC_CLIENT_ID and RC_CLIENT_SECRET and RC_JWT):
        sys.exit("Missing RC_CLIENT_ID / RC_CLIENT_SECRET / RC_JWT in .env — see RINGCENTRAL_SETUP.md")
    basic = base64.b64encode(f"{RC_CLIENT_ID}:{RC_CLIENT_SECRET}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": RC_JWT,
    }).encode()
    tok = _http("POST", f"{RC_SERVER}/restapi/oauth/token",
                headers={"Authorization": f"Basic {basic}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data=body)
    return tok["access_token"]


def fetch_call_log(token, hours=24):
    """Company-wide detailed call log for the lookback window."""
    date_from = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    records, page = [], 1
    while True:
        qs = urllib.parse.urlencode({
            "dateFrom": date_from, "view": "Detailed",
            "perPage": 250, "page": page,
        })
        data = _http("GET", f"{RC_SERVER}/restapi/v1.0/account/~/call-log?{qs}",
                     headers={"Authorization": f"Bearer {token}"})
        records.extend(data.get("records", []))
        paging = data.get("paging", {})
        if page >= paging.get("totalPages", 1):
            break
        page += 1
    return records


def fetch_ringsense_summary(token, telephony_session_id):
    """AI summary for a call via the RingSense API. Returns '' when the
    account/plan has no RingSense data for this call. NOTE: endpoint shape
    may need adjusting to your account's RingSense product — verify against
    https://developers.ringcentral.com/ringsense-api once credentials exist."""
    try:
        url = (f"{RC_SERVER}/ai/ringsense/v1/public/accounts/~/domains/pbx/"
               f"records/{telephony_session_id}/insights")
        data = _http("GET", url, headers={"Authorization": f"Bearer {token}"})
        insights = data.get("insights", {})
        summary = insights.get("summary") or insights.get("aiSummary") or ""
        if isinstance(summary, dict):
            summary = summary.get("text", "")
        return str(summary)
    except Exception:
        return ""


def _norm_phone(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def build_phone_index(patients):
    idx = {}
    for p in patients:
        n = _norm_phone(p.get("phone"))
        if n:
            idx.setdefault(n, p)
    return idx


def sync_once(hours=24):
    token = get_access_token()
    records = fetch_call_log(token, hours=hours)
    print(f"Fetched {len(records)} call-log records (last {hours}h).")

    patients = json.load(open(PATIENT_DB)) if os.path.exists(PATIENT_DB) else []
    phone_idx = build_phone_index(patients)
    calls = json.load(open(CALLS_JSON)) if os.path.exists(CALLS_JSON) else []
    known_ids = {c.get("call_id") for c in calls}
    state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}

    matched = unmatched = skipped = 0
    for rec in records:
        rc_id = rec.get("id")
        if not rc_id or rc_id in known_ids:
            skipped += 1
            continue
        direction = rec.get("direction", "")
        other = rec.get("from" if direction == "Inbound" else "to", {}) or {}
        other_num = _norm_phone(other.get("phoneNumber"))
        patient = phone_idx.get(other_num)

        summary = ""
        tsid = rec.get("telephonySessionId")
        if tsid:
            summary = fetch_ringsense_summary(token, tsid)

        start = rec.get("startTime", "")
        entry = {
            "call_id": rc_id,
            "source": "ringcentral",
            "direction": direction,
            "date": start[:10],
            "time": start[11:16],
            "caller_name": other.get("name", "") or "Unknown caller",
            "caller_phone": other.get("phoneNumber", ""),
            "patient_name": patient.get("name") if patient else "Not matched",
            "duration_sec": rec.get("duration", 0),
            "result": rec.get("result", ""),
            "summary": summary or "No AI summary available for this call.",
            "outcome": "Unlabeled",
        }
        calls.append(entry)
        known_ids.add(rc_id)

        if patient is not None:
            patient.setdefault("calls", []).append({
                "date": entry["date"], "time": entry["time"],
                "direction": direction, "duration_sec": entry["duration_sec"],
                "result": entry["result"], "summary": entry["summary"],
                "source": "ringcentral", "call_id": rc_id,
            })
            note = f"[RingCentral {entry['date']} {entry['time']}] {direction} call ({entry['result']})."
            if summary:
                note += f" AI summary: {summary[:400]}"
            patient.setdefault("notes_summaries", []).append(note)
            matched += 1
        else:
            unmatched += 1

    json.dump(calls, open(CALLS_JSON, "w"), indent=2)
    json.dump(patients, open(PATIENT_DB, "w"), indent=2)
    state["lastSyncAt"] = datetime.now().isoformat()
    state["lastMatched"] = matched
    state["lastUnmatched"] = unmatched
    json.dump(state, open(STATE_PATH, "w"), indent=2)
    print(f"Filed {matched} calls onto patient records; {unmatched} had no phone match; {skipped} already synced.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--loop", action="store_true", help="poll every 5 minutes")
    args = ap.parse_args()
    if args.loop:
        while True:
            try:
                sync_once(hours=args.hours)
            except Exception as e:
                print(f"Sync error: {e}")
            time.sleep(300)
    else:
        sync_once(hours=args.hours)

#!/usr/bin/env python3
"""
ringcentral_fax_sync.py — pull inbound RingCentral faxes, download attachments,
extract best-effort text, classify/summarize, match to patients, and store the
daily fax digest in SQLite.

Credentials are read from .env:
  RC_SERVER_URL=https://platform.ringcentral.com
  RC_CLIENT_ID=...
  RC_CLIENT_SECRET=...
  RC_JWT=...

Usage:
  python3 ringcentral_fax_sync.py
  python3 ringcentral_fax_sync.py --days 7
  python3 ringcentral_fax_sync.py --dry-run
"""

import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = os.environ.get("ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch"))
PATIENT_DB = os.path.join(SCRATCH_DIR, "patient_database.json")
TASKS_JSON = os.path.join(SCRATCH_DIR, "team_tasks.json")
FAX_DIR = os.path.join(SCRATCH_DIR, "faxes")
FAX_DB = os.path.join(SCRATCH_DIR, "fax_digest.sqlite3")


def load_dotenv(path=".env"):
    env_path = os.path.join(WORKSPACE_DIR, path)
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() and key.strip() not in os.environ:
                os.environ[key.strip()] = value.strip().strip('"').strip("'")


load_dotenv()
RC_SERVER = os.environ.get("RC_SERVER_URL", "https://platform.ringcentral.com").rstrip("/")
RC_CLIENT_ID = os.environ.get("RC_CLIENT_ID", "")
RC_CLIENT_SECRET = os.environ.get("RC_CLIENT_SECRET", "")
RC_JWT = os.environ.get("RC_JWT", "")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def db():
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    conn = sqlite3.connect(FAX_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS faxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT UNIQUE NOT NULL,
            received_at TEXT,
            sender TEXT,
            recipient TEXT,
            subject TEXT,
            attachment_path TEXT,
            attachment_mime TEXT,
            extracted_text TEXT,
            fax_type TEXT,
            priority TEXT,
            summary TEXT,
            action_needed TEXT,
            suggested_owner TEXT,
            patient_name TEXT,
            match_confidence TEXT,
            match_reason TEXT,
            status TEXT DEFAULT 'review',
            task_id TEXT,
            timeline_filed INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fax_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fax_message_id TEXT NOT NULL,
            actor TEXT,
            action TEXT,
            detail TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def http_json(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_bytes(method, url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def get_access_token():
    if not (RC_CLIENT_ID and RC_CLIENT_SECRET and RC_JWT):
        raise RuntimeError("Missing RC_CLIENT_ID / RC_CLIENT_SECRET / RC_JWT in .env")
    basic = base64.b64encode(f"{RC_CLIENT_ID}:{RC_CLIENT_SECRET}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": RC_JWT,
    }).encode()
    tok = http_json(
        "POST",
        f"{RC_SERVER}/restapi/oauth/token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    return tok["access_token"]


def fetch_inbound_faxes(token, days=1):
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    records, page = [], 1
    while True:
        qs = urllib.parse.urlencode({
            "direction": "Inbound",
            "messageType": "Fax",
            "dateFrom": date_from,
            "perPage": 100,
            "page": page,
        })
        data = http_json(
            "GET",
            f"{RC_SERVER}/restapi/v1.0/account/~/extension/~/message-store?{qs}",
            headers={"Authorization": f"Bearer {token}"},
        )
        records.extend(data.get("records", []))
        paging = data.get("paging", {})
        if page >= paging.get("totalPages", 1):
            break
        page += 1
    return records


def safe_filename(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("_")
    return value[:140] or "fax"


def content_ext(content_type, fallback="pdf"):
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return "pdf"
    if "tiff" in ct or "tif" in ct:
        return "tif"
    if "png" in ct:
        return "png"
    if "jpeg" in ct or "jpg" in ct:
        return "jpg"
    return fallback


def download_first_attachment(token, record):
    attachments = record.get("attachments") or []
    if not attachments:
        return "", ""
    att = attachments[0]
    uri = att.get("uri") or att.get("contentUri")
    if not uri:
        return "", ""
    url = uri if str(uri).startswith("http") else f"{RC_SERVER}{uri}"
    raw, mime = http_bytes("GET", url, headers={"Authorization": f"Bearer {token}"})
    received = (record.get("creationTime") or record.get("lastModifiedTime") or now_iso())[:10]
    out_dir = os.path.join(FAX_DIR, received)
    os.makedirs(out_dir, exist_ok=True)
    ext = content_ext(mime, safe_filename(att.get("fileName", "fax.pdf")).split(".")[-1])
    out_path = os.path.join(out_dir, f"{safe_filename(record.get('id'))}.{ext}")
    with open(out_path, "wb") as f:
        f.write(raw)
    return out_path, mime


def direct_extract(path):
    if not path or not os.path.exists(path):
        return ""
    if shutil.which("pdftotext") and path.lower().endswith(".pdf"):
        try:
            out = subprocess.check_output(["pdftotext", "-layout", path, "-"], stderr=subprocess.DEVNULL, timeout=20)
            text = out.decode("utf-8", errors="replace").strip()
            if len(text) > 30:
                return text
        except Exception:
            pass
    try:
        raw = open(path, "rb").read()
        text = raw.decode("latin-1", errors="ignore")
        text = re.sub(r"[^A-Za-z0-9.,;:/$@#%+()\\-\\n\\r\\t ]+", " ", text)
        text = re.sub(r"[ \\t]{2,}", " ", text)
        chunks = [line.strip() for line in text.splitlines() if len(line.strip()) > 8]
        return "\n".join(chunks[:240]).strip()
    except Exception:
        return ""


def ocr_extract(path):
    if not path or not os.path.exists(path) or not shutil.which("tesseract"):
        return ""
    try:
        out = subprocess.check_output(["tesseract", path, "stdout"], stderr=subprocess.DEVNULL, timeout=45)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def extract_text(path):
    text = direct_extract(path)
    if len(text) >= 80:
        return text
    ocr = ocr_extract(path)
    return ocr if len(ocr) > len(text) else text


FAX_TYPES = [
    ("EOB/payment document", ["explanation of benefits", " eob", "allowed amount", "amount paid", "payment", "check number", "paid amount"]),
    ("Denial/appeal", ["denied", "denial", "appeal", "not medically necessary", "non compensable", "reconsideration"]),
    ("Prior authorization", ["prior authorization", "authorization", "preauthorization", "approved auth", "auth number"]),
    ("New referral", ["referral", "referred by", "new patient", "consult request", "records attached for review"]),
    ("Medical records", ["medical records", "progress note", "mri", "x-ray", "operative report", "office note"]),
    ("Attorney/legal correspondence", ["attorney", "law office", "letter of protection", " lien", "settlement", "claim petition"]),
    ("Prescription/refill", ["prescription", "refill", "pharmacy", "medication"]),
]


def classify(text):
    low = f" {text.lower()} "
    scores = []
    for label, kws in FAX_TYPES:
        score = sum(1 for kw in kws if kw in low)
        if score:
            scores.append((score, label))
    if not scores:
        return "Unknown/needs review", "medium", "Review and classify this fax."
    _, label = sorted(scores, reverse=True)[0]
    high = label in ("EOB/payment document", "Denial/appeal", "Prior authorization", "New referral")
    action = {
        "EOB/payment document": "Billing team should verify the payment/EOB is posted and attach the EOB to the patient folder.",
        "Denial/appeal": "Billing team should review denial language and prepare appeal or attorney follow-up.",
        "Prior authorization": "Front desk or authorization owner should update auth status and schedule next step.",
        "New referral": "Intake team should create or update the patient chart and referral folder.",
        "Medical records": "Clinical team should attach records to the patient folder and flag anything actionable.",
        "Attorney/legal correspondence": "Billing/legal coordination owner should update attorney or LOP status.",
        "Prescription/refill": "Clinical team should route refill or medication request for provider review.",
    }.get(label, "Review and classify this fax.")
    return label, "high" if high else "medium", action


def load_patients():
    if not os.path.exists(PATIENT_DB):
        return []
    try:
        data = json.load(open(PATIENT_DB, encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def norm_name(value):
    return re.sub(r"[^a-z]+", " ", str(value or "").lower()).strip()


def norm_digits(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 7 else digits


def match_patient(text, patients):
    low = text.lower()
    text_digits = re.sub(r"\D", "", text)
    best = {"patient": "", "confidence": "unmatched", "reason": "No reliable patient identifier found."}
    name_hits = []
    for p in patients:
        name = p.get("name", "")
        n = norm_name(name)
        if not n or len(n.split()) < 2:
            continue
        dob = str(p.get("dob") or "").strip()
        phone = norm_digits(p.get("phone"))
        attorney = norm_name(p.get("attorney"))
        dos = str(p.get("dateOfService") or "").strip()
        score, reasons = 0, []
        if n in low:
            score += 3
            reasons.append("name")
        if dob and dob.lower() in low:
            score += 3
            reasons.append("DOB")
        if phone and phone in text_digits:
            score += 2
            reasons.append("phone")
        if dos and dos.lower() in low:
            score += 2
            reasons.append("DOS")
        if attorney and len(attorney) > 5 and attorney in low:
            score += 1
            reasons.append("attorney")
        if score >= 6:
            return {"patient": name, "confidence": "high", "reason": "Matched by " + ", ".join(reasons)}
        if score >= 3:
            name_hits.append((score, name, reasons))
    if name_hits:
        name_hits.sort(reverse=True)
        top = name_hits[0]
        duplicate_top = len([h for h in name_hits if h[0] == top[0]]) > 1
        return {
            "patient": top[1] if not duplicate_top else "",
            "confidence": "medium" if not duplicate_top else "low",
            "reason": "Matched by " + ", ".join(top[2]) if not duplicate_top else "Multiple possible patient names matched.",
        }
    return best


def summarize(fax_type, text, patient_name, confidence, action):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    signal = []
    for ln in lines:
        low = ln.lower()
        if any(tok in low for tok in ["eob", "denied", "authorization", "referral", "attorney", "records", "payment", "balance", "claim"]):
            signal.append(ln)
        if len(signal) >= 3:
            break
    if not signal:
        signal = lines[:3]
    body = " ".join(signal)[:550] if signal else "No readable text was extracted. Staff should review the original fax."
    patient = patient_name or "No reliable patient match"
    return f"Fax type: {fax_type}. Patient: {patient} ({confidence}). Summary: {body}. Action needed: {action}"


def append_audit(conn, message_id, actor, action, detail):
    conn.execute(
        "INSERT INTO fax_audit (fax_message_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (message_id, actor, action, detail, now_iso()),
    )


def append_timeline_note(patient_name, summary, message_id):
    if not patient_name or not os.path.exists(PATIENT_DB):
        return False
    patients = load_patients()
    changed = False
    note = f"[RingCentral Fax {datetime.now().strftime('%Y-%m-%d')}] {summary}"
    marker = f"fax:{message_id}"
    for p in patients:
        if p.get("name") == patient_name:
            notes = p.setdefault("notes_summaries", [])
            if not any(marker in str(n) for n in notes):
                notes.append(f"{note} ({marker})")
                changed = True
            break
    if changed:
        backup = PATIENT_DB.replace(".json", f".backup-fax-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(PATIENT_DB, backup)
        json.dump(patients, open(PATIENT_DB, "w", encoding="utf-8"), indent=2)
    return changed


def create_task(patient_name, fax_type, action, message_id, owner="Unassigned"):
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    tasks = []
    if os.path.exists(TASKS_JSON):
        try:
            tasks = json.load(open(TASKS_JSON, encoding="utf-8"))
        except Exception:
            tasks = []
    task_key = f"fax_{message_id}"
    existing = next((t for t in tasks if t.get("taskKey") == task_key), None)
    if existing:
        return existing.get("id", "")
    task_id = f"task_fax_{safe_filename(message_id)}"
    task = {
        "id": task_id,
        "patientName": patient_name or "Fax Review Queue",
        "taskKey": task_key,
        "taskLabel": f"Review fax: {fax_type}",
        "assignee": owner,
        "status": "Pending",
        "notes": action,
        "dueDate": datetime.now().strftime("%Y-%m-%d"),
        "autoGenerated": True,
        "createdBy": "ringcentral_fax_sync",
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    tasks.append(task)
    json.dump(tasks, open(TASKS_JSON, "w", encoding="utf-8"), indent=2)
    return task_id


def upsert_fax(conn, record, attachment_path, mime, text):
    message_id = str(record.get("id") or "")
    if not message_id:
        return "skipped"
    if conn.execute("SELECT 1 FROM faxes WHERE message_id = ?", (message_id,)).fetchone():
        return "duplicate"
    patients = load_patients()
    fax_type, priority, action = classify(text)
    match = match_patient(text, patients)
    summary = summarize(fax_type, text, match["patient"], match["confidence"], action)
    status = "filed" if match["confidence"] == "high" else "review"
    owner = "Billing Team" if fax_type in ("EOB/payment document", "Denial/appeal") else ("Intake Team" if fax_type == "New referral" else "Unassigned")
    task_id = create_task(match["patient"], fax_type, action, message_id, owner=owner)
    timeline_filed = 0
    if match["confidence"] == "high":
        timeline_filed = 1 if append_timeline_note(match["patient"], summary, message_id) else 0

    sender = ((record.get("from") or {}).get("phoneNumber") or (record.get("from") or {}).get("name") or "")
    recipient = ((record.get("to") or [{}])[0].get("phoneNumber") if isinstance(record.get("to"), list) and record.get("to") else "")
    conn.execute("""
        INSERT INTO faxes (
            message_id, received_at, sender, recipient, subject, attachment_path, attachment_mime,
            extracted_text, fax_type, priority, summary, action_needed, suggested_owner,
            patient_name, match_confidence, match_reason, status, task_id, timeline_filed,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message_id, record.get("creationTime") or record.get("lastModifiedTime") or now_iso(),
        sender, recipient, record.get("subject", ""), attachment_path, mime, text,
        fax_type, priority, summary, action, owner, match["patient"], match["confidence"],
        match["reason"], status, task_id, timeline_filed, now_iso(), now_iso(),
    ))
    append_audit(conn, message_id, "ringcentral_fax_sync", "ingested", f"{fax_type}; {match['confidence']}; task={task_id}")
    return "created"


def sync(days=1, dry_run=False):
    token = get_access_token()
    records = fetch_inbound_faxes(token, days=days)
    conn = db()
    counts = {"records": len(records), "created": 0, "duplicates": 0, "skipped": 0}
    for rec in records:
        message_id = str(rec.get("id") or "")
        if not message_id:
            counts["skipped"] += 1
            continue
        if conn.execute("SELECT 1 FROM faxes WHERE message_id = ?", (message_id,)).fetchone():
            counts["duplicates"] += 1
            continue
        if dry_run:
            counts["created"] += 1
            continue
        path, mime = download_first_attachment(token, rec)
        text = extract_text(path)
        result = upsert_fax(conn, rec, path, mime, text)
        if result == "created":
            counts["created"] += 1
        elif result == "duplicate":
            counts["duplicates"] += 1
        else:
            counts["skipped"] += 1
        conn.commit()
    conn.close()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        counts = sync(days=args.days, dry_run=args.dry_run)
        print(json.dumps({"status": "ok", **counts}, indent=2))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

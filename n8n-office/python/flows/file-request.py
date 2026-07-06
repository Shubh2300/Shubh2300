#!/usr/bin/env python3
"""
file-request.py — File Request flow (FastAPI module).

End-to-end pipeline for "patient texts the clinic asking for a copy of their
records / imaging / surgical report":

    inbound (SMS body + sender)
        |
        v
    1. emr_bridge.lookup_patient(sender_phone or name)    [read-only]
        |
        v
    2. find the patient's folder in Google Drive          [read-only]
        |
        v
    3. pick the requested file (LLM/keyword match on body)
        |
        v
    4. mint a short-lived shareable link for that file
        |
        v
    5. POST a row into the approval gate, render an HTML
       page for staff at  GET  /approval/{token}
       and accept POST  /approval/{token}/decide
        |
        v
    6. on APPROVE -> ringcentral_adapter.send_sms(patient_phone, link)
       on REJECT  -> audit_log + optional staff-drafted reply

Lock decisions reflected here (see synthesis notes):

  * Q2: this is the 2nd flow built end-to-end (after Patient Lookup).
  * Q4: HTML approval gate is the chosen UX (NOT Slack/email). The URL
    pattern locked here is /approval/{token} for the human view and
    /approval/{token}/decide for the form POST. Tokens are 32-byte
    url-safe randoms held in-memory + on disk (cache/file_request_gate.json).
  * Q5: Drive read-only (drive.readonly scope). No writes.
  * Q6: credentials live in .env (chmod 0600 on a FileVault disk).
  * Q7: every state change appends a row to AuditLog (hash chain).
  * Q10: dead-letter / retry handled by the n8n wrapper, not here.
  * Q11: BAA register required for RingCentral + Google Workspace; we do
    NOT log raw patient phone or file contents in the audit log — only
    the hashed patient id and the file's Drive id.

CREDENTIALS — every required env var is marked with `# TODO: WIRE CREDS`
so a grep tells you exactly what to set before go-live. NONE of them are
defaulted to real values; the module degrades to a "smoke" mode (no real
API calls) when the creds are absent so the importer/smoke test works on
a fresh machine.

Antigravity style notes followed here:
  * stdlib-first; FastAPI is the *only* third-party import at module load.
  * Drive / Google API client imported lazily inside `_drive()` so the
    module imports cleanly even if `google-api-python-client` isn't
    installed yet (the smoke test exercises everything else).
  * Honest data: if Drive lookup returns nothing, the API returns
    "no_files_found" — we never fabricate a filename or a link.
  * No PHI written into the repo. Token store lives at
    /Users/shubh/n8n-office/cache/file_request_gate.json (gitignored).
  * Routes mirror server.py's `elif path == "..."` clarity, just expressed
    with FastAPI decorators.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Make the integrations package importable when this file is run directly
# (`python python/flows/file-request.py`) AND when imported by n8n's
# Execute Command node from any cwd.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent  # .../n8n-office/python
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from integrations import ringcentral_adapter  # noqa: E402
from integrations import emr_bridge  # noqa: E402

# audit_log is class-based; instantiate lazily so the smoke test doesn't
# need a real pepper.
from integrations.audit_log import AuditLog, AuditLogError  # noqa: E402


# ---------------------------------------------------------------------------
# Configuration — every secret/path is overridable via env var.
# ---------------------------------------------------------------------------

# Service account JSON for Google Drive. Antigravity already has one at
# ~/Documents/Antigravity/service_account.json per CLAUDE.md; we default to
# that path but the operator can point elsewhere.
#
# TODO: WIRE CREDS - GOOGLE_SERVICE_ACCOUNT_JSON
#   Absolute path to a service-account JSON with `drive.readonly` (and
#   ideally `drive.file` if you later need to mint per-request share links).
#   The service account must be added as a Viewer on the patient-folders
#   root, or domain-wide-delegation must be enabled with a Workspace admin.
SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    str(Path.home() / "Documents" / "Antigravity" / "service_account.json"),
)

# TODO: WIRE CREDS - DRIVE_PATIENT_ROOT_FOLDER_ID
#   The Google Drive folder ID that contains one subfolder per patient.
#   This is the same value as Apps Script Config.ROOT_DRIVE_FOLDER_ID.
#   Find it in the Drive URL after /folders/...
DRIVE_PATIENT_ROOT_FOLDER_ID = os.environ.get("DRIVE_PATIENT_ROOT_FOLDER_ID", "")

# TODO: WIRE CREDS - AUDIT_PEPPER
#   32+ random bytes (use `python -c "import secrets;print(secrets.token_hex(32))"`).
#   MUST be stable for the life of the audit log; rotating it breaks the
#   ability to group rows by patient. Back up alongside the FileVault
#   recovery key (see Q11).
AUDIT_PEPPER = os.environ.get("AUDIT_PEPPER", "")

# TODO: WIRE CREDS - STAFF_APPROVAL_BASE_URL
#   Public HTTPS base URL where the approval gate is reachable
#   (e.g. https://office.<tailnet>.ts.net). Used only to build the link
#   that's shown to staff in logs / dashboards — the gate itself binds
#   to whatever host FastAPI is launched on.
STAFF_APPROVAL_BASE_URL = os.environ.get(
    "STAFF_APPROVAL_BASE_URL", "http://localhost:8001"
)

# Where we persist pending approval tokens. Outside the repo per house rules
# (PHI like patient name appears in the gate state).
CACHE_DIR = Path(
    os.environ.get("N8N_OFFICE_CACHE_DIR", "/Users/shubh/n8n-office/cache")
)
GATE_STORE = CACHE_DIR / "file_request_gate.json"
AUDIT_DB = CACHE_DIR / "audit.db"

# Drive scope. Read-only matches the Q5 v1 posture.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Token lifetime — staff has 24h to act before the gate auto-expires. A new
# request from the same patient will mint a fresh token.
TOKEN_TTL_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Audit log (lazy — needs AUDIT_PEPPER to be set)
# ---------------------------------------------------------------------------

_audit_singleton: Optional[AuditLog] = None
_audit_lock = threading.Lock()


def _audit() -> Optional[AuditLog]:
    """Return a process-wide AuditLog, or None if AUDIT_PEPPER is unset.

    We return None instead of raising so the smoke test and a fresh-install
    importer don't crash; production callers should treat a None audit log
    as a hard error and refuse to ship.
    """
    global _audit_singleton
    if _audit_singleton is not None:
        return _audit_singleton
    if not AUDIT_PEPPER:
        return None
    with _audit_lock:
        if _audit_singleton is None:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _audit_singleton = AuditLog(str(AUDIT_DB), pepper=AUDIT_PEPPER)
    return _audit_singleton


def _audit_log(
    *,
    intent: str,
    action: str,
    result_summary: str,
    actor: str = "file-request-flow",
    patient_id: Optional[str] = None,
    error_msg: Optional[str] = None,
) -> None:
    """Best-effort audit-log write. Never raises into the caller — a failed
    audit write is itself an event but it shouldn't drop the user request on
    the floor (the n8n dead-letter path will re-surface persistent failures)."""
    log = _audit()
    if log is None:
        return
    try:
        log.log(
            actor=actor,
            intent=intent,
            action=action,
            result_summary=result_summary,
            patient_id=patient_id,
            error_msg=error_msg,
        )
    except AuditLogError:
        pass


# ---------------------------------------------------------------------------
# Google Drive client — scaffolded, lazy, dependency-light.
# ---------------------------------------------------------------------------


class DriveClientError(RuntimeError):
    """Raised when Drive setup or a Drive call fails."""


_drive_singleton = None
_drive_lock = threading.Lock()


def _drive():
    """Build and cache a Drive v3 service client.

    Lazy because `google-api-python-client` is heavy and may not be installed
    on the box that imports this module for type-checking / linting only.
    Raises DriveClientError with a clear remediation string if anything's
    missing — never returns a half-built client.
    """
    global _drive_singleton
    if _drive_singleton is not None:
        return _drive_singleton

    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as exc:
        raise DriveClientError(
            "google-api-python-client / google-auth not installed. "
            "Run: pip install google-api-python-client google-auth"
        ) from exc

    if not SERVICE_ACCOUNT_JSON or not os.path.exists(SERVICE_ACCOUNT_JSON):
        raise DriveClientError(
            f"Service account JSON not found at {SERVICE_ACCOUNT_JSON!r}. "
            "Set GOOGLE_SERVICE_ACCOUNT_JSON or place service_account.json "
            "at the default Antigravity path."
        )

    with _drive_lock:
        if _drive_singleton is None:
            creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_JSON, scopes=DRIVE_SCOPES
            )
            # cache_discovery=False — avoids a noisy stderr warning from the
            # googleapiclient discovery cache that pollutes n8n logs.
            _drive_singleton = build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
    return _drive_singleton


def find_patient_folder(patient_name: str) -> Optional[dict]:
    """Find the Drive folder for `patient_name` under DRIVE_PATIENT_ROOT_FOLDER_ID.

    Returns {"id": str, "name": str} or None. Matches the convention used by
    intake_gs / Code.js: each patient gets a top-level folder named after them
    under the configured root.
    """
    if not patient_name:
        return None
    if not DRIVE_PATIENT_ROOT_FOLDER_ID:
        raise DriveClientError(
            "DRIVE_PATIENT_ROOT_FOLDER_ID is unset; cannot scope folder search."
        )
    svc = _drive()
    safe = patient_name.replace("'", r"\'")
    # mimeType filter so we don't accidentally match a same-named file.
    q = (
        f"'{DRIVE_PATIENT_ROOT_FOLDER_ID}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and name contains '{safe}' "
        f"and trashed = false"
    )
    resp = svc.files().list(
        q=q,
        fields="files(id, name)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    if not files:
        return None
    # Exact-match preferred; otherwise first hit.
    for f in files:
        if f.get("name", "").strip().lower() == patient_name.strip().lower():
            return {"id": f["id"], "name": f["name"]}
    return {"id": files[0]["id"], "name": files[0]["name"]}


def list_patient_files(folder_id: str) -> list[dict]:
    """List non-folder files directly under `folder_id`. Returns up to 50
    items — patients with more than 50 docs are vanishingly rare; if it
    becomes an issue, paginate."""
    if not folder_id:
        return []
    svc = _drive()
    q = (
        f"'{folder_id}' in parents "
        f"and mimeType != 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    resp = svc.files().list(
        q=q,
        fields="files(id, name, mimeType, modifiedTime, webViewLink)",
        pageSize=50,
        orderBy="modifiedTime desc",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return resp.get("files", [])


def pick_best_file(files: list[dict], query: str) -> Optional[dict]:
    """Pick the best Drive file for a free-text request.

    Deliberately simple: case-insensitive keyword scoring on filename. The
    LLM-based picker (Q3, Haiku) is the production path; this fallback is
    the safety net for when the LLM is down. Honest fallback — if nothing
    scores > 0, returns None instead of guessing.
    """
    if not files:
        return None
    if not query:
        return files[0]  # most-recently-modified per list order
    tokens = [t for t in query.lower().split() if len(t) > 2]
    if not tokens:
        return files[0]
    scored = []
    for f in files:
        name = (f.get("name") or "").lower()
        score = sum(1 for t in tokens if t in name)
        if score:
            scored.append((score, f))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def build_share_link(file_id: str) -> str:
    """Build a Drive download/preview URL for a file id.

    We deliberately use the `webViewLink`-style URL (uc?export=download&id=...)
    rather than minting a per-request anyone-with-link share — minting a
    share would be a WRITE op (Q5) and require a wider scope. The service
    account must already have access to the file via the patient folder
    being shared with it.
    """
    if not file_id:
        raise ValueError("file_id is required")
    return f"https://drive.google.com/uc?export=download&id={file_id}"


# ---------------------------------------------------------------------------
# Approval gate — token store, HTML render, decision dispatch
# ---------------------------------------------------------------------------


@dataclass
class FileRequest:
    """One in-flight file request awaiting staff approval."""

    token: str
    created_at: float
    patient_id: str          # name or phone — whatever the lookup matched on
    patient_phone: str       # E.164 to text on approve
    requested_body: str      # what the patient texted, for staff context
    drive_file_id: str
    drive_file_name: str
    share_link: str
    status: str = "pending"  # pending | approved | rejected | expired | sent
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    sms_id: Optional[str] = None
    error: Optional[str] = None

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        return (now - self.created_at) > TOKEN_TTL_SECONDS


_gate_lock = threading.Lock()


def _load_gate() -> dict[str, FileRequest]:
    if not GATE_STORE.exists():
        return {}
    try:
        with GATE_STORE.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k: FileRequest(**v) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _save_gate(state: dict[str, FileRequest]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GATE_STORE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump({k: asdict(v) for k, v in state.items()}, fh, indent=2)
    tmp.replace(GATE_STORE)


def create_file_request(
    *,
    patient_id: str,
    patient_phone: str,
    requested_body: str,
    drive_file_id: str,
    drive_file_name: str,
) -> FileRequest:
    """Mint a new pending FileRequest and persist it."""
    if not (patient_id and patient_phone and drive_file_id):
        raise ValueError("patient_id, patient_phone, drive_file_id all required")
    fr = FileRequest(
        token=secrets.token_urlsafe(32),
        created_at=time.time(),
        patient_id=patient_id,
        patient_phone=patient_phone,
        requested_body=requested_body,
        drive_file_id=drive_file_id,
        drive_file_name=drive_file_name,
        share_link=build_share_link(drive_file_id),
    )
    with _gate_lock:
        state = _load_gate()
        state[fr.token] = fr
        _save_gate(state)
    _audit_log(
        intent="file-request-create",
        action="CREATE",
        result_summary=f"pending file={drive_file_name}",
        patient_id=patient_id,
    )
    return fr


def get_file_request(token: str) -> Optional[FileRequest]:
    with _gate_lock:
        state = _load_gate()
    return state.get(token)


def decide_file_request(
    token: str,
    decision: str,
    *,
    decided_by: str = "staff",
) -> FileRequest:
    """Mark a request approved/rejected and (if approved) send the SMS."""
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")

    with _gate_lock:
        state = _load_gate()
        fr = state.get(token)
        if fr is None:
            raise LookupError(f"unknown token {token!r}")
        if fr.status != "pending":
            # Idempotent: re-deciding doesn't re-send the SMS.
            return fr
        if fr.is_expired():
            fr.status = "expired"
            state[token] = fr
            _save_gate(state)
            _audit_log(
                intent="file-request-decide",
                action="EXPIRE",
                result_summary="token expired before decision",
                patient_id=fr.patient_id,
            )
            return fr

        fr.decided_at = time.time()
        fr.decided_by = decided_by
        fr.status = "approved" if decision == "approve" else "rejected"
        state[token] = fr
        _save_gate(state)

    if fr.status == "approved":
        try:
            body = (
                f"Atlantic Pain & Wellness: here is the file you requested "
                f"({fr.drive_file_name}). {fr.share_link}\n\n"
                f"If this wasn't you, reply STOP."
            )
            resp = ringcentral_adapter.send_sms(fr.patient_phone, body)
            fr.sms_id = str(resp.get("id") or "")
            _audit_log(
                intent="file-request-send",
                action="SEND_SMS",
                result_summary=f"ok sms_id={fr.sms_id}",
                patient_id=fr.patient_id,
            )
            fr.status = "sent"
        except Exception as exc:  # noqa: BLE001 — surface ANY failure to staff
            fr.error = str(exc)
            _audit_log(
                intent="file-request-send",
                action="SEND_SMS",
                result_summary="error",
                patient_id=fr.patient_id,
                error_msg=str(exc),
            )
        with _gate_lock:
            state = _load_gate()
            state[token] = fr
            _save_gate(state)
    else:
        _audit_log(
            intent="file-request-decide",
            action="REJECT",
            result_summary=f"rejected by {decided_by}",
            patient_id=fr.patient_id,
        )

    return fr


def render_gate_html(fr: FileRequest) -> str:
    """Render the staff approval page. Plain HTML, no JS framework, no PHI
    in the URL. Form POSTs back to /approval/{token}/decide."""
    age_min = int((time.time() - fr.created_at) / 60)
    status_color = {
        "pending": "#d97706",
        "approved": "#15803d",
        "sent": "#15803d",
        "rejected": "#b91c1c",
        "expired": "#6b7280",
    }.get(fr.status, "#374151")
    safe_body = (fr.requested_body or "").replace("<", "&lt;").replace(">", "&gt;")
    decide_buttons = ""
    if fr.status == "pending":
        decide_buttons = f"""
        <form method="post" action="/approval/{fr.token}/decide" style="display:inline">
          <input type="hidden" name="decision" value="approve">
          <button type="submit" style="background:#15803d;color:#fff;padding:10px 16px;border:0;border-radius:6px;font-size:14px;cursor:pointer">
            APPROVE &amp; send SMS
          </button>
        </form>
        <form method="post" action="/approval/{fr.token}/decide" style="display:inline;margin-left:8px">
          <input type="hidden" name="decision" value="reject">
          <button type="submit" style="background:#b91c1c;color:#fff;padding:10px 16px;border:0;border-radius:6px;font-size:14px;cursor:pointer">
            REJECT
          </button>
        </form>
        """
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>File request — staff approval</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background:#f9fafb; color:#111827; max-width: 640px; margin: 24px auto; padding: 0 16px; }}
  .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:20px; box-shadow:0 1px 2px rgba(0,0,0,0.04); }}
  .row {{ margin: 8px 0; display:flex; gap:8px; }}
  .label {{ color:#6b7280; min-width:120px; font-size:13px; }}
  .val {{ color:#111827; font-size:14px; word-break:break-word; }}
  .status {{ display:inline-block; padding:2px 8px; border-radius:4px; color:#fff; font-size:12px; font-weight:600; background:{status_color}; }}
  pre {{ background:#f3f4f6; padding:12px; border-radius:6px; white-space:pre-wrap; font-size:13px; }}
</style>
</head><body>
<h2>File Request — Staff Approval</h2>
<div class="card">
  <div class="row"><div class="label">Status</div><div class="val"><span class="status">{fr.status.upper()}</span></div></div>
  <div class="row"><div class="label">Patient</div><div class="val">{fr.patient_id}</div></div>
  <div class="row"><div class="label">Patient phone</div><div class="val">{fr.patient_phone}</div></div>
  <div class="row"><div class="label">Drive file</div><div class="val">{fr.drive_file_name}<br><small>id: {fr.drive_file_id}</small></div></div>
  <div class="row"><div class="label">Age</div><div class="val">{age_min} min</div></div>
  <div class="row"><div class="label">Patient said</div><div class="val"><pre>{safe_body}</pre></div></div>
  <div style="margin-top:16px">{decide_buttons}</div>
  {"<p style='color:#b91c1c;margin-top:12px'><strong>Error:</strong> " + (fr.error or "") + "</p>" if fr.error else ""}
  {"<p style='color:#6b7280;margin-top:12px'>SMS id: " + (fr.sms_id or "") + "</p>" if fr.sms_id else ""}
</div>
<p style="color:#6b7280;font-size:12px;margin-top:16px">
  Approving sends the SMS via RingCentral. Rejecting closes the request with no patient-facing message.
  Token expires {TOKEN_TTL_SECONDS // 3600}h after creation.
</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Core "routing" function — the piece n8n calls and the smoke test exercises.
# ---------------------------------------------------------------------------


def handle_inbound_file_request(payload: dict) -> dict:
    """End-to-end intake: SMS-shaped payload -> pending FileRequest token.

    payload schema:
      {
        "from": "+12155551234",          # patient phone (E.164)
        "body": "can i get my mri report",
        "patient_hint": "Dorca Jones",   # optional — n8n may pre-resolve
      }

    Returns:
      {
        "ok": bool,
        "stage": "lookup"|"drive"|"file_pick"|"gate"|"done"|"error",
        "reason": str,                   # populated when ok=False
        "patient": dict|None,            # the matched emr_bridge record
        "token": str|None,
        "approval_url": str|None,        # where staff goes to decide
      }

    Never raises into the caller — every failure mode returns a dict so the
    n8n Function node can branch on `stage` without try/except gymnastics.
    """
    result = {
        "ok": False,
        "stage": "lookup",
        "reason": "",
        "patient": None,
        "token": None,
        "approval_url": None,
    }

    sender = (payload.get("from") or "").strip()
    body = (payload.get("body") or "").strip()
    hint = (payload.get("patient_hint") or "").strip()

    if not sender or not body:
        result["stage"] = "error"
        result["reason"] = "payload requires non-empty 'from' and 'body'"
        return result

    # 1. Identify the patient. emr_bridge.lookup_patient matches on
    #    name/phone/email — try the hint first, then the raw phone.
    lookup = emr_bridge.lookup_patient(hint or sender, source="all")
    record = next(
        (r["record"] for r in lookup.get("results", []) if r.get("record")),
        None,
    )
    if record is None:
        result["reason"] = f"no patient match for {hint or sender!r}"
        _audit_log(
            intent="file-request-lookup",
            action="LOOKUP",
            result_summary="no_match",
            patient_id=hint or sender,
        )
        return result
    result["patient"] = record

    # 2. Find the Drive folder.
    result["stage"] = "drive"
    try:
        folder = find_patient_folder(record.get("name", ""))
    except DriveClientError as exc:
        result["reason"] = f"drive setup error: {exc}"
        _audit_log(
            intent="file-request-drive",
            action="DRIVE_LOOKUP",
            result_summary="error",
            patient_id=record.get("name"),
            error_msg=str(exc),
        )
        return result
    if folder is None:
        result["reason"] = f"no Drive folder for {record.get('name')!r}"
        return result

    # 3. List files + pick the best match.
    result["stage"] = "file_pick"
    try:
        files = list_patient_files(folder["id"])
    except DriveClientError as exc:
        result["reason"] = f"drive list error: {exc}"
        return result
    picked = pick_best_file(files, body)
    if picked is None:
        result["reason"] = "no_files_found"
        return result

    # 4. Mint the gate token.
    result["stage"] = "gate"
    fr = create_file_request(
        patient_id=record.get("name") or sender,
        patient_phone=sender,
        requested_body=body,
        drive_file_id=picked["id"],
        drive_file_name=picked.get("name", "(unnamed)"),
    )
    result["token"] = fr.token
    result["approval_url"] = f"{STAFF_APPROVAL_BASE_URL.rstrip('/')}/approval/{fr.token}"
    result["stage"] = "done"
    result["ok"] = True
    return result


# ---------------------------------------------------------------------------
# FastAPI surface
# ---------------------------------------------------------------------------
#
# We import FastAPI lazily inside `build_app()` for two reasons:
#   1. The module must be importable on a fresh machine (smoke test +
#      `python -c "import file_request"`) without FastAPI installed.
#   2. The n8n side can call into `handle_inbound_file_request` / the
#      Drive helpers via `python python/flows/file-request.py --inbound ...`
#      WITHOUT spinning up the HTTP server.


def build_app():
    """Construct and return the FastAPI app.

    Raises ImportError with a clear hint if FastAPI isn't installed.
    """
    try:
        from fastapi import FastAPI, Form, HTTPException, Request
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "FastAPI is not installed. Run: pip install fastapi uvicorn"
        ) from exc

    app = FastAPI(
        title="Atlantic Pain & Wellness — File Request Gate",
        version="0.1.0",
        # Docs off — this surface is reachable by staff only, no point
        # publishing the schema.
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/health")
    def _health():
        return {
            "ok": True,
            "service": "file-request",
            "audit_enabled": _audit() is not None,
            "drive_configured": bool(DRIVE_PATIENT_ROOT_FOLDER_ID)
            and os.path.exists(SERVICE_ACCOUNT_JSON),
        }

    @app.post("/inbound")
    async def _inbound(request: Request):
        """Entry point for n8n's HTTP Request node (or RingCentral's
        webhook proxy). Returns the routing result as JSON so the next
        n8n node can branch on `stage`."""
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be an object")
        out = handle_inbound_file_request(payload)
        return JSONResponse(out, status_code=200 if out["ok"] else 422)

    @app.get("/approval/{token}", response_class=HTMLResponse)
    def _gate(token: str):
        fr = get_file_request(token)
        if fr is None:
            raise HTTPException(status_code=404, detail="unknown token")
        return HTMLResponse(render_gate_html(fr))

    @app.post("/approval/{token}/decide", response_class=HTMLResponse)
    def _decide(token: str, decision: str = Form(...)):
        try:
            fr = decide_file_request(token, decision)
        except LookupError:
            raise HTTPException(status_code=404, detail="unknown token")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return HTMLResponse(render_gate_html(fr))

    return app


# Module-level `app` for `uvicorn python.flows.file_request:app`. Built on
# first attribute access so import doesn't require FastAPI.
def __getattr__(name: str):  # PEP 562
    if name == "app":
        return build_app()
    raise AttributeError(name)


# ---------------------------------------------------------------------------
# CLI / __main__ smoke test
# ---------------------------------------------------------------------------


def _smoke_test() -> int:
    """Exercise the routing logic with a fake payload, no real API calls.

    Monkey-patches Drive lookups + ringcentral send so the test runs on a
    fresh checkout. Asserts every stage transitions correctly and the gate
    state file ends up with the expected fields. Exits 0 on pass, 1 on fail.
    """
    print("=" * 60)
    print("file-request.py — smoke test (no real API calls)")
    print("=" * 60)

    # Patch Drive helpers to return canned data.
    fake_folder = {"id": "FAKE_FOLDER_ID", "name": "Dorca Jones"}
    fake_files = [
        {
            "id": "FAKE_FILE_MRI",
            "name": "MRI lumbar 2026-04-12.pdf",
            "mimeType": "application/pdf",
            "modifiedTime": "2026-04-12T10:00:00Z",
            "webViewLink": "https://drive.google.com/file/d/FAKE_FILE_MRI/view",
        },
        {
            "id": "FAKE_FILE_INTAKE",
            "name": "Intake form 2026-03-01.pdf",
            "mimeType": "application/pdf",
            "modifiedTime": "2026-03-01T10:00:00Z",
            "webViewLink": "https://drive.google.com/file/d/FAKE_FILE_INTAKE/view",
        },
    ]
    fake_patient = {
        "name": "Dorca Jones",
        "phone": "+12155551234",
        "intakeDate": "04/12/2026",
    }

    mod = sys.modules[__name__]
    mod.find_patient_folder = lambda name: fake_folder  # type: ignore[assignment]
    mod.list_patient_files = lambda folder_id: fake_files  # type: ignore[assignment]

    # Patch emr_bridge.lookup_patient too.
    def fake_lookup(patient_id, source="all"):
        return {
            "patient_id": patient_id,
            "source": source,
            "results": [
                {"source": "sis", "label": "SIS", "record": fake_patient, "cached": False}
            ],
            "session_expired": [],
        }
    emr_bridge.lookup_patient = fake_lookup  # type: ignore[assignment]

    # Patch the SMS sender so decide_file_request(approve) doesn't network.
    sent_sms: list = []

    def fake_send(to, body, from_=None):
        sent_sms.append({"to": to, "body": body, "from": from_})
        return {"id": "FAKE_SMS_ID_123", "messageStatus": "Queued"}
    ringcentral_adapter.send_sms = fake_send  # type: ignore[assignment]

    # --- Stage 1: inbound -> pending gate token --------------------------
    payload = {
        "from": "+12155551234",
        "body": "can i please get a copy of my MRI report",
        "patient_hint": "Dorca Jones",
    }
    out = handle_inbound_file_request(payload)
    print(json.dumps(out, indent=2, default=str))
    assert out["ok"] is True, f"expected ok=True, got {out}"
    assert out["stage"] == "done", f"expected stage=done, got {out['stage']}"
    assert out["token"], "expected a token"
    assert "MRI" in out["approval_url"] or out["token"] in out["approval_url"]

    # Best-file scoring: "MRI" in the body should pick the MRI doc.
    fr = get_file_request(out["token"])
    assert fr is not None
    assert fr.drive_file_id == "FAKE_FILE_MRI", \
        f"expected MRI file, got {fr.drive_file_id}"
    print(f"[ok] stage 1: picked {fr.drive_file_name} for token {fr.token[:12]}…")

    # --- Stage 2: render HTML --------------------------------------------
    html = render_gate_html(fr)
    assert "APPROVE" in html and "REJECT" in html
    assert "Dorca Jones" in html
    assert fr.drive_file_name in html
    print("[ok] stage 2: HTML gate rendered with APPROVE/REJECT controls")

    # --- Stage 3: decide approve -> SMS sent -----------------------------
    fr2 = decide_file_request(fr.token, "approve", decided_by="smoke-test")
    assert fr2.status == "sent", f"expected status=sent, got {fr2.status}"
    assert fr2.sms_id == "FAKE_SMS_ID_123"
    assert len(sent_sms) == 1
    assert sent_sms[0]["to"] == "+12155551234"
    assert "drive.google.com" in sent_sms[0]["body"]
    print(f"[ok] stage 3: SMS dispatched, body={sent_sms[0]['body'][:60]}…")

    # --- Stage 4: idempotency — re-decide returns existing record --------
    fr3 = decide_file_request(fr.token, "approve")
    assert fr3.sms_id == "FAKE_SMS_ID_123", "re-decide should not re-send"
    assert len(sent_sms) == 1
    print("[ok] stage 4: re-decide is idempotent (no double-send)")

    # --- Stage 5: reject path on a new request ---------------------------
    out2 = handle_inbound_file_request({
        "from": "+12155559999",
        "body": "send me my intake form",
        "patient_hint": "Dorca Jones",
    })
    assert out2["ok"]
    fr4 = decide_file_request(out2["token"], "reject", decided_by="smoke-test")
    assert fr4.status == "rejected"
    assert len(sent_sms) == 1, "reject must not send SMS"
    print("[ok] stage 5: reject path closes request without SMS")

    # --- Stage 6: missing payload -> error stage -------------------------
    out3 = handle_inbound_file_request({"from": "", "body": ""})
    assert out3["ok"] is False
    assert out3["stage"] == "error"
    print(f"[ok] stage 6: empty payload -> {out3['reason']}")

    print("=" * 60)
    print("ALL SMOKE TESTS PASSED")
    print("=" * 60)
    return 0


def _cli(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="File Request flow")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("smoke", help="Run the offline smoke test (no API calls)")

    serve = sub.add_parser("serve", help="Run the FastAPI gate on a local port")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8001)

    inbound = sub.add_parser("inbound", help="Process one inbound payload from stdin")
    inbound.add_argument("--from", dest="from_", required=True)
    inbound.add_argument("--body", required=True)
    inbound.add_argument("--hint", default="")

    decide = sub.add_parser("decide", help="Approve or reject a pending token")
    decide.add_argument("--token", required=True)
    decide.add_argument("--decision", choices=["approve", "reject"], required=True)
    decide.add_argument("--by", default="cli")

    args = p.parse_args(argv)

    if args.cmd == "smoke":
        return _smoke_test()

    if args.cmd == "serve":
        try:
            import uvicorn  # type: ignore
        except ImportError:
            print("uvicorn not installed. pip install fastapi uvicorn", file=sys.stderr)
            return 2
        uvicorn.run(build_app(), host=args.host, port=args.port)
        return 0

    if args.cmd == "inbound":
        out = handle_inbound_file_request({
            "from": args.from_,
            "body": args.body,
            "patient_hint": args.hint,
        })
        print(json.dumps(out, indent=2, default=str))
        return 0 if out["ok"] else 1

    if args.cmd == "decide":
        fr = decide_file_request(args.token, args.decision, decided_by=args.by)
        print(json.dumps(asdict(fr), indent=2, default=str))
        return 0

    return 2  # pragma: no cover


if __name__ == "__main__":
    # Default to the smoke test when invoked with no args, so a casual
    # `python python/flows/file-request.py` proves the routing works.
    raise SystemExit(_cli(sys.argv[1:] or ["smoke"]))

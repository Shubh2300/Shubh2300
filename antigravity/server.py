#!/usr/bin/env python3
"""
server.py - Connected Clinical Dashboard API Server

A lightweight HTTP API server built entirely on the Python standard library.
Serves the clinical frontend dashboard and coordinates backend file/folder
provisioning and database lookups in real-time inside your workspace.

Usage:
    python3 server.py
"""

import os
import json
import re
import time
import hashlib
import urllib.parse
import urllib.request
import logging
import subprocess
import sqlite3
from datetime import datetime, timedelta, date
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─── Load .env file at startup (no external dependencies needed) ──────────────
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
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()

# ─── Import Koko GPT brain (after env is loaded) ─────────────────────────────
try:
    import gpt_client
    KOKO_GPT_AVAILABLE = True
except ImportError:
    KOKO_GPT_AVAILABLE = False

# Configure standard logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("server")

# ─── Simple in-memory rate limiter for /api/koko (30 req/min per IP) ─────────
_koko_rate: dict = {}  # { ip: [timestamp, ...] }
KOKO_RATE_LIMIT = 30
KOKO_RATE_WINDOW = 60  # seconds

PORT = int(os.environ.get("PORT", "8000"))
WORKSPACE_DIR = os.environ.get(
    "ANTIGRAVITY_WORKSPACE_DIR",
    os.path.dirname(os.path.abspath(__file__)),
)
SCRATCH_DIR = os.environ.get(
    "ANTIGRAVITY_SCRATCH_DIR",
    os.path.expanduser("~/.gemini/antigravity/scratch"),
)
OUTPUT_PARENT_DIR = os.path.join(WORKSPACE_DIR, "scratch", "Antigravity_Workspace")
UPCOMING_SCHEDULE_PATH = os.path.join(WORKSPACE_DIR, "upcoming_schedule.json")
FAX_DB_PATH = os.path.join(SCRATCH_DIR, "fax_digest.sqlite3")
AUTOMATION_STATUS_PATH = os.path.join(SCRATCH_DIR, "automation_status.json")

# Replace this with your Google Apps Script Web App URL to sync statuses directly to Sheets
APPS_SCRIPT_WEBHOOK_URL = os.environ.get("APPS_SCRIPT_WEBHOOK_URL", "")

def apw_load_config():
    global APPS_SCRIPT_WEBHOOK_URL
    config_path = os.path.join(WORKSPACE_DIR, "apw_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                APPS_SCRIPT_WEBHOOK_URL = cfg.get("appsScriptWebhookUrl", APPS_SCRIPT_WEBHOOK_URL)
        except Exception as e:
            logger.error(f"Failed to load apw_config.json: {e}")

def apw_save_config(cfg):
    global APPS_SCRIPT_WEBHOOK_URL
    config_path = os.path.join(WORKSPACE_DIR, "apw_config.json")
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        APPS_SCRIPT_WEBHOOK_URL = cfg.get("appsScriptWebhookUrl", APPS_SCRIPT_WEBHOOK_URL)
        return True
    except Exception as e:
        logger.error(f"Failed to save apw_config.json: {e}")
        return False

apw_load_config()

def _load_patient_database():
    db_path = os.path.join(SCRATCH_DIR, "patient_database.json")
    if not os.path.exists(db_path):
        return []
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("Failed reading patient database: %s", e)
        return []

def _env_ready(*keys):
    return all(bool(os.environ.get(k, "").strip()) for k in keys)

def _read_scratch_json(filename, default):
    path = os.path.join(SCRATCH_DIR, filename)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception as e:
        logger.error("Failed reading %s: %s", filename, e)
        return default

def _automation_status_payload():
    status = {}
    if os.path.exists(AUTOMATION_STATUS_PATH):
        try:
            with open(AUTOMATION_STATUS_PATH, "r", encoding="utf-8") as f:
                status = json.load(f)
        except Exception:
            status = {}
    ar_dir = os.path.join(SCRATCH_DIR, "ar_reports")
    ar_reports = []
    if os.path.isdir(ar_dir):
        try:
            ar_reports = [
                os.path.join(ar_dir, f)
                for f in os.listdir(ar_dir)
                if f.lower().endswith((".csv", ".xlsx", ".xlsm")) and not f.startswith("~")
            ]
        except Exception:
            ar_reports = []
    live_readiness = {
        "ringcentral": {
            "ready": _env_ready("RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT"),
            "needs": [] if _env_ready("RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT") else ["RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT"],
        },
        "sis": {
            "ready": os.path.exists(os.path.join(SCRATCH_DIR, "sis_browser_state.json")),
            "needs": [] if os.path.exists(os.path.join(SCRATCH_DIR, "sis_browser_state.json")) else ["Fresh SIS browser session / SMS 2FA"],
        },
        "svigg": {
            "ready": _env_ready("WEBEDOCTOR_URL", "WEBEDOCTOR_USER", "WEBEDOCTOR_PASS"),
            "needs": [] if _env_ready("WEBEDOCTOR_URL", "WEBEDOCTOR_USER", "WEBEDOCTOR_PASS") else ["WEBEDOCTOR_URL", "WEBEDOCTOR_USER", "WEBEDOCTOR_PASS"],
        },
        "arReportsDir": {
            "ready": os.path.isdir(ar_dir),
            "path": ar_dir,
            "reportCount": len(ar_reports),
            "latestReport": max(ar_reports, key=os.path.getmtime) if ar_reports else "",
        },
    }
    return {
        "status": "ok",
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "latestRun": status,
        "readiness": live_readiness,
    }

def _loop_status(required_ready, planned=False):
    if planned:
        return "planned"
    if all(required_ready):
        return "ready"
    if any(required_ready):
        return "partial"
    return "blocked"

def _automation_loop_catalog():
    status_payload = _automation_status_payload()
    readiness = status_payload.get("readiness", {})
    rc_ready = readiness.get("ringcentral", {}).get("ready", False)
    sis_ready = readiness.get("sis", {}).get("ready", False)
    svigg_ready = readiness.get("svigg", {}).get("ready", False)
    ar_ready = readiness.get("arReportsDir", {}).get("ready", False)
    ar_count = readiness.get("arReportsDir", {}).get("reportCount", 0)
    loops = [
        {
            "id": "morning-command-center",
            "title": "Morning Command Center",
            "status": _loop_status([sis_ready, svigg_ready, ar_ready]),
            "trigger": "Every weekday morning or manual dashboard run",
            "sources": ["SIS", "Svigg/WebeDoctor", "local AR reports", "dashboard reconciliation"],
            "actions": ["export AR reports", "import balances", "run discrepancy scan", "surface blockers"],
            "humanReview": "Manager reviews blocked exports, high-severity billing discrepancies, and portal login prompts.",
            "n8nNodes": ["Cron", "Execute Command", "IF", "HTTP Request", "SQLite/Postgres", "Email or Slack"],
            "nextStep": "Connect to launchd/Task Scheduler or n8n Cron once the office machine is chosen.",
        },
        {
            "id": "fax-digest",
            "title": "Fax Digest Loop",
            "status": _loop_status([rc_ready]),
            "trigger": "Morning sync plus manual refresh",
            "sources": ["RingCentral message store", "fax PDF/TIFF attachments", "patient database"],
            "actions": ["download inbound faxes", "extract text/OCR", "classify document", "match patient", "create task"],
            "humanReview": "Staff confirms low-confidence patient matches and unknown fax types before filing.",
            "n8nNodes": ["Cron", "HTTP Request", "Binary Data", "Execute Command", "IF", "Webhook"],
            "nextStep": "Add RingCentral JWT with ReadMessages, then run the first backfill.",
        },
        {
            "id": "eob-reconciliation",
            "title": "EOB Payment Reconciliation Loop",
            "status": _loop_status([rc_ready, ar_ready]),
            "trigger": "New EOB fax or new AR import",
            "sources": ["Fax Digest", "SIS AR reports", "patient billing ledger"],
            "actions": ["detect payment evidence", "compare posted payment", "flag unposted EOBs", "create billing task"],
            "humanReview": "Billing verifies payment posting and marks write-off/appeal/no-action.",
            "n8nNodes": ["Webhook", "IF", "Code", "SQLite/Postgres", "Google Sheets", "Email"],
            "nextStep": "Use fax classifications and ledger evidence to create a reconciliation queue.",
        },
        {
            "id": "denial-appeal",
            "title": "Denial and Appeal Loop",
            "status": _loop_status([ar_ready]),
            "trigger": "Denied status, denial fax, or AR note containing denial language",
            "sources": ["Fax Digest", "billing notes", "SIS ledger", "attorney/case metadata"],
            "actions": ["classify denial reason", "assign appeal owner", "generate appeal checklist", "track due date"],
            "humanReview": "Billing/legal approves appeal language and verifies supporting documents.",
            "n8nNodes": ["Cron", "IF", "Code", "Google Drive", "Email Draft", "Wait"],
            "nextStep": "Add denial reason taxonomy and appeal packet template mapping.",
        },
        {
            "id": "prior-auth",
            "title": "Prior Authorization Loop",
            "status": "planned",
            "trigger": "New referral, pending DOS, or authorization-expiring status",
            "sources": ["patient worklist", "SIS schedule", "faxed auth docs", "insurance fields"],
            "actions": ["detect auth gap", "request missing documents", "assign owner", "escalate before DOS"],
            "humanReview": "Prior-auth staff confirms payer rules and uploads authorization evidence.",
            "n8nNodes": ["Cron", "IF", "Google Drive", "RingCentral SMS", "Email", "Wait"],
            "nextStep": "Confirm which EHR fields contain auth number, valid dates, and payer contact.",
        },
        {
            "id": "post-op-followup",
            "title": "Post-Op Follow-Up Loop",
            "status": "ready",
            "trigger": "Procedure completed or patient enters recovery callback schedule",
            "sources": ["team tasks", "followups.json", "RingCentral call log"],
            "actions": ["create recovery check-ins", "balance nurse workload", "log call outcome", "escalate symptoms"],
            "humanReview": "Nurse decides clinical escalation; automation only routes and records.",
            "n8nNodes": ["Cron", "HTTP Request", "IF", "RingCentral", "SQLite/Postgres", "Wait"],
            "nextStep": "Connect call outcomes to RingCentral call summaries when API credentials are ready.",
        },
        {
            "id": "attorney-status",
            "title": "Attorney and Case Status Loop",
            "status": "planned",
            "trigger": "Aged balance, legal correspondence, or no update after follow-up window",
            "sources": ["billing ledger", "attorney fields", "fax/email correspondence", "case notes"],
            "actions": ["draft status request", "schedule follow-up", "track response window", "update timeline"],
            "humanReview": "Staff sends attorney communications and confirms settlement/legal posture.",
            "n8nNodes": ["Cron", "IF", "Email Draft", "Google Drive", "Wait", "Webhook"],
            "nextStep": "Create approved attorney email/fax templates and response categories.",
        },
        {
            "id": "compliance-watchdog",
            "title": "Compliance Watchdog Loop",
            "status": "ready",
            "trigger": "Every automation run and every human review action",
            "sources": ["audit logs", "task changes", "fax review actions", "automation status"],
            "actions": ["write audit events", "flag low-confidence auto-filing", "block PHI to unapproved AI", "surface missing BAA risks"],
            "humanReview": "Admin reviews failed syncs, suspicious changes, and vendor readiness before expanding automation.",
            "n8nNodes": ["Webhook", "IF", "SQLite/Postgres", "Email", "Error Trigger"],
            "nextStep": "Move audit logs into a queryable table once the office database target is chosen.",
        },
    ]
    return {
        "status": "ok",
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "readiness": readiness,
        "loops": loops,
        "n8nPattern": {
            "default": "Cron/Webhook trigger -> fetch source -> normalize into SQLite -> rules classify -> create dashboard task -> human review -> audit log.",
            "phiRule": "Do not send PHI to any external AI or n8n cloud service unless the vendor path is approved for HIPAA/BAA.",
        },
    }

def _clinical_os_module_catalog():
    status_payload = _automation_status_payload()
    readiness = status_payload.get("readiness", {})
    rc_ready = readiness.get("ringcentral", {}).get("ready", False)
    sis_ready = readiness.get("sis", {}).get("ready", False)
    svigg_ready = readiness.get("svigg", {}).get("ready", False)
    ar_ready = readiness.get("arReportsDir", {}).get("ready", False)
    patients_ready = os.path.exists(os.path.join(SCRATCH_DIR, "patient_database.json"))
    tasks_ready = os.path.exists(os.path.join(SCRATCH_DIR, "team_tasks.json"))
    followups_ready = os.path.exists(os.path.join(SCRATCH_DIR, "followups.json"))
    billing_ready = os.path.exists(os.path.join(SCRATCH_DIR, "billing_ledger.json")) or ar_ready
    ai_configured = _env_ready("GEMINI_API_KEY") or _env_ready("OPENAI_API_KEY")
    modules = [
        {
            "id": "practiceos-kernel",
            "title": "PracticeOS Kernel",
            "status": _loop_status([patients_ready, tasks_ready]),
            "role": "Central coordinator that turns every external signal into patient state, tasks, timeline events, and audit records.",
            "inputs": ["patient records", "tasks", "faxes", "calls", "AR reports", "staff actions"],
            "outputs": ["patient timeline", "work queues", "daily digest", "audit events"],
            "sharedContracts": ["patient_id", "event_id", "task_id", "source_system", "confidence", "review_status"],
            "commercialGate": "Replace loose JSON with a migration-backed database and stable internal IDs before selling beyond one practice.",
        },
        {
            "id": "manager-neural-core",
            "title": "Manager Neural Core",
            "status": _loop_status([patients_ready, tasks_ready, billing_ready]),
            "role": "Always-on supervisor that scans every subsystem for discrepancies, missing patient info, stale work, and revenue risk.",
            "inputs": ["patient graph", "tasks", "followups", "fax review queue", "billing state", "sync health"],
            "outputs": ["manager review queue", "repair recommendations", "severity counts", "staff routing suggestions"],
            "sharedContracts": ["issue_id", "kind", "severity", "patient_id", "recommended_action", "source_refs"],
            "commercialGate": "Add auto-task creation with approval controls, issue suppression, rule tuning, and per-practice thresholds.",
        },
        {
            "id": "patient-graph",
            "title": "Patient Graph",
            "status": "ready" if patients_ready else "blocked",
            "role": "Canonical patient, case, attorney, payer, procedure, document, and task relationships.",
            "inputs": ["SIS/Svigg exports", "Google Drive rows", "manual edits", "fax matches"],
            "outputs": ["patient drawer", "matching service", "timeline", "case status"],
            "sharedContracts": ["patient_id", "case_id", "dob", "phone", "claim_number", "date_of_service"],
            "commercialGate": "Add duplicate resolution, MRN/source identifiers, and tenant-specific merge rules.",
        },
        {
            "id": "event-bus",
            "title": "Clinical Event Bus",
            "status": "planned",
            "role": "Internal nervous system: every fax, call, AR row, note, and status change becomes a durable event.",
            "inputs": ["webhooks", "polling jobs", "staff actions", "scheduled jobs"],
            "outputs": ["workflow triggers", "audit trail", "analytics facts", "notifications"],
            "sharedContracts": ["event_type", "occurred_at", "actor", "entity_ref", "payload_hash"],
            "commercialGate": "Introduce SQLite/Postgres event_log table, idempotency keys, replay tooling, and dead-letter handling.",
        },
        {
            "id": "workflow-engine",
            "title": "Workflow Engine",
            "status": _loop_status([tasks_ready, followups_ready]),
            "role": "Converts events into tasks, owners, due dates, escalation states, and staff review queues.",
            "inputs": ["events", "rules", "staff roster", "patient status"],
            "outputs": ["team tasks", "post-op followups", "review queues", "escalations"],
            "sharedContracts": ["task_id", "owner", "status", "due_date", "reason", "source_event_id"],
            "commercialGate": "Add rule versioning, role-based routing, SLA timers, and cross-practice workflow templates.",
        },
        {
            "id": "integration-hub",
            "title": "Integration Hub",
            "status": _loop_status([sis_ready, svigg_ready, rc_ready]),
            "role": "Adapters for RingCentral, SIS, Svigg/WebeDoctor, Google Drive, and future EHR/payer APIs.",
            "inputs": ["API credentials", "portal sessions", "downloaded reports", "webhooks"],
            "outputs": ["normalized events", "source snapshots", "sync status", "connector errors"],
            "sharedContracts": ["connector_id", "sync_run_id", "source_record_id", "idempotency_key"],
            "commercialGate": "Use per-customer connector configuration, secret vaulting, retry policies, and connector health dashboards.",
        },
        {
            "id": "document-intelligence",
            "title": "Document Intelligence",
            "status": _loop_status([rc_ready]),
            "role": "Ingests faxes/PDFs, extracts text, classifies document type, matches patient, and keeps originals linked.",
            "inputs": ["fax PDFs", "medical records", "EOBs", "prior auth documents", "referrals"],
            "outputs": ["document summary", "patient match", "review task", "timeline evidence"],
            "sharedContracts": ["document_id", "document_type", "attachment_path", "extraction_quality", "match_confidence"],
            "commercialGate": "Use approved OCR/AI path, redact previews where needed, and keep low-confidence filing human-reviewed.",
        },
        {
            "id": "revenue-core",
            "title": "Revenue Core",
            "status": "ready" if billing_ready else "partial",
            "role": "AR, EOB, denial, appeal, underpayment, and reconciliation intelligence.",
            "inputs": ["AR reports", "billing ledger", "faxed EOBs", "claim notes"],
            "outputs": ["balance status", "reconciliation findings", "billing tasks", "appeal packet state"],
            "sharedContracts": ["claim_id", "payer", "dos", "charge", "payment", "balance", "denial_reason"],
            "commercialGate": "Add payer-specific rules, configurable aging buckets, and exportable management reports.",
        },
        {
            "id": "communications-core",
            "title": "Communications Core",
            "status": _loop_status([followups_ready, rc_ready]),
            "role": "Calls, texts, reminders, post-op follow-ups, attorney outreach drafts, and communication digests.",
            "inputs": ["RingCentral calls/texts", "staff notes", "follow-up schedules", "message templates"],
            "outputs": ["call tasks", "communication timeline", "drafts", "missed-response alerts"],
            "sharedContracts": ["communication_id", "channel", "direction", "participant", "outcome", "callback_due"],
            "commercialGate": "Add consent tracking, template approval, opt-out handling, and per-practice phone configuration.",
        },
        {
            "id": "security-audit",
            "title": "Security and Audit Core",
            "status": "partial",
            "role": "Controls access, secrets, PHI boundaries, vendor readiness, audit logs, and compliance evidence.",
            "inputs": ["staff identity", "review actions", "connector secrets", "automation events"],
            "outputs": ["audit logs", "access decisions", "PHI guardrails", "compliance blockers"],
            "sharedContracts": ["actor_id", "tenant_id", "action", "entity_ref", "risk_level", "audit_timestamp"],
            "commercialGate": "Add RBAC, encrypted secrets, per-tenant isolation, immutable audit logs, backups, and BAA/vendor registry.",
        },
        {
            "id": "ai-gateway",
            "title": "AI Gateway",
            "status": "ready" if ai_configured else "blocked",
            "role": "Single approved path for summaries, classification, drafting, and staff copilots.",
            "inputs": ["minimum-necessary context", "prompt templates", "rules output", "redaction policies"],
            "outputs": ["draft summaries", "classifications", "suggested tasks", "confidence scores"],
            "sharedContracts": ["model_provider", "prompt_version", "phi_policy", "confidence", "human_required"],
            "commercialGate": "Only enable PHI processing on HIPAA/BAA-approved model paths with prompt/version audit.",
        },
        {
            "id": "analytics-command",
            "title": "Analytics and Command Center",
            "status": "partial",
            "role": "Turns events and tasks into daily operational, revenue, and staffing dashboards.",
            "inputs": ["event log", "task state", "billing facts", "sync status", "staff workload"],
            "outputs": ["daily digest", "KPIs", "aging report", "bottleneck list", "manager briefing"],
            "sharedContracts": ["metric_id", "period", "source", "value", "drilldown_ref"],
            "commercialGate": "Add warehouse-ready schema, report scheduling, and practice-level benchmarking without cross-tenant PHI leakage.",
        },
        {
            "id": "deployment-tenant",
            "title": "Deployment and Tenant Core",
            "status": "planned",
            "role": "Makes the product sellable: onboarding, tenant settings, backups, updates, support, and observability.",
            "inputs": ["practice profile", "connectors", "staff roles", "billing configuration"],
            "outputs": ["tenant config", "health report", "backup status", "support diagnostics"],
            "sharedContracts": ["tenant_id", "environment", "feature_flags", "backup_id", "version"],
            "commercialGate": "Package installer, migration scripts, environment checks, update channel, and support-safe diagnostics.",
        },
    ]
    return {
        "status": "ok",
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "kernel": {
            "name": "Clinical Operations OS",
            "principle": "Small specialized modules, shared event/task/patient contracts, human review for uncertainty, audit everywhere.",
            "commercialPosture": "Current app is a strong single-practice prototype. Sellable version needs tenant isolation, RBAC, database migrations, encrypted secrets, BAAs, audit hardening, and connector packaging.",
        },
        "modules": modules,
        "contracts": {
            "event": ["event_id", "tenant_id", "event_type", "entity_ref", "source_system", "payload_hash", "occurred_at"],
            "task": ["task_id", "tenant_id", "patient_id", "owner_role", "status", "due_date", "source_event_id"],
            "document": ["document_id", "tenant_id", "patient_id", "document_type", "storage_ref", "match_confidence", "review_status"],
            "audit": ["audit_id", "tenant_id", "actor_id", "action", "entity_ref", "timestamp", "risk_level"],
        },
        "sellableGates": [
            "Tenant isolation and per-practice configuration",
            "Role-based access control and staff identity",
            "Encrypted secret management and connector health",
            "Database migrations, backups, and restore testing",
            "Immutable audit logs for PHI-touching actions",
            "HIPAA vendor/BAA registry for cloud, OCR, AI, messaging, and hosting",
            "Human review queues for low-confidence matches and clinical judgment",
            "Support diagnostics that exclude PHI by default",
        ],
    }

def _manager_review_payload(limit=200):
    patients = _load_patient_database()
    tasks = _read_scratch_json("team_tasks.json", [])
    followups = _load_followups()
    today = date.today()
    issues = []

    def add_issue(kind, severity, title, patient=None, detail="", action="", source="Manager Core", refs=None):
        refs = refs or {}
        seed = json.dumps({
            "kind": kind,
            "patient": patient or "",
            "title": title,
            "detail": detail,
            "refs": refs,
        }, sort_keys=True, default=str)
        issues.append({
            "id": "mgr_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16],
            "kind": kind,
            "severity": severity,
            "title": title,
            "patient": patient or "",
            "detail": detail,
            "recommendedAction": action,
            "source": source,
            "refs": refs,
        })

    def norm_name(value):
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    def patient_dos(p):
        return (p.get("dateOfService") or p.get("dos") or p.get("intakeDate") or p.get("date") or "").strip()

    def patient_type(p):
        return (p.get("type") or p.get("caseType") or p.get("visitType") or "").strip().upper()

    def as_float(value):
        try:
            return float(str(value or "0").replace("$", "").replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            return 0.0

    # 1. Missing core patient/case information.
    for p in patients:
        name = p.get("name") or "Unknown patient"
        missing = []
        ptype = patient_type(p)
        if not (p.get("dob") or "").strip():
            missing.append("DOB")
        if not ((p.get("phone") or "").strip() or (p.get("email") or "").strip()):
            missing.append("phone/email")
        if ptype in ("MVA", "WC") and not (p.get("insurance") or "").strip():
            missing.append("insurance")
        if ptype == "MVA" and not (p.get("attorney") or "").strip():
            missing.append("attorney/LOP")
        billing = p.get("billing") or {}
        has_billing_activity = any(as_float(billing.get(k)) for k in ("balance", "payments", "billed", "charges"))
        if not patient_dos(p) and has_billing_activity:
            missing.append("DOS")
        if missing:
            add_issue(
                "missing-patient-info",
                "high" if len(missing) >= 3 else "medium",
                "Patient record missing required fields",
                patient=name,
                detail=f"Missing: {', '.join(missing)}.",
                action="Create a repair task for front desk/billing to pull missing fields from SIS, referral packet, or latest fax.",
                refs={"missing": missing},
            )

    # 2. Duplicate-looking patients by normalized name and DOB/name-only.
    by_name_dob = {}
    by_name = {}
    for p in patients:
        name_key = norm_name(p.get("name"))
        if not name_key:
            continue
        dob = (p.get("dob") or "").strip()
        by_name.setdefault(name_key, []).append(p)
        if dob:
            by_name_dob.setdefault((name_key, dob), []).append(p)
    for (name_key, dob), group in by_name_dob.items():
        if len(group) > 1:
            add_issue(
                "possible-duplicate",
                "medium",
                "Possible duplicate patient records",
                patient=group[0].get("name"),
                detail=f"{len(group)} records share normalized name and DOB {dob}.",
                action="Manager should merge or mark records as separate cases with source IDs.",
                refs={"count": len(group), "dob": dob, "names": [g.get("name") for g in group[:5]]},
            )
    for name_key, group in by_name.items():
        no_dob = [g for g in group if not (g.get("dob") or "").strip()]
        if len(group) > 1 and no_dob:
            add_issue(
                "identity-ambiguity",
                "medium",
                "Same patient name appears without enough identifiers",
                patient=group[0].get("name"),
                detail=f"{len(group)} records share the name key '{name_key}', and at least one lacks DOB.",
                action="Add DOB/MRN/source record ID so matching faxes, calls, and AR rows does not file to the wrong patient.",
                refs={"count": len(group), "names": [g.get("name") for g in group[:5]]},
            )

    # 3. Revenue risk: open balances with weak operational context.
    for p in patients:
        billing = p.get("billing") or {}
        balance = as_float(billing.get("balance"))
        payments = as_float(billing.get("payments"))
        if balance <= 0:
            continue
        notes = p.get("notes_summaries") or p.get("notes") or []
        note_text = " ".join(str(n) for n in notes[-8:])
        stale_or_sparse = len(note_text.strip()) < 80 or not re.search(r"call|emailed|fax|appeal|eob|paid|denied|review", note_text, re.I)
        if balance >= 10000 or stale_or_sparse:
            add_issue(
                "revenue-risk",
                "high" if balance >= 25000 else "medium",
                "Open balance needs manager review",
                patient=p.get("name"),
                detail=f"Open balance ${balance:,.2f}; payments ${payments:,.2f}. Recent context appears {'sparse' if stale_or_sparse else 'present'}.",
                action="Assign billing owner to verify latest payer/attorney status, EOB evidence, appeal need, and next follow-up date.",
                refs={"balance": balance, "payments": payments, "rawStatus": billing.get("raw_status", "")},
            )

    # 4. Overdue or blocked tasks.
    for t in tasks:
        status = str(t.get("status") or "").lower()
        if status == "completed":
            continue
        due = t.get("dueDate") or ""
        overdue = False
        try:
            overdue = bool(due) and datetime.strptime(due, "%Y-%m-%d").date() < today
        except ValueError:
            overdue = False
        if overdue or status == "blocked":
            add_issue(
                "task-risk",
                "high" if overdue else "medium",
                "Task is overdue or blocked",
                patient=t.get("patientName"),
                detail=f"{t.get('taskLabel', 'Task')} is {t.get('status', 'Pending')} and due {due or 'unspecified'}.",
                action="Manager should reassign, unblock, or close the task with an outcome note.",
                refs={"taskId": t.get("id"), "assignee": t.get("assignee"), "dueDate": due},
            )

    # 5. Overdue clinical follow-up calls.
    for fu in followups:
        if _followup_bucket(fu, today) == "overdue":
            add_issue(
                "overdue-followup",
                "high",
                "Patient follow-up call is overdue",
                patient=fu.get("patientName"),
                detail=f"{fu.get('title', 'Follow-up')} was due {fu.get('dueDate', 'unknown')}.",
                action="Assign nurse callback owner, document outcome, and escalate clinical concerns.",
                refs={"followupId": fu.get("id"), "assignee": fu.get("assignee"), "dueDate": fu.get("dueDate")},
            )

    # 6. Unmatched or low-confidence faxes waiting for staff review.
    try:
        conn = _fax_db()
        rows = conn.execute(
            "SELECT message_id, received_at, sender, fax_type, patient_name, match_confidence, summary "
            "FROM faxes WHERE status = 'review' OR match_confidence IN ('low', 'unmatched') "
            "ORDER BY received_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        for row in rows:
            add_issue(
                "fax-review",
                "high" if row["fax_type"] in ("Denial/appeal", "Prior authorization") else "medium",
                "Fax needs manager/staff review",
                patient=row["patient_name"] or "",
                detail=f"{row['fax_type'] or 'Unknown fax'} from {row['sender'] or 'unknown sender'}; confidence {row['match_confidence'] or 'unknown'}.",
                action="Confirm patient match, classify fax, create task, then file to timeline if appropriate.",
                refs={"messageId": row["message_id"], "receivedAt": row["received_at"], "summary": row["summary"]},
            )
    except Exception as e:
        logger.error("Manager fax review failed: %s", e)

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: (severity_rank.get(i.get("severity"), 9), i.get("kind", ""), i.get("patient", "")))
    total_by_kind = {}
    total_by_severity = {"high": 0, "medium": 0, "low": 0}
    for issue in issues:
        total_by_kind[issue["kind"]] = total_by_kind.get(issue["kind"], 0) + 1
        total_by_severity[issue["severity"]] = total_by_severity.get(issue["severity"], 0) + 1
    return {
        "status": "ok",
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total": len(issues),
            "high": total_by_severity.get("high", 0),
            "medium": total_by_severity.get("medium", 0),
            "low": total_by_severity.get("low", 0),
            "byKind": total_by_kind,
        },
        "mission": "Manager Core watches all subsystems, catches discrepancies, creates repair queues, and keeps low-confidence actions human-reviewed.",
        "issues": issues[:limit],
    }

def _create_manager_repair_task(issue, created_by="Manager Core"):
    patient_name = (issue.get("patient") or "").strip()
    if not patient_name:
        raise ValueError("Manager issue is not tied to a patient record.")

    issue_id = issue.get("id") or "unknown"
    task_key = f"manager_repair_{issue_id}"
    tasks_path = os.path.join(SCRATCH_DIR, "team_tasks.json")
    tasks = []
    if os.path.exists(tasks_path):
        with open(tasks_path, "r", encoding="utf-8") as f:
            tasks = json.load(f)

    existing = next((t for t in tasks if t.get("taskKey") == task_key), None)
    if existing:
        return existing.get("id"), False

    kind = issue.get("kind") or "manager-review"
    label_by_kind = {
        "missing-patient-info": "Manager repair: complete patient identifiers",
        "possible-duplicate": "Manager repair: resolve duplicate patient records",
        "identity-ambiguity": "Manager repair: confirm patient identity",
        "revenue-risk": "Manager review: open balance and EOB status",
        "task-risk": "Manager repair: unblock overdue task",
        "overdue-followup": "Manager repair: complete overdue follow-up",
        "fax-review": "Manager repair: classify and file fax",
    }
    severity = issue.get("severity") or "medium"
    due_days = 1 if severity == "high" else 3 if severity == "medium" else 7
    due_date = (date.today() + timedelta(days=due_days)).isoformat()
    task_id = f"task_{task_key}"
    notes = (
        f"{issue.get('title') or 'Manager finding'}\n"
        f"{issue.get('detail') or ''}\n"
        f"Recommended action: {issue.get('recommendedAction') or 'Review and resolve with source-system evidence.'}\n"
        f"Source: Manager Core issue {issue_id}. Human review required before changing patient data."
    ).strip()

    task = {
        "id": task_id,
        "patientName": patient_name,
        "taskKey": task_key,
        "taskLabel": label_by_kind.get(kind, "Manager repair task"),
        "assignee": "Unassigned",
        "status": "Pending",
        "notes": notes,
        "dueDate": due_date,
        "createdBy": created_by or "Manager Core",
        "createdAt": datetime.now().isoformat() + "-04:00",
        "updatedAt": datetime.now().isoformat() + "-04:00",
        "source": "manager_core",
        "sourceIssueId": issue_id,
    }
    tasks.append(task)
    with open(tasks_path, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)

    log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
    with open(log_file_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] Manager Core created repair task '{task['taskLabel']}' for {patient_name}\n")
    return task_id, True

def _fax_db():
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    conn = sqlite3.connect(FAX_DB_PATH)
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

def _fax_dict(row):
    d = dict(row)
    if d.get("extracted_text") and len(d["extracted_text"]) > 1200:
        d["extracted_text_preview"] = d["extracted_text"][:1200] + "..."
    else:
        d["extracted_text_preview"] = d.get("extracted_text") or ""
    d.pop("extracted_text", None)
    d["timeline_filed"] = bool(d.get("timeline_filed"))
    return d

def _fax_digest_payload(day_filter="today", limit=200):
    conn = _fax_db()
    today = date.today()
    where = []
    params = []
    if day_filter == "today":
        where.append("substr(received_at, 1, 10) = ?")
        params.append(today.strftime("%Y-%m-%d"))
    elif day_filter == "yesterday":
        where.append("substr(received_at, 1, 10) = ?")
        params.append((today - timedelta(days=1)).strftime("%Y-%m-%d"))
    elif day_filter == "review":
        where.append("status = 'review'")
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM faxes{sql_where} ORDER BY received_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    all_rows = conn.execute(f"SELECT fax_type, priority, status, match_confidence FROM faxes{sql_where}", params).fetchall()
    summary = {
        "total": len(all_rows),
        "matched": sum(1 for r in all_rows if r["match_confidence"] in ("high", "medium")),
        "unmatched": sum(1 for r in all_rows if r["match_confidence"] in ("low", "unmatched", None, "")),
        "highPriority": sum(1 for r in all_rows if r["priority"] == "high"),
        "needsReview": sum(1 for r in all_rows if r["status"] == "review"),
        "newReferrals": sum(1 for r in all_rows if r["fax_type"] == "New referral"),
        "eobs": sum(1 for r in all_rows if r["fax_type"] == "EOB/payment document"),
        "denials": sum(1 for r in all_rows if r["fax_type"] == "Denial/appeal"),
    }
    last = conn.execute("SELECT MAX(updated_at) AS last_sync FROM faxes").fetchone()
    payload = {
        "status": "ok",
        "filter": day_filter,
        "lastUpdated": last["last_sync"] if last else None,
        "summary": summary,
        "faxes": [_fax_dict(r) for r in rows],
    }
    conn.close()
    return payload

def _fax_audit(conn, message_id, actor, action, detail):
    conn.execute(
        "INSERT INTO fax_audit (fax_message_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (message_id, actor or "dashboard", action, detail, datetime.now().isoformat(timespec="seconds")),
    )

def _append_patient_fax_note(patient_name, summary, message_id):
    if not patient_name:
        return False
    db_path = os.path.join(SCRATCH_DIR, "patient_database.json")
    if not os.path.exists(db_path):
        return False
    patients = _load_patient_database()
    marker = f"fax:{message_id}"
    changed = False
    for p in patients:
        if p.get("name") == patient_name:
            notes = p.setdefault("notes_summaries", [])
            if not any(marker in str(n) for n in notes):
                notes.append(f"[RingCentral Fax] {summary} ({marker})")
                changed = True
            break
    if changed:
        backup = db_path.replace(".json", f".backup-fax-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        try:
            subprocess.run(["cp", db_path, backup], check=False)
        except Exception:
            pass
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(patients, f, indent=2)
    return changed

def _create_fax_task(patient_name, fax_type, action_needed, message_id, owner="Unassigned"):
    tasks_path = os.path.join(SCRATCH_DIR, "team_tasks.json")
    tasks = []
    if os.path.exists(tasks_path):
        try:
            with open(tasks_path, "r", encoding="utf-8") as f:
                tasks = json.load(f)
        except Exception:
            tasks = []
    task_key = f"fax_{message_id}"
    existing = next((t for t in tasks if t.get("taskKey") == task_key), None)
    if existing:
        return existing.get("id", "")
    task_id = f"task_fax_{re.sub(r'[^A-Za-z0-9]+', '_', message_id).strip('_')}"
    task = {
        "id": task_id,
        "patientName": patient_name or "Fax Review Queue",
        "taskKey": task_key,
        "taskLabel": f"Review fax: {fax_type or 'Unknown'}",
        "assignee": owner or "Unassigned",
        "status": "Pending",
        "notes": action_needed or "Review fax and decide next action.",
        "dueDate": date.today().strftime("%Y-%m-%d"),
        "autoGenerated": True,
        "createdBy": "fax-digest",
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    tasks.append(task)
    with open(tasks_path, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)
    return task_id

def _load_upcoming_schedule():
    if not os.path.exists(UPCOMING_SCHEDULE_PATH):
        return {"appointments": []}
    try:
        with open(UPCOMING_SCHEDULE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("appointments"), list):
            return data
    except Exception as e:
        logger.error("Failed reading upcoming_schedule.json: %s", e)
    return {"appointments": []}

def _patient_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")

# ─── Staff roster (named accountability — no more anonymous "Nurse 3") ────────
STAFF_PATH = os.path.join(SCRATCH_DIR, "staff.json")
DEFAULT_STAFF = [
    # Placeholder slots matching historical assignee values. Rename these to
    # the real team in Settings — accountability requires named owners.
    {"id": "nurse-1", "name": "Nurse 1", "role": "nurse", "active": True},
    {"id": "nurse-2", "name": "Nurse 2", "role": "nurse", "active": True},
    {"id": "nurse-3", "name": "Nurse 3", "role": "nurse", "active": True},
    {"id": "nurse-4", "name": "Nurse 4", "role": "nurse", "active": True},
    {"id": "intern-1", "name": "Intern 1", "role": "intern", "active": True},
    {"id": "intern-2", "name": "Intern 2", "role": "intern", "active": True},
    {"id": "intern-3", "name": "Intern 3", "role": "intern", "active": True},
    {"id": "intern-4", "name": "Intern 4", "role": "intern", "active": True},
]

def _load_staff():
    if os.path.exists(STAFF_PATH):
        try:
            with open(STAFF_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            staff = data.get("staff") if isinstance(data, dict) else data
            if isinstance(staff, list) and staff:
                return staff
        except Exception as e:
            logger.error("Failed reading staff.json: %s", e)
    return list(DEFAULT_STAFF)

def _save_staff(staff):
    with open(STAFF_PATH, "w", encoding="utf-8") as f:
        json.dump({"staff": staff, "updatedAt": datetime.now().isoformat()}, f, indent=2)

def _staff_role_map():
    return {s.get("name", ""): s.get("role", "") for s in _load_staff()}

# ─── Follow-up queue (first-class records, not localStorage state) ────────────
FOLLOWUPS_PATH = os.path.join(SCRATCH_DIR, "followups.json")
# Default post-procedure outreach cadence. Editable via apw_config.json
# key "followupProtocol": [{"offsetDays": 0, "title": "..."}, ...]
DEFAULT_FOLLOWUP_PROTOCOL = [
    {"offsetDays": 0, "callKey": "call1", "title": "Call 1: OR Discharge & Transport (Day of Surgery)"},
    {"offsetDays": 1, "callKey": "call2", "title": "Call 2: Next-Day Clinical Sync (DOS + 1 Day)"},
    {"offsetDays": 10, "callKey": "call3", "title": "Call 3: 10-Day Healing Recovery (DOS + 10 Days)"},
]

def _followup_protocol():
    config_path = os.path.join(WORKSPACE_DIR, "apw_config.json")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                proto = json.load(f).get("followupProtocol")
            if isinstance(proto, list) and proto:
                return proto
    except Exception as e:
        logger.error("Failed reading followupProtocol: %s", e)
    return DEFAULT_FOLLOWUP_PROTOCOL

def _load_followups():
    if os.path.exists(FOLLOWUPS_PATH):
        try:
            with open(FOLLOWUPS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.error("Failed reading followups.json: %s", e)
    return []

def _save_followups(followups):
    with open(FOLLOWUPS_PATH, "w", encoding="utf-8") as f:
        json.dump(followups, f, indent=2)

def _followup_bucket(fu, today=None):
    """pending follow-up -> overdue / due-today / upcoming based on dueDate."""
    today = today or date.today()
    if fu.get("status") == "completed":
        return "completed"
    try:
        due = datetime.strptime(fu.get("dueDate", ""), "%Y-%m-%d").date()
    except ValueError:
        return "upcoming"
    if due < today:
        return "overdue"
    if due == today:
        return "due-today"
    return "upcoming"

def _generate_followups_for_patient(patient_name, dos_str, assignee="Unassigned", created_by="system"):
    """Seed protocol follow-ups for a patient from their date of service.
    Returns (created, skipped) counts. Never duplicates an existing
    (patientName, callKey) pair."""
    try:
        dos = datetime.strptime(dos_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 0, 0
    followups = _load_followups()
    existing_keys = {(f.get("patientName"), f.get("callKey")) for f in followups}
    created = skipped = 0
    for step in _followup_protocol():
        call_key = step.get("callKey") or f"day{step.get('offsetDays', 0)}"
        if (patient_name, call_key) in existing_keys:
            skipped += 1
            continue
        due = dos + timedelta(days=int(step.get("offsetDays", 0)))
        followups.append({
            "id": f"fu_{_patient_slug(patient_name).lower()}_{call_key}",
            "patientName": patient_name,
            "callKey": call_key,
            "title": step.get("title", f"Follow-up (DOS + {step.get('offsetDays', 0)}d)"),
            "dos": dos_str,
            "dueDate": due.strftime("%Y-%m-%d"),
            "assignee": assignee,
            "status": "pending",
            "outcome": "",
            "notes": "",
            "createdAt": datetime.now().isoformat(),
            "createdBy": created_by,
            "completedAt": None,
            "completedBy": None,
        })
        created += 1
    if created:
        _save_followups(followups)
    return created, skipped

def _normalize_patient_lookup_name(name: str) -> str:
    clean = re.sub(r"\bDOB\b.*$", "", name or "", flags=re.I).strip()
    clean = re.sub(r"^(ms|mr|mrs|dr)\.?\s+", "", clean, flags=re.I).strip()
    clean = re.sub(r"[^a-z0-9]+", " ", clean.lower()).strip()
    return clean

def _is_safe_local_path(path: str) -> bool:
    try:
        real = os.path.realpath(path)
        allowed_roots = [
            os.path.realpath(WORKSPACE_DIR),
            os.path.realpath(SCRATCH_DIR),
        ]
        return any(real == root or real.startswith(root + os.sep) for root in allowed_roots)
    except Exception:
        return False

def _find_patient_in_db(patient_name: str):
    target = _normalize_patient_lookup_name(patient_name)
    patients = _load_patient_database()
    for p in patients:
        candidate = _normalize_patient_lookup_name(p.get("name", ""))
        if candidate == target:
            return p
    for p in patients:
        candidate = _normalize_patient_lookup_name(p.get("name", ""))
        if target and (candidate.startswith(target) or target.startswith(candidate)):
            return p
    return None

def _find_patient_in_collection(patient_name: str, patients):
    target = _normalize_patient_lookup_name(patient_name)
    for p in patients:
        if _normalize_patient_lookup_name(p.get("name", "")) == target:
            return p
    for p in patients:
        candidate = _normalize_patient_lookup_name(p.get("name", ""))
        if target and (candidate.startswith(target) or target.startswith(candidate)):
            return p
    return None

def _resource_not_scanned_payload(patient_name: str, resource_type: str):
    slug = _patient_slug(patient_name)
    return {
        "patientName": patient_name,
        "resourceType": resource_type,
        "exists": False,
        "openable": False,
        "path": "",
        "expectedPath": os.path.join(OUTPUT_PARENT_DIR, slug),
        "missingReason": "Not scanned during schedule load. Use the resource button to check/open this item on demand.",
        "scanDeferred": True,
        "knownData": {},
    }

def _candidate_patient_folders(patient_name: str, dob: str = ""):
    slug = _patient_slug(patient_name)
    roots = [
        OUTPUT_PARENT_DIR,
        os.path.join(SCRATCH_DIR, "Antigravity_Workspace"),
    ]
    candidates = []
    if slug:
        candidates.append(slug)
        if dob:
            candidates.append(f"{slug}_DOB_{dob}")
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirname in os.listdir(root):
            normalized_dir = _patient_slug(dirname).lower()
            if slug and slug.lower() in normalized_dir:
                candidates.insert(0, os.path.join(root, dirname))
        for candidate in candidates:
            if os.path.isabs(candidate):
                path = candidate
            else:
                path = os.path.join(root, candidate)
            if os.path.exists(path):
                yield path

def _find_file_under(folder: str, keywords):
    if not folder or not os.path.isdir(folder):
        return None
    lowered_keywords = [k.lower() for k in keywords]
    for root, _, files in os.walk(folder):
        for filename in files:
            lowered = filename.lower()
            if all(k in lowered for k in lowered_keywords):
                return os.path.join(root, filename)
    return None

def patient_resource_payload(patient_name: str, resource_type: str):
    patient = _find_patient_in_db(patient_name) or {}
    dob = patient.get("dob", "")
    folder = next(_candidate_patient_folders(patient_name, dob), None)
    slug = _patient_slug(patient_name)
    expected_folder = os.path.join(OUTPUT_PARENT_DIR, f"{slug}_DOB_{dob}" if dob else slug)

    payload = {
        "patientName": patient_name,
        "resourceType": resource_type,
        "exists": False,
        "openable": False,
        "path": "",
        "expectedPath": expected_folder,
        "missingReason": "",
        "knownData": {
            "dob": dob,
            "phone": patient.get("phone", ""),
            "email": patient.get("email", ""),
            "notesCount": len(patient.get("notes_summaries", []) or []),
            "hasTranscript": bool(patient.get("transcript") and patient.get("transcript") != "No transcript available."),
        },
    }

    if resource_type == "folder":
        if folder:
            payload.update({
                "exists": True,
                "openable": True,
                "path": folder,
                "url": f"file://{folder}",
            })
        else:
            payload["missingReason"] = "No local or synced Drive folder was found for this patient."
        return payload

    if resource_type == "summary":
        summary_path = _find_file_under(folder, ["case", "summary"]) if folder else None
        if summary_path:
            payload.update({
                "exists": True,
                "openable": True,
                "path": summary_path,
                "url": f"file://{summary_path}",
            })
        else:
            payload["expectedPath"] = os.path.join(expected_folder, "Internal_Forms", f"{slug}_Case_Summary.md")
            payload["missingReason"] = "No case summary document was found in the patient's Internal Forms folder."
        return payload

    if resource_type == "notes":
        notes_path = _find_file_under(folder, ["clinical", "notes"]) if folder else None
        if notes_path:
            payload.update({
                "exists": True,
                "openable": True,
                "path": notes_path,
                "url": f"file://{notes_path}",
            })
        else:
            payload["expectedPath"] = os.path.join(expected_folder, "Referral_&_Medical_Records", f"{slug}_Clinical_Notes")
            payload["missingReason"] = "No clinical notes document was found. The app only has database notes_summaries for this patient."
        return payload

    payload["missingReason"] = f"Unknown resource type: {resource_type}"
    return payload

def billing_clearance_analysis(patient, patient_tasks, resource_status, case_type, insurance, notes_text):
    name = patient.get("name", "Unknown patient")
    status = patient.get("surgeryStatus", "Intake")
    task_by_key = {t.get("taskKey"): t for t in patient_tasks}
    blocked_tasks = [t for t in patient_tasks if t.get("status") == "Blocked"]
    pending_tasks = [t for t in patient_tasks if t.get("status") in {"Pending", "In Progress"}]
    billing_task = task_by_key.get("billing_followup", {})

    evidence = [
        f"Patient database profile for {name}.",
        f"Current case type: {case_type}.",
        f"Current payer shown in dashboard: {insurance}.",
        f"Current surgery status: {status}.",
        f"{len(patient.get('notes_summaries', []) or [])} intake note(s) available.",
        f"{len(patient_tasks)} task record(s) checked.",
    ]

    missing = []
    blockers = []
    next_steps = []
    owner = "Billing Team"
    confidence = "Medium"

    if not resource_status.get("folder", {}).get("exists"):
        missing.append("Patient Drive folder is not present in the local/synced workspace.")
    else:
        evidence.append("Patient Drive folder exists locally.")

    if not resource_status.get("summary", {}).get("exists"):
        missing.append("Case summary document is missing.")
    else:
        evidence.append("Case summary document exists.")

    if not resource_status.get("notes", {}).get("exists"):
        missing.append("Clinical notes document is missing; only database note summaries are available.")
    else:
        evidence.append("Clinical notes document exists.")

    if blocked_tasks:
        owner = blocked_tasks[0].get("assignee") or owner
        confidence = "High"
        for task in blocked_tasks:
            blockers.append(f"{task.get('taskLabel')}: {task.get('notes') or 'No note entered.'}")
            next_steps.append(f"{task.get('assignee') or 'Assigned staff'} should resolve: {task.get('taskLabel')}.")

    if status in {"Issue", "Denied"}:
        confidence = "High"
        blockers.append(f"Surgery status is marked {status}.")
        next_steps.append("Billing team should review the denial/issue record before the nurse spends time searching folders.")

    if "claim number" in notes_text and any(token in notes_text for token in ["no claim", "don't have a claim", "do not have a claim", "does not have a claim"]):
        confidence = "High"
        blockers.append("Intake notes say the patient does not have a claim number yet.")
        missing.append("Claim number.")
        next_steps.append("Call patient or attorney for claim number before billing clearance can be completed.")

    if "attorney" in notes_text and any(token in notes_text for token in ["lop", "letter of protection", "lien"]):
        evidence.append("Notes mention attorney/LOP context.")
    elif case_type == "PIP/MVA":
        missing.append("Attorney/LOP confirmation is not structured in the current data.")

    if case_type == "Workers' Comp":
        missing.append("Workers' Comp claim acceptance/adjuster confirmation is not structured in the current data.")

    if billing_task:
        evidence.append(f"Billing follow-up task is {billing_task.get('status', 'Unknown')}: {billing_task.get('notes', 'No note entered.')}")
        if billing_task.get("status") != "Completed":
            blockers.append(f"Billing follow-up task is {billing_task.get('status', 'Pending')}.")
            next_steps.append(f"{billing_task.get('assignee') or 'Billing Team'} should update claim submission/payment follow-up.")

    if not blockers and pending_tasks:
        owner = pending_tasks[0].get("assignee") or owner
        blockers.append("No single billing denial was found, but clearance is incomplete because checklist tasks are still open.")
        next_steps.append(f"{owner} should complete the next open checklist task: {pending_tasks[0].get('taskLabel')}.")

    if not blockers:
        blockers.append("No billing-specific blocker is documented in the current local data.")
        next_steps.append("Billing team should add claim number, LOP/attorney status, submitted claim date, payer response, and expected payment date.")
        confidence = "Low"

    if not missing:
        missing.append("No missing collateral detected from the local resource scan.")

    return {
        "summary": blockers[0],
        "owner": owner,
        "confidence": confidence,
        "blockers": blockers[:4],
        "missingInfo": missing[:6],
        "evidenceChecked": evidence[:8],
        "nextSteps": next_steps[:4],
    }

def _schedule_patient_payload(appt, schedule_meta, patients, p_tasks_map):
    name = appt.get("name", "").strip()
    patient = _find_patient_in_collection(name, patients) or {}
    db_name = patient.get("name") or name
    p_tasks = p_tasks_map.get(db_name) or p_tasks_map.get(name) or []
    schedule_insurance = (appt.get("insurance") or "").strip()
    insurance_missing = schedule_insurance in {"", "(none)", "none", "None"}
    insurance = patient.get("insurance") or ("" if insurance_missing else schedule_insurance)
    insurance_display = insurance or "Not listed in scheduler"
    provider = appt.get("provider") or schedule_meta.get("provider") or ""
    visit_type = appt.get("visitType") or "Appointment"
    appointment_time = appt.get("time") or ""
    appointment_date = schedule_meta.get("scheduleDate", "")
    appointment_date_display = schedule_meta.get("scheduleDateDisplay") or appointment_date
    note = appt.get("note", "").strip()
    schedule_status = appt.get("status", "Scheduled")

    resource_name = db_name or name
    resource_status = {
        "folder": _resource_not_scanned_payload(resource_name, "folder"),
        "summary": _resource_not_scanned_payload(resource_name, "summary"),
        "notes": _resource_not_scanned_payload(resource_name, "notes"),
    }

    patient_known = bool(patient)
    has_demographics = bool(patient.get("dob") and patient.get("phone"))
    booking_status = "Blocked" if schedule_status == "Needs Review" else "Completed"
    checklist_payload = {
        "pip": {
            "label": "Insurance",
            "status": "Pending" if insurance_missing else "Completed",
            "assignee": "Intern 1",
            "notes": "Scheduler insurance column is blank; confirm payer before arrival." if insurance_missing else f"Scheduler shows {schedule_insurance}."
        },
        "records": {
            "label": "Chart",
            "status": "Completed" if patient_known else "Pending",
            "assignee": "Intern 2",
            "notes": "Patient record is linked in the local database." if patient_known else "No matching local patient record was found; link/create chart."
        },
        "forms": {
            "label": "Demographics",
            "status": "Completed" if has_demographics else ("In Progress" if patient_known else "Pending"),
            "assignee": "Nurse 1",
            "notes": "DOB and phone are available." if has_demographics else "Confirm DOB and phone before rooming."
        },
        "booking": {
            "label": "Booking",
            "status": booking_status,
            "assignee": "Nurse 2",
            "notes": note or f"Scheduled for {appointment_date_display} at {appointment_time}."
        }
    }

    completed_steps = sum(1 for item in checklist_payload.values() if item["status"] == "Completed")
    clearance = int((completed_steps / len(checklist_payload)) * 100)
    actionable_steps = []
    if booking_status == "Blocked":
        actionable_steps.append("Front desk must verify the appointment status because the scheduler row is marked Needs Review.")
    if insurance_missing:
        actionable_steps.append("Intern 1 must confirm payer/insurance because the scheduler insurance column says (none).")
    if not patient_known:
        actionable_steps.append("Intern 2 must link or create the patient chart from the scheduler appointment.")
    if not has_demographics:
        actionable_steps.append("Nurse 1 must confirm DOB and phone before rooming.")
    if note:
        actionable_steps.append(f"Scheduler note: {note}")
    if not actionable_steps:
        actionable_steps.append("Appointment is scheduled and core intake data is linked. Verify arrival paperwork before rooming.")

    notes = patient.get("notes_summaries", []) or []
    notes_text = " ".join(notes).lower()
    case_type = "New Patient" if visit_type.upper().startswith("NP") else "Established"
    clinical_summary = (
        f"{name} is scheduled for {appointment_date_display} at {appointment_time} with {provider}. "
        f"Visit type: {visit_type}. Scheduler insurance: {schedule_insurance or '(none)'}. "
        f"Local chart match: {'yes' if patient_known else 'no'}. "
        f"{('Scheduler note: ' + note) if note else ''}"
    ).strip()

    billing_clearance = billing_clearance_analysis(
        {**patient, "name": name, "surgeryStatus": patient.get("surgeryStatus", schedule_status)},
        p_tasks,
        resource_status,
        case_type,
        insurance_display,
        notes_text,
    )
    if insurance_missing:
        billing_clearance.update({
            "summary": "Scheduler insurance is not listed.",
            "owner": "Front Desk / Intern 1",
            "confidence": "High",
            "blockers": ["Scheduler insurance column says (none)."],
            "missingInfo": ["Insurance or payer name.", "Policy/member details if applicable."],
            "nextSteps": ["Confirm payer with patient before arrival or at check-in.", "Update scheduler and patient profile once confirmed."],
        })
    elif schedule_status == "Needs Review":
        billing_clearance.update({
            "summary": "Scheduler status needs review before rooming.",
            "owner": "Front Desk",
            "confidence": "High",
            "blockers": ["Scheduler row is marked Needs Review."],
            "missingInfo": ["Confirm whether the appointment is active, canceled, or requires reschedule."],
            "nextSteps": ["Verify appointment status in SIS Complete.", "Update the dashboard checklist once confirmed."],
        })
    elif not patient_known:
        billing_clearance.update({
            "summary": "Scheduler appointment is not linked to a local patient profile.",
            "owner": "Intern 2",
            "confidence": "High",
            "blockers": ["No matching local patient record was found."],
            "missingInfo": ["Linked patient chart.", "DOB and phone from SIS Complete."],
            "nextSteps": ["Create or link the patient profile before the visit.", "Confirm demographics at check-in."],
        })
    else:
        billing_clearance.update({
            "summary": "No schedule-level billing blocker is visible.",
            "owner": "Front Desk",
            "confidence": "Medium",
            "blockers": ["No insurance blocker is visible in the schedule row."],
            "missingInfo": ["Drive folder and case summary are checked on demand when staff open those resources."],
            "nextSteps": ["Verify arrival paperwork and update any missing demographics before rooming."],
        })

    return {
        "name": name,
        "appointmentName": appt.get("appointmentName", name),
        "appointmentDate": appointment_date,
        "appointmentDateDisplay": appointment_date_display,
        "appointmentTime": appointment_time,
        "provider": provider,
        "visitType": visit_type,
        "schedulerInsurance": schedule_insurance or "(none)",
        "scheduleStatus": schedule_status,
        "scheduleNote": note,
        "accepted": bool(appt.get("accepted", False)),
        "dob": patient.get("dob", ""),
        "phone": patient.get("phone", ""),
        "email": patient.get("email", ""),
        "caseType": case_type,
        "insurance": insurance_display,
        "clearance": clearance,
        "clinicalSummary": clinical_summary,
        "folderUrl": resource_status["folder"].get("url", ""),
        "summaryDocUrl": resource_status["summary"].get("url", ""),
        "surgeryStatus": patient.get("surgeryStatus", schedule_status),
        "notes": notes,
        "checklist": checklist_payload,
        "actionableSteps": actionable_steps,
        "holdupReason": billing_clearance["summary"],
        "billingClearance": billing_clearance,
        "resourceStatus": resource_status,
    }

def _scheduled_upcoming_payloads(patients, p_tasks_map):
    schedule = _load_upcoming_schedule()
    appointments = schedule.get("appointments", [])
    return [_schedule_patient_payload(appt, schedule, patients, p_tasks_map) for appt in appointments]


# Ensure local outputs directory exists
os.makedirs(OUTPUT_PARENT_DIR, exist_ok=True)

def koko_status_payload():
    """Return Koko runtime status without making a paid model call."""
    provider_override = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if provider_override == "gemini":
        key_source = next((k for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GPT_API_KEY") if os.environ.get(k)), "GEMINI_API_KEY")
    elif provider_override == "openai":
        key_source = next((k for k in ("GPT_API_KEY", "OPENAI_API_KEY") if os.environ.get(k)), "GPT_API_KEY")
    else:
        key_source = next((k for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GPT_API_KEY", "OPENAI_API_KEY") if os.environ.get(k)), "GEMINI_API_KEY")

    api_key = os.environ.get(key_source, "")
    provider = "gemini" if key_source in {"GEMINI_API_KEY", "GOOGLE_API_KEY"} or provider_override == "gemini" else "openai"
    endpoint = os.environ.get("GPT_ENDPOINT", "https://api.openai.com/v1/chat/completions")
    normalized_key = api_key.strip().strip('"').strip("'").lower()
    placeholder_key = (
        not normalized_key
        or normalized_key.startswith("sk-...")
        or "your_api" in normalized_key
        or "your-openai-key" in normalized_key
        or "your-gemini-key" in normalized_key
        or normalized_key in {"your_api_key_here", "your-openai-key-here", "your-gemini-key-here", "your-key-here", "replace_me"}
    )
    configured = bool(api_key and not placeholder_key)
    key_shape_ok = bool(provider == "gemini" or (api_key.startswith(("sk-", "sess-")) and len(api_key) >= 35))

    if not KOKO_GPT_AVAILABLE:
        state = "offline"
        message = "gpt_client.py could not be imported."
    elif not configured:
        state = "offline"
        message = "Koko needs a real API key. Set GEMINI_API_KEY, GPT_API_KEY, or OPENAI_API_KEY in .env, then restart the server."
    elif "api.openai.com" in endpoint and not key_shape_ok:
        state = "degraded"
        message = "GPT_API_KEY is present but does not look like a valid OpenAI key."
    else:
        state = "online"
        message = "Koko endpoint is available and AI credentials are configured."

    return {
        "id": "koko",
        "name": "Koko AI",
        "status": state,
        "brainLoaded": KOKO_GPT_AVAILABLE,
        "configured": configured,
        "provider": provider,
        "keySource": key_source if configured else None,
        "endpointConfigured": bool(endpoint),
        "model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash") if provider == "gemini" else os.environ.get("GPT_MODEL", "gpt-4o"),
        "message": message,
    }

def agent_health_payload():
    """Conservative live status map for visible dashboard agents."""
    koko = koko_status_payload()
    patients_available = os.path.exists(os.path.join(SCRATCH_DIR, "patient_database.json")) or os.path.exists(os.path.join(SCRATCH_DIR, "calls.json"))
    logs_available = os.path.exists(os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log"))
    archives_available = os.path.exists(OUTPUT_PARENT_DIR)

    return [
        {
            "id": "intake",
            "name": "Intake Agent",
            "status": "online" if patients_available else "degraded",
            "message": "Live intake database available." if patients_available else "Using sandbox fallback because no scraped intake database was found.",
        },
        {
            "id": "scheduling",
            "name": "Scheduling Agent",
            "status": "offline",
            "message": "No live calendar or WEBeDoctor scheduling connector is configured.",
        },
        {
            "id": "scribe",
            "name": "Scribe Agent",
            "status": "offline",
            "message": "No dictation upload listener is configured.",
        },
        {
            "id": "previsit",
            "name": "Pre-Visit Briefing Agent",
            "status": "idle",
            "message": "Standby only. No scheduled pre-visit briefing job is running.",
        },
        {
            "id": "coding",
            "name": "Coding Suggestion Agent",
            "status": koko["status"],
            "message": "Uses Koko's GPT brain for coding suggestions. " + koko["message"],
        },
        {
            "id": "claim-scrubber",
            "name": "Claim Scrubber Agent",
            "status": "idle",
            "message": "Standby only. Nightly claim scrub job is not scheduled in this local server.",
        },
        {
            "id": "billing-clearance",
            "name": "Billing Clearance Agent",
            "status": "online" if patients_available else "degraded",
            "message": "Explains unpaid-case hold-ups from local patient notes, task status, and resource availability." if patients_available else "Needs patient database before it can explain billing hold-ups.",
        },
        {
            "id": "prior-auth",
            "name": "Prior Auth Agent",
            "status": "offline" if not APPS_SCRIPT_WEBHOOK_URL else "degraded",
            "message": "No prior-auth portal connector/webhook is configured." if not APPS_SCRIPT_WEBHOOK_URL else "Webhook configured; portal automation still needs verification.",
        },
        {
            "id": "denial-triage",
            "name": "Denial Triage Agent",
            "status": koko["status"],
            "message": "Appeal drafting depends on Koko. " + koko["message"],
        },
        {
            "id": "follow-up",
            "name": "Follow-up Agent",
            "status": "online" if logs_available else "degraded",
            "message": "Local recovery workflow/logs are available." if logs_available else "Follow-up workflow can run locally, but no operations log was found.",
        },
        {
            "id": "refill",
            "name": "Refill Triage Agent",
            "status": "offline",
            "message": "No pharmacy/refill inbox connector is configured.",
        },
        {
            "id": "inbound-router",
            "name": "Inbound Router Agent",
            "status": "degraded" if archives_available else "offline",
            "message": "Local archive explorer is available, but no live portal message connector is configured." if archives_available else "No portal routing source is configured.",
        },
    ]

class ClinicalDashboardHandler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def send_cors_headers(self):
        """Sets standard CORS headers to permit standalone index loads fetching local endpoints."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        """Responds cleanly to CORS preflight options requests."""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        # 1. Route: Main Dashboard Page
        if path == "/" or path == "/index.html":
            dashboard_path = os.path.join(WORKSPACE_DIR, "dashboard.html")
            try:
                with open(dashboard_path, "r", encoding="utf-8") as f:
                    html_content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))
            except Exception as e:
                self.send_error(500, f"Error loading dashboard: {str(e)}")

        # 1.2 Route: Serve extracted patient data JS (PHI — served from scratch, never repo)
        elif path == "/patients_data.js":
            data_path = os.path.join(SCRATCH_DIR, "patients_data.js")
            if os.path.exists(data_path):
                with open(data_path, "rb") as f:
                    payload = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_json({"error": "patients_data.js not generated yet — run extract_patients_data.py"}, status=404)

        # 1.3 Route: Browser favicon probe (avoid noisy 404s)
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_cors_headers()
            self.end_headers()

        # 1.5 Route: Serve PNG Images (like avatars)
        elif path.endswith(".png"):
            file_name = os.path.basename(path)
            file_path = os.path.join(WORKSPACE_DIR, file_name)
            if os.path.exists(file_path):
                try:
                    with open(file_path, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_cors_headers()
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as e:
                    self.send_error(500, f"Error loading image: {str(e)}")
            else:
                self.send_error(404, "Image not found.")

        # 2. Route: Retrieve Real Scraped Phone Intake Patients (paginated)
        elif path == "/api/patients":
            database_path = os.path.join(SCRATCH_DIR, "patient_database.json")
            patients = []

            try:
                if os.path.exists(database_path):
                    with open(database_path, "r", encoding="utf-8") as f:
                        patients = json.load(f)
                else:
                    # Fallback to general calls.json if primary profile database isn't scraped
                    calls_path = os.path.join(SCRATCH_DIR, "calls.json")
                    if os.path.exists(calls_path):
                        with open(calls_path, "r", encoding="utf-8") as f:
                            patients = json.load(f)

                # No records -> honest empty list. (Previously injected a fake
                # "Alfredo Silva" demo patient that staff couldn't tell apart
                # from a real referral.)

                # Parse query params for search, page, and limit
                query_params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                search_q = query_params.get("q", [None])[0]
                try:
                    page = int(query_params.get("page", [0])[0])
                except (ValueError, TypeError):
                    page = 0
                try:
                    limit = int(query_params.get("limit", [50])[0])
                except (ValueError, TypeError):
                    limit = 50

                # Apply case-insensitive search across name, insurance, and attorney
                if search_q:
                    search_lower = search_q.lower()
                    def _matches(p):
                        return (
                            search_lower in str(p.get("name", "")).lower()
                            or search_lower in str(p.get("insurance", "")).lower()
                            or search_lower in str(p.get("attorney", "")).lower()
                        )
                    patients = [p for p in patients if _matches(p)]

                total = len(patients)
                pages = (total + limit - 1) // limit if limit > 0 else 1
                start = page * limit
                page_patients = patients[start:start + limit]

                self.send_json({"patients": page_patients, "total": total, "page": page, "pages": pages, "limit": limit})
            except Exception as e:
                self.send_error(500, f"Error loading patient list: {str(e)}")

        # 2.5 Route: Per-patient notes (GET)
        elif path == "/api/notes":
            query_params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            patient_name = query_params.get("patient", [None])[0]
            if not patient_name:
                self.send_json({"error": "Missing patient param"}, status=400)
            else:
                notes_path = os.path.join(SCRATCH_DIR, "patient_notes.json")
                statuses_path = os.path.join(SCRATCH_DIR, "surgery_statuses.json")
                notes_db = {}
                statuses_db = {}
                try:
                    if os.path.exists(notes_path):
                        with open(notes_path, "r", encoding="utf-8") as f:
                            notes_db = json.load(f)
                except Exception:
                    pass
                try:
                    if os.path.exists(statuses_path):
                        with open(statuses_path, "r", encoding="utf-8") as f:
                            statuses_db = json.load(f)
                except Exception:
                    pass
                self.send_json({"note": notes_db.get(patient_name, ""), "status": statuses_db.get(patient_name, "")})

        # 3. Route: Retrieve Real-time Background Execution Logs
        elif path == "/api/logs":
            # Stream the latest 10 rows from our local log file
            log_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
            log_lines = []
            if os.path.exists(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        log_lines = f.readlines()[-15:]
                except Exception:
                    pass

            if not log_lines:
                log_lines = ["[SYSTEM] No agent activity logged yet."]

            response_data = json.dumps([line.strip() for line in log_lines])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(response_data.encode("utf-8"))

        # 3.4 Route: Retrieve SIS Sync 2FA/Auth State
        elif path == "/api/sis/status":
            state_path = os.path.join(SCRATCH_DIR, "sync_state.json")
            state_data = {"status": "idle"}
            if os.path.exists(state_path):
                try:
                    with open(state_path, "r", encoding="utf-8") as f:
                        state_data = json.load(f)
                except Exception:
                    pass
            response_data = json.dumps(state_data)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(response_data.encode("utf-8"))

        # 3.5 Route: Retrieve dynamic Google Drive Archive Folder Explorer structure
        elif path == "/api/archives":
            archive_data = []
            try:
                if os.path.exists(OUTPUT_PARENT_DIR):
                    for folder in os.listdir(OUTPUT_PARENT_DIR):
                        folder_path = os.path.join(OUTPUT_PARENT_DIR, folder)
                        if os.path.isdir(folder_path):
                            files_list = []
                            for root, dirs, files in os.walk(folder_path):
                                for f in files:
                                    rel_path = os.path.relpath(os.path.join(root, f), folder_path)
                                    files_list.append(rel_path)
                            archive_data.append({
                                "folder_name": folder,
                                "files": files_list
                            })
                response_data = json.dumps(archive_data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(response_data.encode("utf-8"))
            except Exception as e:
                self.send_error(500, f"Error reading archives: {str(e)}")

        elif path == "/api/health":
            self.send_json({
                "status": "ok",
                "service": "Clinical Dashboard API",
                "port": PORT,
                "serverTime": datetime.now().isoformat(timespec="seconds"),
                "koko": koko_status_payload(),
                "agents": agent_health_payload(),
                "endpoints": ["/api/patients", "/api/logs", "/api/archives", "/api/koko", "/api/koko/status", "/api/agents/health"],
            })

        elif path == "/api/koko/status":
            self.send_json(koko_status_payload())

        elif path == "/api/agents/health":
            self.send_json({"agents": agent_health_payload(), "koko": koko_status_payload()})

        elif path == "/api/settings":
            self.send_json({
                "appsScriptWebhookUrl": APPS_SCRIPT_WEBHOOK_URL,
                "serverRuntime": "active",
                "scratchDir": SCRATCH_DIR,
                "workspaceDir": WORKSPACE_DIR,
            })

        elif path == "/api/automation/status":
            self.send_json(_automation_status_payload())

        elif path == "/api/automation/loops":
            self.send_json(_automation_loop_catalog())

        elif path == "/api/clinical-os/modules":
            self.send_json(_clinical_os_module_catalog())

        elif path == "/api/manager/review":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = int((query.get("limit") or ["200"])[0] or 200)
            self.send_json(_manager_review_payload(limit=limit))

        elif path == "/api/upcoming-schedule":
            patients = _load_patient_database()
            tasks_path = os.path.join(SCRATCH_DIR, "team_tasks.json")
            tasks = []
            if os.path.exists(tasks_path):
                try:
                    with open(tasks_path, "r", encoding="utf-8") as f:
                        tasks = json.load(f)
                except Exception:
                    pass
            p_tasks_map = {}
            for t in tasks:
                p_name = t.get("patientName")
                if p_name not in p_tasks_map:
                    p_tasks_map[p_name] = []
                p_tasks_map[p_name].append(t)
            schedule = _load_upcoming_schedule()
            self.send_json({
                "schedule": schedule,
                "upcoming": _scheduled_upcoming_payloads(patients, p_tasks_map),
            })

        elif path == "/api/intake-queue":
            intake_queue_path = os.path.join(SCRATCH_DIR, "intake_queue.json")
            payload = None
            if os.path.exists(intake_queue_path):
                try:
                    with open(intake_queue_path, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                except Exception:
                    payload = None
            if isinstance(payload, dict):
                payload["available"] = True
                self.send_json(payload)
            else:
                self.send_json({
                    "available": False,
                    "hint": "run: python3 intake_queue_sync.py (and share the log sheet with the service account)",
                })

        elif path == "/api/patient-resource":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            patient_name = (query.get("name") or [""])[0].strip()
            resource_type = (query.get("type") or [""])[0].strip()
            if not patient_name or not resource_type:
                self.send_error(400, "Missing patient resource name or type.")
                return
            self.send_json(patient_resource_payload(patient_name, resource_type))

        elif path == "/api/tasks":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            include_all_tasks = (query.get("all") or ["0"])[0] in ("1", "true", "yes")
            task_role = (query.get("role") or [""])[0].strip().lower()
            try:
                task_limit = max(20, min(1000, int((query.get("limit") or ["300"])[0])))
            except Exception:
                task_limit = 300
            tasks_path = os.path.join(SCRATCH_DIR, "team_tasks.json")
            tasks = []

            # Generate AT MOST ONE honest next-action task per patient.
            # The old generator fabricated 6 tasks per patient with fictional
            # status notes and bulk role assignments — that buried real work
            # and made the queues untrustworthy. Auto tasks are now: a single
            # status-derived next action, unassigned, clearly marked.
            NEXT_ACTION_BY_STATUS = {
                "Denied": ("denial_followup", "Denial Review & Appeal Prep"),
                "Issue": ("resolve_issue", "Resolve Open Case Issue"),
                "Unresolved": ("resolve_issue", "Resolve Open Case Issue"),
                "Pending": ("auth_scheduling", "Verify Authorization & Scheduling"),
                "Pending Auth": ("auth_scheduling", "Verify Authorization & Scheduling"),
                "Ready For Pre-Op": ("preop_confirmation", "Confirm Pre-Op Clearance & Appointment"),
                "In Progress": ("case_progress", "Confirm Next Appointment & Documentation"),
            }

            def _gen_default_tasks(p_name, surgery_status=""):
                key, label = NEXT_ACTION_BY_STATUS.get(
                    surgery_status, ("intake_review", "Intake Review & Next-Step Triage"))
                due = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")
                return [{
                    "id": f"task_auto_{_patient_slug(p_name).lower()}_{key}",
                    "patientName": p_name,
                    "taskKey": key,
                    "taskLabel": label,
                    "assignee": "Unassigned",
                    "status": "Pending",
                    "notes": "Auto-suggested from case status. Assign an owner to make it real work.",
                    "autoGenerated": True,
                    "dueDate": due,
                    "updatedAt": datetime.now().isoformat() + "-04:00"
                }]

            def _task_sort_key(task):
                status_rank = {"Blocked": 0, "Pending": 1, "In Progress": 2, "Completed": 3}
                assignee = task.get("assignee") or "Unassigned"
                assignee_rank = 0 if assignee != "Unassigned" else 1
                # Overdue / soonest-due work floats to the top within a status.
                due = task.get("dueDate") or "9999-12-31"
                return (
                    status_rank.get(task.get("status"), 4),
                    due,
                    assignee_rank,
                    assignee,
                    task.get("patientName") or "",
                    task.get("taskKey") or task.get("id") or "",
                )

            def _dedupe_tasks(task_list):
                deduped = {}
                for task in task_list:
                    key = (
                        (task.get("patientName") or "").strip().lower(),
                        (task.get("taskKey") or task.get("taskLabel") or task.get("id") or "").strip().lower(),
                    )
                    existing = deduped.get(key)
                    if not existing or _task_sort_key(task) < _task_sort_key(existing):
                        deduped[key] = task
                return list(deduped.values())

            try:
                if os.path.exists(tasks_path):
                    with open(tasks_path, "r", encoding="utf-8") as f:
                        tasks = json.load(f)

                # Ensure all active patients in patient_database have default tasks generated
                db_path = os.path.join(SCRATCH_DIR, "patient_database.json")
                if os.path.exists(db_path):
                    try:
                        with open(db_path, "r", encoding="utf-8") as f:
                            pts = json.load(f)
                        priority_names = set()  # no demo-name bias — active status decides
                        active_statuses = {"Denied", "Issue", "Pending", "Pending Auth", "Unresolved", "Ready For Pre-Op", "In Progress"}
                        seed_names = []
                        seed_status = {}
                        for p in pts:
                            name = p.get("name")
                            if not name:
                                continue
                            if name in priority_names or p.get("surgeryStatus") in active_statuses:
                                seed_names.append(name)
                                seed_status[name] = p.get("surgeryStatus") or ""
                            if len(seed_names) >= 80:
                                break
                        all_p_names = list(dict.fromkeys(seed_names))

                        existing_p_names = set([t.get("patientName") for t in tasks])
                        missing_p_names = [n for n in all_p_names if n not in existing_p_names]

                        if missing_p_names:
                            for name in missing_p_names:
                                tasks.extend(_gen_default_tasks(name, seed_status.get(name, "")))
                            with open(tasks_path, "w", encoding="utf-8") as f:
                                json.dump(tasks, f, indent=2)
                    except Exception as ex:
                        logger.error(f"Error ensuring all patients have tasks: {ex}")

                tasks = _dedupe_tasks(tasks)
                if not include_all_tasks:
                    actionable = [
                        t for t in tasks
                        if t.get("status") != "Completed"
                        and (t.get("assignee") != "Unassigned" or t.get("autoGenerated"))
                    ]
                    tasks = actionable or tasks
                    if task_role in ("nurse", "intern"):
                        role_map = _staff_role_map()
                        prefix = task_role.title() + " "
                        tasks = [
                            t for t in tasks
                            if role_map.get(str(t.get("assignee", ""))) == task_role
                            or str(t.get("assignee", "")).startswith(prefix)
                        ]
                    tasks = sorted(tasks, key=_task_sort_key)[:task_limit]

                self.send_json(tasks)
            except Exception as e:
                self.send_error(500, f"Error loading tasks: {str(e)}")

        elif path == "/api/staff":
            self.send_json({"staff": _load_staff()})

        elif path == "/api/reconciliation":
            # The "discrepancy finder": cross-checks every connected data
            # source and reports records that disagree or are silently
            # incomplete — so staff find problems before the doctor does.
            try:
                patients = _load_patient_database()
                ledger_path = os.path.join(SCRATCH_DIR, "billing_ledger.json")
                ledger = []
                if os.path.exists(ledger_path):
                    with open(ledger_path, "r", encoding="utf-8") as f:
                        ledger = json.load(f)
                followups = _load_followups()
                checks = []

                def add_check(check_id, title, severity, items, advice):
                    checks.append({
                        "id": check_id, "title": title, "severity": severity,
                        "count": len(items), "items": items[:50], "advice": advice,
                    })

                # 1. Surgery happened (or case looks active) but billing is a blank
                surgical_statuses = {"Performed", "Billed", "Completed", "Paid", "Ready for Bill"}
                no_billing = [
                    {"patient": p.get("name"), "surgeryStatus": p.get("surgeryStatus", "")}
                    for p in patients
                    if p.get("surgeryStatus") in surgical_statuses
                    and (p.get("billing") or {}).get("dataMissing")
                ]
                add_check("surgery-no-billing", "Surgical case with NO billing data on file",
                          "high", no_billing,
                          "These cases look performed/billed in the pipeline but no money record exists in any source. Pull them in the next AR report import — this is the 'Brian Rocotti' class of gap.")

                # 2. Payments recorded but no EOB link in the ledger row
                missing_eob = []
                for row in ledger:
                    if not isinstance(row, dict):
                        continue
                    name = row.get("Name")
                    if not name or str(name).strip().lower() in ("", "name"):
                        continue
                    for amt_key, eob_key, side in (
                        ("Surgery Center Paid Amount ($)", "Surgery Center EOB Link", "Surgery Center"),
                        ("Atlantic Paid Amount ($)", "Atlantic EOB Link", "Atlantic"),
                    ):
                        raw_amt = str(row.get(amt_key, "")).replace("$", "").replace(",", "").strip()
                        try:
                            amt = float(raw_amt)
                        except ValueError:
                            continue
                        if amt > 0 and not str(row.get(eob_key, "")).strip():
                            missing_eob.append({"patient": str(name).strip(), "side": side,
                                                "paid": amt, "dos": str(row.get("DOS", "")).strip()})
                add_check("paid-no-eob", "Payment recorded but EOB link missing",
                          "high", missing_eob,
                          "Money came in but there is no EOB on file. Locate the EOB in the payer portal / SIS and attach the link to the ledger row.")

                # 3. Duplicate patient records (same normalized name)
                from collections import Counter
                def norm(n):
                    return re.sub(r"[^a-z]", "", str(n or "").lower().split(" dob")[0])
                name_counts = Counter(norm(p.get("name")) for p in patients if p.get("name"))
                dupes = []
                seen = set()
                for p in patients:
                    k = norm(p.get("name"))
                    if k and name_counts[k] > 1 and k not in seen:
                        seen.add(k)
                        dupes.append({"patient": p.get("name"), "copies": name_counts[k]})
                add_check("duplicate-patients", "Duplicate patient records",
                          "medium", dupes,
                          "Multiple records share one normalized name. Merge them or confirm they are different people (check DOB).")

                # 4. Dirty patient names (DOB or dates embedded in the name field)
                dirty = [{"patient": p.get("name")} for p in patients
                         if p.get("name") and re.search(r"\d{1,2}/\d{1,2}/\d{2,4}|DOB", p.get("name", ""))]
                add_check("dirty-names", "Patient name field contains DOB/date text",
                          "medium", dirty,
                          "Imports stuffed dates into the name field. Split into name + dob so matching across systems works.")

                # 5. Overdue follow-up calls
                today = date.today()
                overdue_fu = [
                    {"patient": f.get("patientName"), "call": f.get("title"),
                     "dueDate": f.get("dueDate"), "assignee": f.get("assignee")}
                    for f in followups if _followup_bucket(f, today) == "overdue"
                ]
                add_check("overdue-followups", "Overdue patient follow-up calls",
                          "high", overdue_fu,
                          "These outreach calls are past due. Assign owners in the Nurse Hub — every missed follow-up risks patient trust and revenue.")

                # 6. Active surgical patients missing contact info
                no_contact = [
                    {"patient": p.get("name"), "surgeryStatus": p.get("surgeryStatus", "")}
                    for p in patients
                    if p.get("surgeryStatus") in surgical_statuses
                    and not (p.get("phone") or "").strip()
                ]
                add_check("missing-contact", "Surgical patient with no phone number",
                          "medium", no_contact,
                          "Follow-up calls are impossible without a number. Pull contact info from the referral email or intake form.")

                # 7. Ghost patients — no DOS, no billing data, no contact info,
                # no meaningful notes. Likely referrals that never came in;
                # candidates for archiving so the working lists stay clean.
                def _is_ghost(p):
                    if p.get("dateOfService"):
                        return False
                    b = p.get("billing") or {}
                    if b and not b.get("dataMissing") and (b.get("payments") or b.get("balance")):
                        return False
                    if (p.get("phone") or "").strip() or (p.get("email") or "").strip():
                        return False
                    notes = " ".join(p.get("notes_summaries") or [])
                    # import/sync boilerplate doesn't count as signal
                    if re.sub(r"(Imported from|Synced from)[^.]*\.?", "", notes).strip():
                        return False
                    return True
                ghosts = [{"patient": p.get("name"), "intakeDate": p.get("intakeDate", "")}
                          for p in patients if p.get("name") and _is_ghost(p)]
                add_check("ghost-patients", "Ghost patients (no visit, no billing, no contact info)",
                          "low", ghosts,
                          "These records carry no clinical or financial signal — likely referrals that never came in. Review and archive them in bulk to declutter the working lists.")

                high = sum(c["count"] for c in checks if c["severity"] == "high")
                total = sum(c["count"] for c in checks)
                self.send_json({
                    "generatedAt": datetime.now().isoformat(),
                    "summary": {"total": total, "high": high},
                    "checks": checks,
                })
            except Exception as e:
                logger.error(f"Reconciliation scan failed: {e}")
                self.send_error(500, f"Reconciliation scan failed: {str(e)}")

        elif path == "/api/followups":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            assignee_filter = (query.get("assignee") or [""])[0].strip()
            followups = _load_followups()
            today = date.today()
            today_str = today.strftime("%Y-%m-%d")
            summary = {"overdue": 0, "dueToday": 0, "upcoming": 0, "completedToday": 0}
            enriched = []
            for fu in followups:
                bucket = _followup_bucket(fu, today)
                fu = dict(fu)
                fu["bucket"] = bucket
                if bucket == "overdue":
                    summary["overdue"] += 1
                elif bucket == "due-today":
                    summary["dueToday"] += 1
                elif bucket == "upcoming":
                    summary["upcoming"] += 1
                elif bucket == "completed" and str(fu.get("completedAt", ""))[:10] == today_str:
                    summary["completedToday"] += 1
                if assignee_filter and fu.get("assignee") != assignee_filter:
                    continue
                enriched.append(fu)
            bucket_rank = {"overdue": 0, "due-today": 1, "upcoming": 2, "completed": 3}
            enriched.sort(key=lambda f: (bucket_rank.get(f["bucket"], 4), f.get("dueDate") or "9999"))
            self.send_json({"followups": enriched, "summary": summary, "protocol": _followup_protocol()})

        elif path == "/api/faxes/digest":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                day_filter = (query.get("filter") or ["today"])[0]
                limit = int((query.get("limit") or ["200"])[0])
                if day_filter not in ("today", "yesterday", "review", "all"):
                    day_filter = "today"
                self.send_json(_fax_digest_payload(day_filter=day_filter, limit=limit))
            except Exception as e:
                logger.error(f"Fax digest failed: {e}")
                self.send_error(500, f"Fax digest failed: {str(e)}")

        elif path == "/api/billing":
            # Calculates true billing stats from merged billing fields in patient_database.json
            db_path = os.path.join(SCRATCH_DIR, "patient_database.json")
            patients = []
            if os.path.exists(db_path):
                try:
                    with open(db_path, "r", encoding="utf-8") as f:
                        patients = json.load(f)
                except Exception as ex:
                    logger.error(f"Error reading patient_database.json in /api/billing: {ex}")

            billing_patients = []
            total_collected = 0.0
            total_pending = 0.0
            fully_paid_count = 0
            owes_us_count = 0
            partially_paid_count = 0
            denied_count = 0
            pending_count = 0
            no_balance_count = 0
            not_found_count = 0

            for p in patients:
                b = p.get("billing")
                if b:
                    payments = b.get("payments", 0.0)
                    balance = b.get("balance", 0.0)
                    status = b.get("status", "Unknown")
                    raw_status = b.get("raw_status", "")
                    
                    total_collected += payments
                    total_pending += balance
                    
                    if status == "Fully Paid":
                        fully_paid_count += 1
                    elif status == "Owes Us" or status == "Active Balance" or str(status).startswith("Overdue"):
                        owes_us_count += 1
                    elif status == "Partially Paid":
                        partially_paid_count += 1
                    elif status == "Denied":
                        denied_count += 1
                    elif status == "Pending":
                        pending_count += 1
                    elif status == "No Balance":
                        no_balance_count += 1
                    elif status in ("Not Found", "No Data"):
                        not_found_count += 1

                    billing_patients.append({
                        "name": p.get("name"),
                        "status": status,
                        "payments": payments,
                        "balance": balance,
                        "dataMissing": bool(b.get("dataMissing")) or status in ("Not Found", "No Data"),
                        "raw_status": raw_status,
                        "insurance": p.get("insurance", ""),
                        "attorney": p.get("attorney", ""),
                        "phone": p.get("phone", "")
                    })

            # Per-CPT claim stats computed live from the real ledger rows —
            # replaces the hand-typed snapshot table in the billing modal.
            cpt_stats = {}
            try:
                ledger_path = os.path.join(SCRATCH_DIR, "billing_ledger.json")
                if os.path.exists(ledger_path):
                    with open(ledger_path, "r", encoding="utf-8") as f:
                        ledger_rows = json.load(f)
                    for row in ledger_rows:
                        if not isinstance(row, dict):
                            continue
                        cpt_raw = str(row.get("CPT Code(s)", "") or "").strip()
                        if not cpt_raw:
                            continue
                        statuses = f"{row.get('Surgery Center Status', '')} {row.get('Atlantic Status', '')}".lower()
                        paid_amt = 0.0
                        for amt_key in ("Surgery Center Paid Amount ($)", "Atlantic Paid Amount ($)"):
                            try:
                                paid_amt += float(str(row.get(amt_key, "")).replace("$", "").replace(",", "") or 0)
                            except ValueError:
                                pass
                        is_paid = bool(re.search(r"(?<!un)\bpaid\b", statuses)) or paid_amt > 0
                        is_denied = "denied" in statuses
                        for code_part in cpt_raw.split(","):
                            code_match = re.match(r"\s*(\d{5})", code_part)
                            if not code_match:
                                continue
                            code = code_match.group(1)
                            label = code_part.split("-", 1)[1].strip() if "-" in code_part else ""
                            s = cpt_stats.setdefault(code, {"code": code, "label": label, "claims": 0, "paid": 0, "denied": 0})
                            if label and not s["label"]:
                                s["label"] = label
                            s["claims"] += 1
                            if is_paid:
                                s["paid"] += 1
                            elif is_denied:
                                s["denied"] += 1
            except Exception as ex:
                logger.error(f"CPT stat aggregation failed: {ex}")
            cpt_list = []
            for s in cpt_stats.values():
                decided = s["paid"] + s["denied"]
                s["approvalRate"] = round(s["paid"] / decided * 100) if decided else None
                cpt_list.append(s)
            cpt_list.sort(key=lambda s: -s["claims"])

            # Sort billing patients by payments descending
            top_patients = sorted(billing_patients, key=lambda x: -x["payments"])
            
            total_patients = len(patients)
            # "Tracked cases" = patients with REAL billing activity only. Excludes the
            # 1,700+ "No Balance" defaults and "Not Found" lookups so management stats
            # aren't diluted by empty profiles.
            billed_cases = (fully_paid_count + owes_us_count + partially_paid_count
                            + denied_count + pending_count)
            total_billable = total_collected + total_pending
            collection_rate = (total_collected / total_billable * 100) if total_billable > 0 else 0.0
            case_paid_rate = (fully_paid_count / billed_cases * 100) if billed_cases > 0 else 0.0

            self.send_json({
                "collected": round(total_collected, 2),                 # money in
                "pending": round(total_pending, 2),                     # money owed
                "estBillable": round(total_billable, 2),
                "estUncollected": round(total_pending, 2),
                "collectionRate": round(collection_rate, 1),            # $ collected / $ billed
                "casePaidRate": round(case_paid_rate, 1),               # % of billed cases fully paid
                "totalCasesTracked": billed_cases,                      # cases with real billing activity
                "billedCases": billed_cases,
                "totalPatients": total_patients,                        # everyone in the DB
                "fullyPaidCount": fully_paid_count,
                "owesUsCount": owes_us_count,
                "partiallyPaidCount": partially_paid_count,
                "deniedCount": denied_count,
                "pendingCount": pending_count,
                "noBalanceCount": no_balance_count,
                "notFoundCount": not_found_count,
                # Compatibility keys for existing dashboard code
                "scPaidCount": fully_paid_count,
                "scPendingCount": owes_us_count + partially_paid_count + pending_count,
                "scDeniedCount": denied_count + not_found_count,
                "atlanticPaid": 0.0,
                "surgeryCenterPaid": round(total_collected, 2),
                "avgPerCase": round((total_collected / fully_paid_count) if fully_paid_count > 0 else 0, 2),
                "paidPatientCount": len([x for x in billing_patients if x["payments"] > 0]),
                "topPatients": top_patients[:50],
                "allBillingPatients": billing_patients,
                "cptStats": cpt_list[:12],
                "ledgerRowCount": total_patients
            })

        elif path == "/api/monday-meeting":
            try:
                db_path = os.path.join(SCRATCH_DIR, "patient_database.json")
                patients = []
                if os.path.exists(db_path):
                    with open(db_path, "r", encoding="utf-8") as f:
                        patients = json.load(f)

                tasks_path = os.path.join(SCRATCH_DIR, "team_tasks.json")
                tasks = []
                if os.path.exists(tasks_path):
                    try:
                        with open(tasks_path, "r", encoding="utf-8") as f:
                            tasks = json.load(f)
                    except Exception:
                        pass

                # Task seeding is owned by /api/tasks. The meeting route should read
                # the current task set without generating thousands of background rows.

                p_tasks_map = {}
                for t in tasks:
                    p_name = t.get("patientName")
                    if p_name not in p_tasks_map:
                        p_tasks_map[p_name] = []
                    p_tasks_map[p_name].append(t)

                scheduled_upcoming = _scheduled_upcoming_payloads(patients, p_tasks_map)
                if scheduled_upcoming:
                    self.send_json({
                        "upcoming": scheduled_upcoming,
                        "oldCases": [],
                        "schedule": _load_upcoming_schedule(),
                    })
                    return

                upcoming_list = []
                old_cases_list = []

                for p in patients:
                    name = p.get("name", "")
                    p_tasks = p_tasks_map.get(name, [])

                    # Clearance is the real task completion ratio — 0 when no
                    # tasks exist. (Previously invented per-name percentages.)
                    if p_tasks:
                        comp = sum(1 for t in p_tasks if t.get("status") == "Completed")
                        clearance = int((comp / len(p_tasks)) * 100)
                    else:
                        clearance = 0

                    # Find task statuses for Checklist — defaults are honest
                    # "Pending / Unassigned", never fabricated completions.
                    task_status_map = {t.get("taskKey"): t for t in p_tasks}

                    def _check_item(task_key):
                        t = task_status_map.get(task_key, {})
                        return {
                            "status": t.get("status", "Pending"),
                            "assignee": t.get("assignee", "Unassigned"),
                            "notes": t.get("notes", "")
                        }

                    checklist_payload = {
                        "pip": _check_item("benefits_verification"),
                        "records": _check_item("document_collection"),
                        "forms": _check_item("intake_form_prefill"),
                        "booking": _check_item("booking_confirmation")
                    }

                    # Actionable steps derive only from REAL tasks. No tasks =
                    # say so, instead of inventing a 4-step to-do list.
                    actionable_steps = []
                    if p_tasks:
                        step_labels = [
                            ("pip", "PIP verification", "verify PIP/WC benefit limits"),
                            ("records", "Medical records", "obtain medical charts from the referring physician"),
                            ("forms", "Intake forms", "follow up on the outstanding pre-visit form"),
                            ("booking", "Booking confirmation", "confirm the appointment booking"),
                        ]
                        for key, label, action in step_labels:
                            item = checklist_payload[key]
                            if item["status"] != "Completed":
                                step = f"{label}: {item['assignee']} to {action}."
                                if item["notes"]:
                                    step += f" Hold-up: '{item['notes']}'"
                                actionable_steps.append(step)
                        if not actionable_steps:
                            actionable_steps.append("All tracked tasks completed for this patient.")
                    else:
                        actionable_steps.append("No tasks created yet — assign owners on the task board.")

                    # Build summary from real data only
                    summary = p.get("transcript", "")
                    if not summary or summary == "No transcript available.":
                        notes = p.get("notes_summaries", [])
                        summary = " ".join([n for n in notes if len(n) > 30])
                        if not summary:
                            summary = "No intake summary on file for this patient yet."

                    notes_str = " ".join(p.get("notes_summaries", [])).lower()
                    is_wc = ("workmans" in notes_str or "workers" in notes_str
                             or re.search(r"\bwc\b", notes_str) is not None
                             or p.get("type") == "WC")
                    case_type = "Workers' Comp" if is_wc else "PIP/MVA"
                    # Insurance comes from the record or is empty — never guessed.
                    insurance = (p.get("insurance") or "").strip()

                    resource_status = {
                        "folder": patient_resource_payload(name, "folder"),
                        "summary": patient_resource_payload(name, "summary"),
                        "notes": patient_resource_payload(name, "notes"),
                    }
                    folder_url = resource_status["folder"].get("url", "")
                    summary_doc_url = resource_status["summary"].get("url", "")

                    billing_clearance = billing_clearance_analysis(p, p_tasks, resource_status, case_type, insurance, notes_str)
                    holdup_reason = billing_clearance["summary"]
                    blocked_tasks = [t for t in p_tasks if t.get("status") == "Blocked"]
                    if blocked_tasks:
                        # Real blocked tasks are the most specific hold-up signal.
                        holdup_reason = "; ".join([f"{t.get('taskLabel')}: {t.get('notes')}" for t in blocked_tasks])
                        billing_clearance["summary"] = holdup_reason
                    # (Previously: keyword-triggered canned hold-up reasons like
                    # "Missing PIP claim number" overwrote the real analysis —
                    # removed; the billing-clearance summary stands on its own.)

                    patient_payload = {
                        "name": name,
                        "dob": p.get("dob", ""),
                        "phone": p.get("phone", ""),
                        "email": p.get("email", ""),
                        "caseType": case_type,
                        "insurance": insurance,
                        "clearance": clearance,
                        "clinicalSummary": summary,
                        "folderUrl": folder_url,
                        "summaryDocUrl": summary_doc_url,
                        "surgeryStatus": p.get("surgeryStatus", "Intake"),
                        "notes": p.get("notes_summaries", []),
                        "checklist": checklist_payload,
                        "actionableSteps": actionable_steps,
                        "holdupReason": holdup_reason,
                        "billingClearance": billing_clearance
                    }
                    patient_payload["resourceStatus"] = resource_status

                    # Membership comes from real signals only — no hardcoded
                    # "showcase" names forcing specific patients into the board.
                    if "scheduled" in notes_str or "appointment" in notes_str or p.get("surgeryStatus") == "Pending":
                        upcoming_list.append(patient_payload)

                    if p.get("surgeryStatus") in {"Pending", "Issue", "Triage"}:
                        old_cases_list.append(patient_payload)

                # An empty board is the honest answer when nothing is scheduled.
                # (Previously injected a fabricated "Alfredo Silva" demo card.)

                self.send_json({
                    "upcoming": upcoming_list,
                    "oldCases": old_cases_list,
                    "schedule": _load_upcoming_schedule(),
                })
            except Exception as e:
                self.send_error(500, f"Error loading Monday meeting data: {str(e)}")

        elif path == "/api/webedoctor/crawl":
            state_file = os.path.join(SCRATCH_DIR, "crawl_state.json")
            state = {
                "last_index": 0,
                "status": "idle",
                "total_crawled": 0,
                "matched_sis": 0,
                "matched_svigg": 0,
                "not_found": 0,
                "num_workers": 4,
                "timestamp": datetime.now().isoformat()
            }
            if os.path.exists(state_file):
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        state = json.load(f)
                except:
                    pass
            
            # Count total patients in database for progress calculation
            db_path = os.path.join(SCRATCH_DIR, "patient_database.json")
            total_patients = 1999
            if os.path.exists(db_path):
                try:
                    with open(db_path, "r", encoding="utf-8") as f:
                        total_patients = len(json.load(f))
                except:
                    pass
            state["total_patients"] = total_patients
            self.send_json(state)

        else:
            self.send_error(404, "Endpoint not found.")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        # Route: Save per-patient note and/or surgery status
        if path == "/api/notes":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode("utf-8"))
                patient_name = data.get("patient", "").strip()
                if not patient_name:
                    self.send_json({"ok": False, "error": "Missing patient field"}, status=400)
                else:
                    notes_path = os.path.join(SCRATCH_DIR, "patient_notes.json")
                    statuses_path = os.path.join(SCRATCH_DIR, "surgery_statuses.json")
                    notes_db = {}
                    statuses_db = {}
                    try:
                        if os.path.exists(notes_path):
                            with open(notes_path, "r", encoding="utf-8") as f:
                                notes_db = json.load(f)
                    except Exception:
                        pass
                    try:
                        if os.path.exists(statuses_path):
                            with open(statuses_path, "r", encoding="utf-8") as f:
                                statuses_db = json.load(f)
                    except Exception:
                        pass
                    if "note" in data:
                        notes_db[patient_name] = data["note"]
                    if "status" in data:
                        statuses_db[patient_name] = data["status"]
                    os.makedirs(SCRATCH_DIR, exist_ok=True)
                    with open(notes_path, "w", encoding="utf-8") as f:
                        json.dump(notes_db, f, ensure_ascii=False, indent=2)
                    with open(statuses_path, "w", encoding="utf-8") as f:
                        json.dump(statuses_db, f, ensure_ascii=False, indent=2)
                    self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, status=500)

        # 4. Route: Run Clinical Provisioner Agent in real-time
        elif path == "/api/run-agent":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)

            try:
                data = json.loads(post_data.decode("utf-8"))
                patient_name = data.get("name", "Unknown_Patient")
                dob = data.get("dob", "")
                phone = data.get("phone", "")
                email = data.get("email", "")
                insurance = data.get("insurance", "")
                summary = data.get("summary", "No clinical summary available.")
                injuries = data.get("injuries", [])
                case_type = data.get("type", "MVA")

                # Format Folder Name
                sanitized_name = re.sub(r'\s+', '_', patient_name)
                folder_name = f"{sanitized_name}_DOB_{dob}" if dob else sanitized_name
                patient_dir = os.path.join(OUTPUT_PARENT_DIR, folder_name)

                # 1. Provision Actual Local Directories
                records_dir = os.path.join(patient_dir, "Referral_&_Medical_Records")
                forms_dir = os.path.join(patient_dir, "Internal_Forms")

                os.makedirs(records_dir, exist_ok=True)
                os.makedirs(forms_dir, exist_ok=True)

                # 2. Generate a Beautiful Markdown Case Summary
                summary_file_name = f"{sanitized_name}_Case_Summary.md"
                summary_path = os.path.join(forms_dir, summary_file_name)

                current_date = datetime.now().strftime("%Y-%m-%d %H:%M")

                md_content = f"""# CLINICAL CASE SUMMARY
*IntakeOS Proved of Concept - Generated Date: {current_date}*

##  Patient Demographics & Profile
- **Patient Name**: {patient_name}
- **Date of Birth**: {dob}
- **Primary Phone**: {phone}
- **Email Address**: {email}
- **Referring Doctor / Source**: AI Phone Intake System
- **Case Type / Injury Category**: {case_type} (Insurance Provider: {insurance})

---

##  Documented Injuries & Physical Complaints
{chr(10).join([f'- {inj}' for inj in injuries])}

---

##  Narrative Clinical Reconstruction (Gemini Core)
{summary}

---
*Property of Atlantic Pain & Wellness Institute. Confidentially Transmitted.*
"""
                with open(summary_path, "w", encoding="utf-8") as f:
                    f.write(md_content)

                # 3. Create a Local Email Draft
                email_path = os.path.join(forms_dir, f"{sanitized_name}_Welcome_Email_Draft.txt")
                email_content = f"""To: {email}
Subject: Welcome! Action Required: Complete Your Pre-Visit Forms

Hello {patient_name},

Welcome to our clinic! We have successfully received your clinical referral records from AI Phone Intake.
To finalize your check-in, please complete your pre-registration intake form: https://docs.google.com/forms/d/e/.../viewform?entry.patient_name={urllib.parse.quote(patient_name)}

Best regards,
Clinical Patient Care Team
"""
                with open(email_path, "w", encoding="utf-8") as f:
                    f.write(email_content)

                # Log sync event in terminal log format
                log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                with open(log_file_path, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] Provisioned local directories in scratch/Antigravity_Workspace/{folder_name}\n")
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [COMPOSER] Created clinical prefilled document: {summary_file_name}\n")

                # Prepare response packet
                response_payload = {
                    "status": "SUCCESS",
                    "patientName": patient_name,
                    "folderPath": f"scratch/Antigravity_Workspace/{folder_name}",
                    "summaryPath": f"scratch/Antigravity_Workspace/{folder_name}/Internal_Forms/{summary_file_name}",
                    "emailPath": f"scratch/Antigravity_Workspace/{folder_name}/Internal_Forms/{sanitized_name}_Welcome_Email_Draft.txt"
                }

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(response_payload).encode("utf-8"))

            except Exception as e:
                self.send_error(500, f"Error executing agent pipeline: {str(e)}")

        # 5. Route: Sync and Update Surgery Center Status
        elif path == "/api/update-status":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)

            try:
                data = json.loads(post_data.decode("utf-8"))
                patient_name = data.get("name")
                status = data.get("status")

                if not patient_name or not status:
                    self.send_error(400, "Missing patient name or status.")
                    return

                # 1. Update in scratch/patient_database.json
                database_path = os.path.join(SCRATCH_DIR, "patient_database.json")
                if os.path.exists(database_path):
                    with open(database_path, "r", encoding="utf-8") as f:
                        patients = json.load(f)

                    updated = False
                    for p in patients:
                        if p.get("name", "").lower() == patient_name.lower():
                            p["surgeryStatus"] = status
                            updated = True

                    if not updated:
                        patients.append({
                            "name": patient_name,
                            "surgeryStatus": status,
                            "dob": "",
                            "notes_summaries": [f"Created from dashboard status update ({datetime.now().strftime('%m/%d/%Y')}) — demographics not yet on file."],
                            "transcript": f"Surgery Center Status updated to {status}."
                        })

                    with open(database_path, "w", encoding="utf-8") as f:
                        json.dump(patients, f, indent=2, ensure_ascii=False)

                # 2. Update physical file tag in workspace patient folder (Google Drive aligned)
                sanitized_name = re.sub(r'\s+', '_', patient_name)
                target_dir = None
                if os.path.exists(OUTPUT_PARENT_DIR):
                    for folder in os.listdir(OUTPUT_PARENT_DIR):
                        if folder == sanitized_name or folder.startswith(f"{sanitized_name}_DOB_"):
                            target_dir = os.path.join(OUTPUT_PARENT_DIR, folder)
                            break

                if target_dir and os.path.exists(target_dir):
                    # Clean up old status markers
                    for filename in os.listdir(target_dir):
                        if filename.startswith("SURGERY_STATUS_") and filename.endswith(".txt"):
                            try:
                                os.remove(os.path.join(target_dir, filename))
                            except Exception:
                                pass

                    # Allocate new tag file
                    status_tag_path = os.path.join(target_dir, f"SURGERY_STATUS_{status.upper()}.txt")
                    with open(status_tag_path, "w", encoding="utf-8") as f:
                        f.write(f"Patient Name: {patient_name}\nSurgery Center Status: {status}\nLast Synced: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

                # 2.5 Forward to Google Sheets Webhook if configured
                if APPS_SCRIPT_WEBHOOK_URL:
                    try:
                        payload = json.dumps({"action": "updateSurgeryStatus", "patientName": patient_name, "status": status}).encode('utf-8')
                        req = urllib.request.Request(APPS_SCRIPT_WEBHOOK_URL, data=payload, headers={'Content-Type': 'application/json'})
                        urllib.request.urlopen(req, timeout=5)
                    except Exception as e:
                        logger.error(f"Failed to forward status to Google Sheets webhook: {e}")

                # 3. Log event
                log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                with open(log_file_path, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] Synced '{status}' Surgery Status for {patient_name} in database & Drive folder\n")

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"status": "SUCCESS", "message": f"Status updated to {status}."}).encode("utf-8"))

            except Exception as e:
                self.send_error(500, f"Error updating status: {str(e)}")
        # 6. Route: Koko AI Chat — real GPT-powered conversational agent
        elif path == "/api/koko":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)

            # Rate limiting
            client_ip = self.client_address[0]
            now = time.time()
            window = _koko_rate.get(client_ip, [])
            window = [t for t in window if now - t < KOKO_RATE_WINDOW]
            if len(window) >= KOKO_RATE_LIMIT:
                self.send_error(429, "Rate limit exceeded. Try again in a minute.")
                return
            window.append(now)
            _koko_rate[client_ip] = window

            try:
                data = json.loads(post_data.decode("utf-8"))
                query = data.get("query", "").strip()
                # history: [{role, content}, ...] — optional multi-turn context
                history = data.get("history", [])

                if not query:
                    self.send_error(400, "Missing 'query' field.")
                    return

                if not KOKO_GPT_AVAILABLE:
                    reply = ("Koko's AI brain (gpt_client.py) could not be loaded. "
                             "Make sure gpt_client.py is in the same directory as server.py.")
                else:
                    messages = list(history) + [{"role": "user", "content": query}]
                    reply = gpt_client.get_completion(messages)

                logger.info(f"[KOKO] Query from {client_ip}: {query[:80]}")

                response_payload = {"reply": reply, "status": "ok"}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(response_payload).encode("utf-8"))

            except RuntimeError as e:
                # Friendly error (e.g. missing API key, model error)
                logger.error(f"[KOKO] RuntimeError: {e}")
                fallback = {
                    "reply": f" <strong>Koko is offline.</strong> {e}",
                    "status": "error",
                    "koko": koko_status_payload(),
                }
                self.send_response(200)  # 200 so JS can display the message in chat
                self.send_header("Content-Type", "application/json")
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(fallback).encode("utf-8"))

            except Exception as e:
                self.send_error(500, f"Koko error: {str(e)}")

        # ─── Settings sync and configuration updates ─────────────────────────
        elif path == "/api/settings":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
                payload = json.loads(raw) if raw else {}
                url = payload.get("appsScriptWebhookUrl", "").strip()

                cfg = {"appsScriptWebhookUrl": url}
                ok = apw_save_config(cfg)
                if ok:
                    self.send_json({"status": "SUCCESS", "message": "Settings saved successfully.", "appsScriptWebhookUrl": APPS_SCRIPT_WEBHOOK_URL})
                else:
                    self.send_error(500, "Failed to persist configuration file.")
            except Exception as e:
                self.send_error(500, f"Error saving settings: {str(e)}")

        elif path == "/api/automation/run":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
                payload = json.loads(raw) if raw else {}
                script = os.path.join(WORKSPACE_DIR, "daily_clinical_sync.py")
                if not os.path.exists(script):
                    self.send_json({
                        "status": "ERROR",
                        "message": "daily_clinical_sync.py was not found.",
                        "automation": _automation_status_payload(),
                    })
                    return
                cmd = ["python3", script]
                if payload.get("statusOnly"):
                    cmd.append("--status-only")
                if payload.get("skipPortals"):
                    cmd.append("--skip-portals")
                if payload.get("skipRingCentral"):
                    cmd.append("--skip-ringcentral")
                proc = subprocess.run(
                    cmd,
                    cwd=WORKSPACE_DIR,
                    capture_output=True,
                    text=True,
                    timeout=1200,
                )
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
                parsed = None
                if stdout:
                    try:
                        parsed = json.loads(stdout)
                    except Exception:
                        parsed = None
                if parsed is None:
                    parsed = _automation_status_payload().get("latestRun") or {}
                self.send_json({
                    "status": "SUCCESS" if proc.returncode == 0 else "ERROR",
                    "returnCode": proc.returncode,
                    "automation": parsed,
                    "message": "Automation run completed." if proc.returncode == 0 else "Automation run finished with errors or blockers.",
                    "stderr": stderr[-2000:],
                })
            except subprocess.TimeoutExpired:
                self.send_json({
                    "status": "ERROR",
                    "message": "Automation run timed out after 20 minutes.",
                    "automation": _automation_status_payload(),
                })
            except Exception as e:
                self.send_error(500, f"Error running automation: {str(e)}")

        elif path == "/api/test-sync":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
                payload = json.loads(raw) if raw else {}
                url = payload.get("appsScriptWebhookUrl", "").strip()
                if not url:
                    self.send_json({"status": "ERROR", "message": "Sync URL is blank. Please enter a valid URL."})
                    return

                # Send test request to Apps Script Web App
                test_payload = json.dumps({"action": "testConnection"}).encode('utf-8')
                req = urllib.request.Request(url, data=test_payload, headers={'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(req, timeout=5) as response:
                        res_data = json.loads(response.read().decode('utf-8'))
                        if res_data.get("success"):
                            self.send_json({"status": "SUCCESS", "message": "Connection verified! " + res_data.get("message", "")})
                        else:
                            self.send_json({"status": "ERROR", "message": "Apps Script error: " + res_data.get("error", "Unknown error")})
                except Exception as ex:
                    self.send_json({"status": "ERROR", "message": f"Connection failed: {str(ex)}. Check Web App deployment."})
            except Exception as e:
                self.send_error(500, f"Error testing connection: {str(e)}")

        elif path == "/api/sync-patient":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
                data = json.loads(raw) if raw else {}

                patient_name = data.get("name")
                dob = data.get("dob")
                fields = data.get("fields", {})

                if not patient_name:
                    self.send_error(400, "Missing patient name.")
                    return

                # 1. Update in local scratch/patient_database.json
                database_path = os.path.join(SCRATCH_DIR, "patient_database.json")
                if os.path.exists(database_path):
                    with open(database_path, "r", encoding="utf-8") as f:
                        patients = json.load(f)

                    updated = False
                    for p in patients:
                        if p.get("name", "").lower() == patient_name.lower():
                            if fields.get("patientPhone"): p["phone"] = fields["patientPhone"]
                            if fields.get("patientEmail"): p["email"] = fields["patientEmail"]
                            if fields.get("patientDOB"): p["dob"] = fields["patientDOB"]
                            if fields.get("surgeryStatus"): p["surgeryStatus"] = fields["surgeryStatus"]
                            updated = True
                    if not updated:
                        patients.append({
                            "name": patient_name,
                            "dob": dob or fields.get("patientDOB", ""),
                            "phone": fields.get("patientPhone", ""),
                            "email": fields.get("patientEmail", ""),
                            "surgeryStatus": fields.get("surgeryStatus", "Intake"),
                            "notes_summaries": ["Synchronized Patient Details Form"],
                            "transcript": "Updated details from clinical dashboard editor form."
                        })
                    with open(database_path, "w", encoding="utf-8") as f:
                        json.dump(patients, f, indent=2, ensure_ascii=False)

                # 2. Update physical folder status tag if configured
                sanitized_name = re.sub(r'\s+', '_', patient_name)
                target_dir = None
                if os.path.exists(OUTPUT_PARENT_DIR):
                    for folder in os.listdir(OUTPUT_PARENT_DIR):
                        if folder == sanitized_name or folder.startswith(f"{sanitized_name}_DOB_"):
                            target_dir = os.path.join(OUTPUT_PARENT_DIR, folder)
                            break
                if target_dir and os.path.exists(target_dir):
                    s_status = fields.get("surgeryStatus", "INTAKE")
                    for filename in os.listdir(target_dir):
                        if filename.startswith("SURGERY_STATUS_") and filename.endswith(".txt"):
                            try: os.remove(os.path.join(target_dir, filename))
                            except Exception: pass
                    status_tag_path = os.path.join(target_dir, f"SURGERY_STATUS_{s_status.upper()}.txt")
                    with open(status_tag_path, "w", encoding="utf-8") as f:
                        f.write(f"Patient Name: {patient_name}\nSurgery Center Status: {s_status}\nLast Synced: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

                # 3. Forward to Google Sheets Webhook
                sheet_updated = 0
                if APPS_SCRIPT_WEBHOOK_URL:
                    try:
                        payload = json.dumps({"action": "updatePatient", "patientName": patient_name, "patientDOB": dob, "fields": fields}).encode('utf-8')
                        req = urllib.request.Request(APPS_SCRIPT_WEBHOOK_URL, data=payload, headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req, timeout=5) as response:
                            res_data = json.loads(response.read().decode('utf-8'))
                            sheet_updated = res_data.get("rowsUpdated", 0)
                    except Exception as e:
                        logger.error(f"Failed to forward patient update to Google Sheets webhook: {e}")

                # 4. Log event
                log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                with open(log_file_path, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] Edited patient profile for {patient_name} in DB & Sheets (updated {sheet_updated} rows)\n")

                self.send_json({"status": "SUCCESS", "message": "Patient synced successfully.", "rowsUpdated": sheet_updated})
            except Exception as e:
                self.send_error(500, f"Error syncing patient: {str(e)}")

        elif path == "/api/sync-note":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
                data = json.loads(raw) if raw else {}

                patient_name = data.get("patientName")
                dob = data.get("patientDOB")
                note = data.get("note")
                author = data.get("author", "unknown")

                if not patient_name or not note:
                    self.send_error(400, "Missing patientName or note.")
                    return

                # 1. Forward to Google Docs Webhook
                doc_url = ""
                if APPS_SCRIPT_WEBHOOK_URL:
                    try:
                        payload = json.dumps({"action": "appendPatientNote", "patientName": patient_name, "patientDOB": dob, "note": note, "author": author}).encode('utf-8')
                        req = urllib.request.Request(APPS_SCRIPT_WEBHOOK_URL, data=payload, headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req, timeout=5) as response:
                            res_data = json.loads(response.read().decode('utf-8'))
                            doc_url = res_data.get("docUrl", "")
                    except Exception as e:
                        logger.error(f"Failed to append note to Google Doc: {e}")

                # 2. Log sync event locally — and report honestly whether the
                # note actually reached Google or only the local database.
                synced_to_google = bool(APPS_SCRIPT_WEBHOOK_URL) and bool(doc_url)
                log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                with open(log_file_path, "a", encoding="utf-8") as f:
                    outcome = "synced to Google Doc" if synced_to_google else "saved locally (Google sync not configured/failed)"
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [NOTE] Clinical note for {patient_name}: {outcome}\n")

                self.send_json({
                    "status": "SUCCESS" if synced_to_google else "LOCAL_ONLY",
                    "message": "Note synchronized to Google Doc successfully." if synced_to_google
                               else "Note saved locally. Google Doc sync did not run (webhook not configured or unreachable).",
                    "docUrl": doc_url
                })
            except Exception as e:
                self.send_error(500, f"Error syncing note: {str(e)}")

        elif path == "/api/open-meeting-resource":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
                data = json.loads(raw) if raw else {}
                patient_name = data.get("patientName", "").strip()
                resource_type = data.get("resourceType", "").strip()
                if not patient_name or not resource_type:
                    self.send_error(400, "Missing patientName or resourceType.")
                    return
                payload = patient_resource_payload(patient_name, resource_type)
                if not payload.get("exists") or not payload.get("path"):
                    self.send_json({"status": "MISSING", **payload}, status=404)
                    return
                path_to_open = payload["path"]
                if not _is_safe_local_path(path_to_open):
                    self.send_error(403, "Resource path is outside the approved project/scratch directories.")
                    return
                subprocess.run(["open", path_to_open], check=False)
                self.send_json({"status": "OPENED", **payload})
            except Exception as e:
                self.send_error(500, f"Error opening resource: {str(e)}")

        elif path == "/api/staff":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                staff = data.get("staff")
                if not isinstance(staff, list) or not staff:
                    self.send_error(400, "Body must contain a non-empty 'staff' list.")
                    return
                cleaned = []
                for s in staff:
                    name = str(s.get("name", "")).strip()
                    role = str(s.get("role", "")).strip().lower()
                    if not name or role not in ("nurse", "intern", "admin", "doctor", "front-desk"):
                        continue
                    cleaned.append({
                        "id": s.get("id") or _patient_slug(name).lower(),
                        "name": name,
                        "role": role,
                        "active": bool(s.get("active", True)),
                    })
                if not cleaned:
                    self.send_error(400, "No valid staff entries (need name + role).")
                    return
                _save_staff(cleaned)
                self.send_json({"status": "SUCCESS", "staff": cleaned})
            except Exception as e:
                self.send_error(500, f"Error saving staff roster: {str(e)}")

        elif path == "/api/followups/generate":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                p_name = data.get("patientName", "").strip()
                dos = data.get("dos", "").strip()
                if not p_name or not dos:
                    self.send_error(400, "Missing patientName or dos (YYYY-MM-DD).")
                    return
                created, skipped = _generate_followups_for_patient(
                    p_name, dos,
                    assignee=data.get("assignee", "Unassigned"),
                    created_by=data.get("createdBy", "dashboard"))
                self.send_json({"status": "SUCCESS", "created": created, "skipped": skipped})
            except Exception as e:
                self.send_error(500, f"Error generating follow-ups: {str(e)}")

        elif path == "/api/followups/add":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                p_name = data.get("patientName", "").strip()
                due = data.get("dueDate", "").strip()
                if not p_name or not due:
                    self.send_error(400, "Missing patientName or dueDate (YYYY-MM-DD).")
                    return
                followups = _load_followups()
                fu = {
                    "id": f"fu_{_patient_slug(p_name).lower()}_custom_{int(time.time() * 1000) % 100000}",
                    "patientName": p_name,
                    "callKey": "custom",
                    "title": data.get("title", "Patient Follow-up Call").strip() or "Patient Follow-up Call",
                    "dos": data.get("dos", ""),
                    "dueDate": due,
                    "assignee": data.get("assignee", "Unassigned"),
                    "status": "pending",
                    "outcome": "",
                    "notes": data.get("notes", ""),
                    "createdAt": datetime.now().isoformat(),
                    "createdBy": data.get("createdBy", "dashboard"),
                    "completedAt": None,
                    "completedBy": None,
                }
                followups.append(fu)
                _save_followups(followups)
                self.send_json({"status": "SUCCESS", "followup": fu})
            except Exception as e:
                self.send_error(500, f"Error adding follow-up: {str(e)}")

        elif path == "/api/followups/complete":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                fu_id = data.get("id")
                completed_by = str(data.get("completedBy", "")).strip()
                if not fu_id:
                    self.send_error(400, "Missing follow-up id.")
                    return
                if not completed_by:
                    self.send_error(400, "Missing completedBy — completions must be attributed to a named staff member.")
                    return
                followups = _load_followups()
                target = next((f for f in followups if f.get("id") == fu_id), None)
                if not target:
                    self.send_error(404, "Follow-up not found.")
                    return
                target["status"] = "completed"
                target["outcome"] = data.get("outcome", "").strip()
                if data.get("notes"):
                    target["notes"] = data["notes"]
                target["completedAt"] = datetime.now().isoformat()
                target["completedBy"] = completed_by
                _save_followups(followups)
                self.send_json({"status": "SUCCESS", "followup": target})
            except Exception as e:
                self.send_error(500, f"Error completing follow-up: {str(e)}")

        elif path == "/api/followups/update":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                fu_id = data.get("id")
                if not fu_id:
                    self.send_error(400, "Missing follow-up id.")
                    return
                followups = _load_followups()
                target = next((f for f in followups if f.get("id") == fu_id), None)
                if not target:
                    self.send_error(404, "Follow-up not found.")
                    return
                for field in ("assignee", "dueDate", "notes", "title"):
                    if data.get(field) is not None:
                        target[field] = data[field]
                if data.get("status") in ("pending", "skipped"):
                    target["status"] = data["status"]
                target["updatedAt"] = datetime.now().isoformat()
                target["updatedBy"] = data.get("updatedBy", "")
                _save_followups(followups)
                self.send_json({"status": "SUCCESS", "followup": target})
            except Exception as e:
                self.send_error(500, f"Error updating follow-up: {str(e)}")

        elif path == "/api/faxes/sync":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                days = int(data.get("days") or 1)
                days = max(1, min(days, 30))
                cmd = ["python3", os.path.join(WORKSPACE_DIR, "ringcentral_fax_sync.py"), "--days", str(days)]
                proc = subprocess.run(cmd, cwd=WORKSPACE_DIR, capture_output=True, text=True, timeout=180)
                try:
                    payload = json.loads(proc.stdout.strip() or "{}")
                except Exception:
                    payload = {"status": "error", "error": proc.stdout.strip() or proc.stderr.strip()}
                if proc.returncode != 0 or payload.get("status") == "error":
                    self.send_json({
                        "status": "ERROR",
                        "message": payload.get("error") or proc.stderr.strip() or "RingCentral fax sync failed.",
                        "digest": _fax_digest_payload(day_filter="today"),
                    }, status=200)
                    return
                payload["digest"] = _fax_digest_payload(day_filter=data.get("filter", "today"))
                self.send_json({"status": "SUCCESS", **payload})
            except subprocess.TimeoutExpired:
                self.send_json({"status": "ERROR", "message": "RingCentral fax sync timed out."}, status=200)
            except Exception as e:
                self.send_error(500, f"Error syncing faxes: {str(e)}")

        elif path == "/api/faxes/review":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                message_id = str(data.get("messageId") or "").strip()
                action = str(data.get("action") or "").strip()
                actor = str(data.get("actor") or "").strip() or "dashboard"
                if not message_id or not action:
                    self.send_error(400, "Missing messageId or action.")
                    return
                conn = _fax_db()
                row = conn.execute("SELECT * FROM faxes WHERE message_id = ?", (message_id,)).fetchone()
                if not row:
                    conn.close()
                    self.send_error(404, "Fax not found.")
                    return
                updates = {"updated_at": datetime.now().isoformat(timespec="seconds")}
                detail = ""
                if action == "update":
                    for src, col in (
                        ("faxType", "fax_type"),
                        ("patientName", "patient_name"),
                        ("matchConfidence", "match_confidence"),
                        ("status", "status"),
                        ("suggestedOwner", "suggested_owner"),
                    ):
                        if src in data:
                            updates[col] = data[src]
                    detail = "Updated fax review fields."
                elif action == "mark_no_action":
                    updates["status"] = "closed"
                    detail = "Marked no action needed."
                elif action == "create_task":
                    task_id = _create_fax_task(
                        data.get("patientName") or row["patient_name"],
                        data.get("faxType") or row["fax_type"],
                        data.get("actionNeeded") or row["action_needed"],
                        message_id,
                        owner=data.get("suggestedOwner") or row["suggested_owner"] or "Unassigned",
                    )
                    updates["task_id"] = task_id
                    updates["status"] = "task-created"
                    detail = f"Created/linked task {task_id}."
                elif action == "attach_timeline":
                    patient_name = data.get("patientName") or row["patient_name"]
                    summary = data.get("summary") or row["summary"]
                    filed = _append_patient_fax_note(patient_name, summary, message_id)
                    updates["patient_name"] = patient_name
                    updates["timeline_filed"] = 1 if filed or row["timeline_filed"] else 0
                    updates["status"] = "filed"
                    detail = f"Attached to timeline for {patient_name}."
                else:
                    conn.close()
                    self.send_error(400, "Unsupported fax review action.")
                    return

                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(f"UPDATE faxes SET {set_clause} WHERE message_id = ?", (*updates.values(), message_id))
                _fax_audit(conn, message_id, actor, action, detail)
                conn.commit()
                updated = conn.execute("SELECT * FROM faxes WHERE message_id = ?", (message_id,)).fetchone()
                payload = _fax_dict(updated)
                conn.close()
                self.send_json({"status": "SUCCESS", "fax": payload, "digest": _fax_digest_payload(day_filter="today")})
            except Exception as e:
                self.send_error(500, f"Error reviewing fax: {str(e)}")

        elif path == "/api/faxes/open":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                message_id = str(data.get("messageId") or "").strip()
                conn = _fax_db()
                row = conn.execute("SELECT attachment_path FROM faxes WHERE message_id = ?", (message_id,)).fetchone()
                conn.close()
                if not row or not row["attachment_path"]:
                    self.send_error(404, "Fax attachment not found.")
                    return
                path_to_open = row["attachment_path"]
                real = os.path.realpath(path_to_open)
                if not real.startswith(os.path.realpath(SCRATCH_DIR)) or not os.path.exists(real):
                    self.send_error(403, "Unsafe or missing fax attachment path.")
                    return
                subprocess.run(["open", real], check=False)
                self.send_json({"status": "OPENED", "path": real})
            except Exception as e:
                self.send_error(500, f"Error opening fax: {str(e)}")

        elif path == "/api/manager/repair-task":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                issue_id = str(data.get("issueId") or "").strip()
                created_by = str(data.get("createdBy") or "Manager Core").strip()
                if not issue_id:
                    self.send_error(400, "Missing issueId.")
                    return

                review = _manager_review_payload(limit=20000)
                issue = next((i for i in review.get("issues", []) if i.get("id") == issue_id), None)
                if not issue:
                    self.send_error(404, "Manager issue no longer exists. Refresh the review queue.")
                    return

                task_id, created = _create_manager_repair_task(issue, created_by=created_by)
                self.send_json({
                    "status": "SUCCESS",
                    "taskId": task_id,
                    "created": created,
                    "issueId": issue_id,
                    "message": "Repair task created." if created else "Repair task already exists.",
                })
            except ValueError as e:
                self.send_error(400, str(e))
            except Exception as e:
                self.send_error(500, f"Error creating manager repair task: {str(e)}")

        elif path == "/api/tasks/add":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"

            try:
                data = json.loads(raw) if raw else {}
                p_name = data.get("patientName", "").strip()
                t_label = data.get("taskLabel", "").strip()
                assignee = data.get("assignee", "Unassigned").strip()
                status = data.get("status", "Pending").strip()
                notes = data.get("notes", "").strip()

                if not p_name or not t_label:
                    self.send_error(400, "Missing patientName or taskLabel.")
                    return

                tasks_path = os.path.join(SCRATCH_DIR, "team_tasks.json")
                tasks = []
                if os.path.exists(tasks_path):
                    with open(tasks_path, "r", encoding="utf-8") as f:
                        tasks = json.load(f)

                import random
                custom_id = f"task_{random.randint(1000, 9999)}_custom_{int(time.time() * 1000) % 1000}"

                new_task = {
                    "id": custom_id,
                    "patientName": p_name,
                    "taskKey": "custom_task",
                    "taskLabel": t_label,
                    "assignee": assignee,
                    "status": status,
                    "notes": notes,
                    "dueDate": data.get("dueDate", ""),
                    "createdBy": data.get("createdBy", ""),
                    "createdAt": datetime.now().isoformat() + "-04:00",
                    "updatedAt": datetime.now().isoformat() + "-04:00"
                }

                tasks.append(new_task)
                with open(tasks_path, "w", encoding="utf-8") as f:
                    json.dump(tasks, f, indent=2)

                sheet_updated = 0
                if APPS_SCRIPT_WEBHOOK_URL:
                    try:
                        payload = json.dumps({
                            "action": "updatePatientTask",
                            "patientName": p_name,
                            "taskKey": "custom_task",
                            "status": status,
                            "assignee": assignee,
                            "notes": new_task["notes"]
                        }).encode('utf-8')
                        req = urllib.request.Request(APPS_SCRIPT_WEBHOOK_URL, data=payload, headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req, timeout=5) as response:
                            res_data = json.loads(response.read().decode('utf-8'))
                            sheet_updated = res_data.get("rowsUpdated", 0)
                    except Exception as e:
                        logger.error(f"Failed to sync newly added task to Google Sheets: {e}")

                log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                with open(log_file_path, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] Created custom task '{t_label}' for {p_name} assigned to {assignee}\n")

                self.send_json({"status": "SUCCESS", "message": "Custom task added successfully."})
            except Exception as e:
                self.send_error(500, f"Error adding custom task: {str(e)}")

        elif path == "/api/tasks/update":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"

            try:
                data = json.loads(raw) if raw else {}
                task_id = data.get("id")
                status = data.get("status")
                assignee = data.get("assignee")
                notes = data.get("notes")
                note_update = data.get("noteUpdate", "").strip()

                if not task_id:
                    self.send_error(400, "Missing task id.")
                    return

                tasks_path = os.path.join(SCRATCH_DIR, "team_tasks.json")
                tasks = []
                if os.path.exists(tasks_path):
                    with open(tasks_path, "r", encoding="utf-8") as f:
                        tasks = json.load(f)

                updated = False
                p_name = ""
                t_key = ""
                t_label = ""
                updated_by = str(data.get("updatedBy", "")).strip()
                for t in tasks:
                    if t.get("id") == task_id:
                        if status is not None: t["status"] = status
                        if assignee is not None: t["assignee"] = assignee
                        if notes is not None: t["notes"] = notes
                        if data.get("dueDate") is not None: t["dueDate"] = data["dueDate"]

                        if note_update:
                            timestamp = datetime.now().strftime("%m/%d %H:%M")
                            author = f" {updated_by}" if updated_by else ""
                            t["notes"] = f"{t.get('notes', '')}\n- [{timestamp}{author}]: {note_update}"

                        if updated_by:
                            t["updatedBy"] = updated_by
                        if status == "Completed" and not t.get("completedAt"):
                            t["completedAt"] = datetime.now().isoformat() + "-04:00"
                            t["completedBy"] = updated_by or t.get("assignee", "")
                        t["updatedAt"] = datetime.now().isoformat() + "-04:00"
                        p_name = t.get("patientName")
                        t_key = t.get("taskKey")
                        t_label = t.get("taskLabel")
                        updated = True
                        break

                if updated:
                    with open(tasks_path, "w", encoding="utf-8") as f:
                        json.dump(tasks, f, indent=2)

                    sheet_updated = 0
                    if APPS_SCRIPT_WEBHOOK_URL:
                        try:
                            payload = json.dumps({
                                "action": "updatePatientTask",
                                "patientName": p_name,
                                "taskKey": t_key,
                                "status": status,
                                "assignee": assignee,
                                "notes": t["notes"]
                            }).encode('utf-8')
                            req = urllib.request.Request(APPS_SCRIPT_WEBHOOK_URL, data=payload, headers={'Content-Type': 'application/json'})
                            with urllib.request.urlopen(req, timeout=5) as response:
                                res_data = json.loads(response.read().decode('utf-8'))
                                sheet_updated = res_data.get("rowsUpdated", 0)
                        except Exception as e:
                            logger.error(f"Failed to forward task update to Google Sheets: {e}")

                    log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                    with open(log_file_path, "a", encoding="utf-8") as f:
                        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] Task '{t_label}' updated to {status} for {p_name} by {assignee}\n")

                    self.send_json({"status": "SUCCESS", "message": "Task updated successfully."})
                else:
                    self.send_error(404, "Task not found.")
            except Exception as e:
                self.send_error(500, f"Error updating task: {str(e)}")

        elif path == "/api/monday-meeting/log":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"

            try:
                data = json.loads(raw) if raw else {}
                p_name = data.get("patientName")
                note_text = data.get("note")
                author = data.get("author", "Dr. Gupta")

                if not p_name or not note_text:
                    self.send_error(400, "Missing patientName or note.")
                    return

                db_path = os.path.join(SCRATCH_DIR, "patient_database.json")
                if os.path.exists(db_path):
                    with open(db_path, "r", encoding="utf-8") as f:
                        pts = json.load(f)

                    updated = False
                    for p in pts:
                        if p.get("name", "").lower() == p_name.lower():
                            if "notes_summaries" not in p:
                                p["notes_summaries"] = []
                            p["notes_summaries"].append(f"[{author} - Meeting Note]: {note_text}")
                            updated = True
                            break

                    if updated:
                        with open(db_path, "w", encoding="utf-8") as f:
                            json.dump(pts, f, indent=2, ensure_ascii=False)

                doc_url = ""
                if APPS_SCRIPT_WEBHOOK_URL:
                    try:
                        payload = json.dumps({
                            "action": "appendPatientNote",
                            "patientName": p_name,
                            "note": f"Monday meeting note: {note_text}",
                            "author": author
                        }).encode('utf-8')
                        req = urllib.request.Request(APPS_SCRIPT_WEBHOOK_URL, data=payload, headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req, timeout=5) as response:
                            res_data = json.loads(response.read().decode('utf-8'))
                            doc_url = res_data.get("docUrl", "")
                    except Exception as e:
                        logger.error(f"Failed to append Monday meeting note via Apps Script: {e}")

                log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                with open(log_file_path, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] Monday Meeting note added for {p_name}: '{note_text[:40]}...'\n")

                self.send_json({"status": "SUCCESS", "message": "Monday meeting note appended successfully.", "docUrl": doc_url})
            except Exception as e:
                self.send_error(500, f"Error logging meeting note: {str(e)}")

        elif path == "/api/webedoctor/sync":
            try:
                # Load credentials from local git-ignored .env securely
                env = os.environ.copy()
                dotenv_path = os.path.join(WORKSPACE_DIR, ".env")
                if os.path.exists(dotenv_path):
                    with open(dotenv_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                env[k.strip()] = v.strip()

                # Clean out existing 2FA sync state & code files
                state_path = os.path.join(SCRATCH_DIR, "sync_state.json")
                if os.path.exists(state_path):
                    try:
                        os.remove(state_path)
                    except Exception:
                        pass
                code_path = os.path.join(SCRATCH_DIR, "2fa_code.txt")
                if os.path.exists(code_path):
                    try:
                        os.remove(code_path)
                    except Exception:
                        pass

                # Spawn the Playwright sync script in a background subprocess
                cmd = ["python3", os.path.join(SCRATCH_DIR, "run_crm_sync.py")]
                
                log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                with open(log_file_path, "w", encoding="utf-8") as lf:
                    lf.write(f"[{datetime.now().strftime('%H:%M:%S')}] [INFO] Starting Unified CRM Billing Sync Bot (SIS Complete + Sunny Vigg)...\n")

                subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                self.send_json({
                    "status": "SUCCESS", 
                    "message": "Unified CRM database synchronization bot triggered successfully in the background."
                })
            except Exception as e:
                self.send_error(500, f"Error launching unified CRM sync bot: {str(e)}")

        elif path == "/api/sis/2fa":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                code = data.get("code")
                if not code:
                    self.send_error(400, "Missing verification code.")
                    return
                
                # Write code to file for sis_agent.py to pick up
                code_path = os.path.join(SCRATCH_DIR, "2fa_code.txt")
                with open(code_path, "w", encoding="utf-8") as f:
                    f.write(code.strip())
                
                # Append log message
                log_file_path = os.path.join(WORKSPACE_DIR, "webedoctor_rpa.log")
                with open(log_file_path, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [2FA] User submitted verification code: {code[:3]}***\n")
                
                self.send_json({"status": "SUCCESS", "message": "MFA verification code registered."})
            except Exception as e:
                self.send_error(500, f"Failed to register MFA code: {str(e)}")

        elif path == "/api/patients/update":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                body = json.loads(raw) if raw else {}
                target_name = body.get("name", "").strip()
                fields = body.get("fields", {})

                if not target_name:
                    self.send_json({"ok": False, "error": "Missing name field"}, status=400)
                    return

                database_path = os.path.join(SCRATCH_DIR, "patient_database.json")
                patients = []
                if os.path.exists(database_path):
                    with open(database_path, "r", encoding="utf-8") as f:
                        patients = json.load(f)

                updated_patient = None
                for p in patients:
                    if p.get("name", "").lower() == target_name.lower():
                        for key, val in fields.items():
                            if key == "name":
                                p["name"] = val
                            else:
                                p[key] = val
                        updated_patient = p
                        break

                if updated_patient is None:
                    self.send_json({"ok": False, "error": "Patient not found"}, status=404)
                    return

                with open(database_path, "w", encoding="utf-8") as f:
                    json.dump(patients, f, indent=2, ensure_ascii=False)

                self.send_json({"ok": True, "patient": updated_patient})
            except Exception as e:
                self.send_error(500, str(e))

        elif path == "/api/patients/delete":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                body = json.loads(raw) if raw else {}
                target_name = body.get("name", "").strip()

                if not target_name:
                    self.send_json({"ok": False, "error": "Missing name field"}, status=400)
                    return

                database_path = os.path.join(SCRATCH_DIR, "patient_database.json")
                patients = []
                if os.path.exists(database_path):
                    with open(database_path, "r", encoding="utf-8") as f:
                        patients = json.load(f)

                filtered = [p for p in patients if p.get("name", "").lower() != target_name.lower()]

                with open(database_path, "w", encoding="utf-8") as f:
                    json.dump(filtered, f, indent=2, ensure_ascii=False)

                self.send_json({"ok": True, "deleted": target_name})
            except Exception as e:
                self.send_error(500, str(e))

        elif path == "/api/webedoctor/crawl":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                data = json.loads(raw) if raw else {}
                action = data.get("action")
                
                state_file = os.path.join(SCRATCH_DIR, "crawl_state.json")
                state = {
                    "last_index": 0,
                    "status": "idle",
                    "total_crawled": 0,
                    "matched_sis": 0,
                    "matched_svigg": 0,
                    "not_found": 0,
                    "num_workers": 4,
                    "timestamp": datetime.now().isoformat()
                }
                if os.path.exists(state_file):
                    try:
                        with open(state_file, "r", encoding="utf-8") as f:
                            state = json.load(f)
                    except:
                        pass
                
                if action == "start" or action == "resume":
                    num_workers = int(data.get("num_workers", state.get("num_workers", 4)))
                    num_workers = max(1, min(16, num_workers))
                    state["status"] = "running"
                    state["num_workers"] = num_workers
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2)
                        
                    env = os.environ.copy()
                    dotenv_path = os.path.join(WORKSPACE_DIR, ".env")
                    if os.path.exists(dotenv_path):
                        with open(dotenv_path, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line and not line.startswith("#") and "=" in line:
                                    k, v = line.split("=", 1)
                                    env[k.strip()] = v.strip()
                                    
                    cmd = ["python3", os.path.join(SCRATCH_DIR, "crawl_all_patients_crm.py"), "--workers", str(num_workers)]
                    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    self.send_json({"status": "SUCCESS", "crawl_state": state})
                elif action == "pause":
                    state["status"] = "paused"
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2)
                    self.send_json({"status": "SUCCESS", "crawl_state": state})
                elif action == "reset":
                    state = {
                        "last_index": 0,
                        "status": "idle",
                        "total_crawled": 0,
                        "matched_sis": 0,
                        "matched_svigg": 0,
                        "not_found": 0,
                        "num_workers": 4,
                        "timestamp": datetime.now().isoformat()
                    }
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2)
                    self.send_json({"status": "SUCCESS", "crawl_state": state})
                else:
                    self.send_error(400, "Invalid action.")
            except Exception as e:
                self.send_error(500, f"Error processing crawl command: {str(e)}")

        else:
            self.send_error(404, "Endpoint not found.")


def run_server():
    server_address = ("", PORT)
    httpd = HTTPServer(server_address, ClinicalDashboardHandler)
    logger.info(f"Connected Clinical Dashboard Server running on http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down.")
        httpd.server_close()

if __name__ == "__main__":
    run_server()

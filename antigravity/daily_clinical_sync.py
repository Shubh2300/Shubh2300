#!/usr/bin/env python3
"""
daily_clinical_sync.py — one morning automation runner for the clinical
dashboard.

It coordinates the integrations that make the app feel like an office brain:
SIS/Svig AR exports, AR import, RingCentral calls, RingCentral faxes, and the
dashboard reconciliation scan. It is intentionally forgiving: missing portal
sessions or API credentials are reported as blockers instead of crashing the
whole run.

Usage:
  python3 daily_clinical_sync.py
  python3 daily_clinical_sync.py --status-only
  python3 daily_clinical_sync.py --skip-portals
  python3 daily_clinical_sync.py --skip-ringcentral
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

WORKSPACE_DIR = os.environ.get("ANTIGRAVITY_WORKSPACE_DIR", os.path.dirname(os.path.abspath(__file__)))
SCRATCH_DIR = os.environ.get("ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch"))
STATUS_PATH = os.path.join(SCRATCH_DIR, "automation_status.json")


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


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def present(*keys):
    return all(bool(os.environ.get(k, "").strip()) for k in keys)


def ar_report_files():
    ar_dir = os.path.join(SCRATCH_DIR, "ar_reports")
    if not os.path.isdir(ar_dir):
        return []
    return [
        os.path.join(ar_dir, f)
        for f in os.listdir(ar_dir)
        if f.lower().endswith((".csv", ".xlsx", ".xlsm")) and not f.startswith("~")
    ]


def readiness():
    ar_dir = os.path.join(SCRATCH_DIR, "ar_reports")
    reports = ar_report_files()
    return {
        "ringcentral": {
            "ready": present("RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT"),
            "needs": [] if present("RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT") else ["RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT"],
        },
        "sis": {
            "ready": os.path.exists(os.path.join(SCRATCH_DIR, "sis_browser_state.json")),
            "needs": [] if os.path.exists(os.path.join(SCRATCH_DIR, "sis_browser_state.json")) else ["Fresh SIS browser session / SMS 2FA"],
        },
        "svigg": {
            "ready": present("WEBEDOCTOR_URL", "WEBEDOCTOR_USER", "WEBEDOCTOR_PASS"),
            "needs": [] if present("WEBEDOCTOR_URL", "WEBEDOCTOR_USER", "WEBEDOCTOR_PASS") else ["WEBEDOCTOR_URL", "WEBEDOCTOR_USER", "WEBEDOCTOR_PASS"],
        },
        "playwright": {
            "ready": shutil.which("python3") is not None,
            "needs": [],
        },
        "arReportsDir": {
            "ready": os.path.isdir(ar_dir),
            "path": ar_dir,
            "reportCount": len(reports),
            "latestReport": max(reports, key=os.path.getmtime) if reports else "",
        },
    }


def write_status(payload):
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, STATUS_PATH)


def run_step(name, cmd, timeout=300, blocker=None):
    if blocker:
        return {
            "name": name,
            "status": "blocked",
            "startedAt": now_iso(),
            "finishedAt": now_iso(),
            "durationSec": 0,
            "command": " ".join(cmd),
            "message": blocker,
            "output": "",
        }
    started = time.time()
    started_iso = now_iso()
    try:
        proc = subprocess.run(
            cmd,
            cwd=WORKSPACE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return {
            "name": name,
            "status": "ok" if proc.returncode == 0 else "error",
            "startedAt": started_iso,
            "finishedAt": now_iso(),
            "durationSec": round(time.time() - started, 2),
            "command": " ".join(cmd),
            "returnCode": proc.returncode,
            "message": "Completed." if proc.returncode == 0 else "Command returned a non-zero exit code.",
            "output": output[-4000:],
        }
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "status": "error",
            "startedAt": started_iso,
            "finishedAt": now_iso(),
            "durationSec": round(time.time() - started, 2),
            "command": " ".join(cmd),
            "message": f"Timed out after {timeout}s.",
            "output": "",
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "error",
            "startedAt": started_iso,
            "finishedAt": now_iso(),
            "durationSec": round(time.time() - started, 2),
            "command": " ".join(cmd),
            "message": str(exc),
            "output": "",
        }


def reconciliation_step():
    started = time.time()
    try:
        with urllib.request.urlopen("http://localhost:8000/api/reconciliation", timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "name": "Reconciliation scan",
            "status": "ok",
            "startedAt": now_iso(),
            "finishedAt": now_iso(),
            "durationSec": round(time.time() - started, 2),
            "message": f"{data.get('summary', {}).get('high', 0)} high-severity findings; {data.get('summary', {}).get('total', 0)} total.",
            "output": json.dumps(data.get("summary", {}), indent=2),
        }
    except Exception as exc:
        return {
            "name": "Reconciliation scan",
            "status": "blocked",
            "startedAt": now_iso(),
            "finishedAt": now_iso(),
            "durationSec": round(time.time() - started, 2),
            "message": f"Dashboard server unavailable or scan failed: {exc}",
            "output": "",
        }


def build_status(status="idle", steps=None):
    steps = steps or []
    return {
        "status": status,
        "generatedAt": now_iso(),
        "workspaceDir": WORKSPACE_DIR,
        "scratchDir": SCRATCH_DIR,
        "readiness": readiness(),
        "steps": steps,
        "summary": {
            "ok": sum(1 for s in steps if s.get("status") == "ok"),
            "blocked": sum(1 for s in steps if s.get("status") == "blocked"),
            "error": sum(1 for s in steps if s.get("status") == "error"),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status-only", action="store_true")
    ap.add_argument("--skip-portals", action="store_true")
    ap.add_argument("--skip-ringcentral", action="store_true")
    args = ap.parse_args()

    if args.status_only:
        payload = build_status("ready")
        write_status(payload)
        print(json.dumps(payload, indent=2))
        return 0

    os.makedirs(SCRATCH_DIR, exist_ok=True)
    r = readiness()
    steps = []
    write_status(build_status("running", steps))

    if not args.skip_portals:
        portal_blocker = None
        if not r["sis"]["ready"] or not r["svigg"]["ready"]:
            missing = r["sis"]["needs"] + r["svigg"]["needs"]
            portal_blocker = "Portal export blocked: " + ", ".join(missing)
        steps.append(run_step(
            "SIS/Svig AR report export",
            ["python3", "ar_export_agent.py"],
            timeout=900,
            blocker=portal_blocker,
        ))

    import_blocker = None
    if not ar_report_files():
        import_blocker = "AR import blocked: no CSV/XLSX reports found yet. Export from SIS/Svigg or place files in scratch/ar_reports."
    steps.append(run_step(
        "AR report import",
        ["python3", "import_ar_reports.py"],
        timeout=240,
        blocker=import_blocker,
    ))

    if not args.skip_ringcentral:
        rc_blocker = None if r["ringcentral"]["ready"] else "RingCentral blocked: " + ", ".join(r["ringcentral"]["needs"])
        steps.append(run_step(
            "RingCentral call log sync",
            ["python3", "ringcentral_sync.py", "--hours", "24"],
            timeout=240,
            blocker=rc_blocker,
        ))
        steps.append(run_step(
            "RingCentral fax digest sync",
            ["python3", "ringcentral_fax_sync.py", "--days", "1"],
            timeout=240,
            blocker=rc_blocker,
        ))

    steps.append(reconciliation_step())
    payload = build_status("complete", steps)
    write_status(payload)
    print(json.dumps(payload, indent=2))
    return 1 if payload["summary"]["error"] else 0


if __name__ == "__main__":
    sys.exit(main())

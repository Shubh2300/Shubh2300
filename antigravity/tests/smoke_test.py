#!/usr/bin/env python3
"""
Smoke test for the Clinical Dashboard (Antigravity).

Stdlib-only (no pip installs). Boots server.py if it isn't already running,
checks the core API endpoints, and statically checks dashboard.html.

    python3 tests/smoke_test.py

Exit code 0 = all hard checks passed. Non-zero = something core is broken.

Note: the JS workload-balancer total check from CODEX_LOOSE_ENDS needs a
browser/JS runtime (not available here), so this covers the backend + markup.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PORT", "8000"))
BASE = f"http://localhost:{PORT}"

passes, warns, fails = [], [], []
def ok(m):   passes.append(m); print(f"  \033[32m✓\033[0m {m}")
def warn(m): warns.append(m);  print(f"  \033[33m!\033[0m {m}")
def fail(m): fails.append(m);  print(f"  \033[31m✗\033[0m {m}")


def port_open(host, port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def get(path, timeout=5):
    with urllib.request.urlopen(urllib.request.Request(BASE + path), timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


def main():
    print("Antigravity smoke test\n----------------------")

    # --- Static checks (no server needed) ---
    print("Static: dashboard.html")
    dash = os.path.join(ROOT, "dashboard.html")
    if os.path.exists(dash):
        ok("dashboard.html exists")
        with open(dash, encoding="utf-8") as f:
            html = f.read()
        for anchor in ("Clinical Operations Board", "API_BASE", "/api/patients"):
            (ok if anchor in html else fail)(f"dashboard.html contains {anchor!r}")
    else:
        fail("dashboard.html missing")

    # --- Server checks ---
    print("Server: boot + endpoints")
    proc = None
    if port_open("localhost", PORT):
        warn(f"port {PORT} already in use — testing the running server")
    else:
        proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        up = False
        for _ in range(40):  # ~10s max
            try:
                st, body = get("/api/health", timeout=1)
                if st == 200:
                    up = True
                    break
            except (urllib.error.URLError, ConnectionError, socket.timeout, OSError):
                time.sleep(0.25)

        if not up:
            fail("/api/health did not respond (server failed to boot)")
        else:
            data = json.loads(body)
            (ok if data.get("status") == "ok" else fail)("/api/health status == ok")
            (ok if "Clinical Dashboard" in data.get("service", "") else warn)(
                "/api/health service label present")
            # Data-dependent endpoints: warn (not fail) on non-200 to avoid flakiness
            for ep in ("/api/patients", "/api/tasks", "/api/agents/health"):
                try:
                    st, body = get(ep, timeout=4)
                    json.loads(body)
                    (ok if st == 200 else warn)(f"{ep} -> {st}, valid JSON")
                except Exception as e:  # noqa: BLE001
                    warn(f"{ep} -> error: {e}")

            # Billing: assert the field schema the frontend actually reads (catches
            # the stale-server field-mismatch that showed $NaN revenue).
            try:
                st, body = get("/api/billing", timeout=5)
                b = json.loads(body)
                needed = {"collected", "collectionRate", "scPaidCount", "ledgerRowCount"}
                missing = needed - b.keys()
                if st == 200 and not missing:
                    ok(f"/api/billing -> ${b.get('collected', 0):,.0f} collected, schema matches frontend")
                else:
                    fail(f"/api/billing schema mismatch (missing {missing or 'n/a'}, status {st}) — revenue will render $NaN")
            except Exception as e:  # noqa: BLE001
                warn(f"/api/billing -> error: {e}")

            try:
                st, body = get("/api/faxes/digest?filter=all", timeout=4)
                faxes = json.loads(body)
                needed = {"status", "summary", "faxes"}
                missing = needed - faxes.keys()
                if st == 200 and not missing and isinstance(faxes.get("faxes"), list):
                    ok("/api/faxes/digest -> valid digest schema")
                else:
                    fail(f"/api/faxes/digest schema mismatch (missing {missing or 'n/a'}, status {st})")
            except Exception as e:  # noqa: BLE001
                warn(f"/api/faxes/digest -> error: {e}")

            try:
                st, body = get("/api/automation/status", timeout=4)
                automation = json.loads(body)
                needed = {"status", "latestRun", "readiness"}
                missing = needed - automation.keys()
                if st == 200 and not missing and isinstance(automation.get("readiness"), dict):
                    ok("/api/automation/status -> valid automation readiness schema")
                else:
                    fail(f"/api/automation/status schema mismatch (missing {missing or 'n/a'}, status {st})")
            except Exception as e:  # noqa: BLE001
                warn(f"/api/automation/status -> error: {e}")

            try:
                st, body = get("/api/automation/loops", timeout=4)
                loops = json.loads(body)
                needed = {"status", "loops", "n8nPattern"}
                missing = needed - loops.keys()
                if st == 200 and not missing and isinstance(loops.get("loops"), list) and loops["loops"]:
                    ok("/api/automation/loops -> valid brain loop catalog")
                else:
                    fail(f"/api/automation/loops schema mismatch (missing {missing or 'n/a'}, status {st})")
            except Exception as e:  # noqa: BLE001
                warn(f"/api/automation/loops -> error: {e}")

            try:
                st, body = get("/api/clinical-os/modules", timeout=4)
                modules = json.loads(body)
                needed = {"status", "kernel", "modules", "contracts", "sellableGates"}
                missing = needed - modules.keys()
                if st == 200 and not missing and isinstance(modules.get("modules"), list) and modules["modules"]:
                    ok("/api/clinical-os/modules -> valid Clinical OS module catalog")
                else:
                    fail(f"/api/clinical-os/modules schema mismatch (missing {missing or 'n/a'}, status {st})")
            except Exception as e:  # noqa: BLE001
                warn(f"/api/clinical-os/modules -> error: {e}")

            try:
                st, body = get("/api/manager/review?limit=10", timeout=6)
                manager = json.loads(body)
                needed = {"status", "summary", "issues", "mission"}
                missing = needed - manager.keys()
                has_issue_ids = all("id" in issue for issue in manager.get("issues", [])[:5])
                if st == 200 and not missing and isinstance(manager.get("issues"), list) and has_issue_ids:
                    ok("/api/manager/review -> valid Manager Core review schema")
                else:
                    fail(f"/api/manager/review schema mismatch (missing {missing or 'n/a'}, status {st})")
            except Exception as e:  # noqa: BLE001
                warn(f"/api/manager/review -> error: {e}")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()

    print(f"\nSummary: {len(passes)} passed, {len(warns)} warnings, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

#!/bin/bash
# Cheap full verification for the Antigravity project.
# Syntax-checks every Python file, every inline <script> in dashboard.html,
# patients_data.js (if present), and smoke-tests the core API endpoints.
# Exits non-zero on any failure. Designed so a reviewer only needs the output.
set -u
cd "$(dirname "$0")/.."
FAIL=0

echo "── Python syntax ──"
for f in *.py; do
  python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null \
    || { echo "PY FAIL: $f"; python3 -c "import ast; ast.parse(open('$f').read())" 2>&1 | tail -2; FAIL=1; }
done
[ $FAIL -eq 0 ] && echo "all .py parse OK"

echo "── JS syntax (inline scripts + patients_data.js) ──"
python3 - <<'EOF' || FAIL=1
import re, subprocess, tempfile, os, sys
node = os.path.expanduser('~/.local/node-runtime/bin/node')
targets = []
html = open('dashboard.html', encoding='utf-8').read()
for i, s in enumerate(re.findall(r'<script(?![^>]*src)[^>]*>(.*?)</script>', html, re.S)):
    targets.append((f"dashboard inline #{i}", s))
pdata = os.path.expanduser('~/.gemini/antigravity/scratch/patients_data.js')
if os.path.exists(pdata):
    targets.append(("patients_data.js", open(pdata, encoding='utf-8').read()))
ok = True
for label, code in targets:
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as f:
        f.write(code); p = f.name
    r = subprocess.run([node, '--check', p], capture_output=True, text=True)
    if r.returncode:
        ok = False
        print(f"JS FAIL [{label}]: {r.stderr[:300]}")
    os.unlink(p)
print("all JS parse OK" if ok else "JS ERRORS")
sys.exit(0 if ok else 1)
EOF

echo "── API smokes (server must be running on :8000) ──"
for ep in "/api/health" "/api/patients?limit=1" "/api/billing" "/api/followups"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://localhost:8000$ep")
  if [ "$code" = "200" ]; then echo "200 $ep"; else echo "ENDPOINT FAIL: $ep -> $code"; FAIL=1; fi
done

if [ $FAIL -eq 0 ]; then echo "VERIFY OK"; else echo "VERIFY FAILED"; fi
exit $FAIL

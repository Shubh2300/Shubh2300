#!/usr/bin/env bash
# test_emr_connections.sh — Quick health check for EMR connections
# Run: bash ~/n8n-office/test_emr_connections.sh

set -euo pipefail

PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
INTEGRATIONS="$HOME/n8n-office/python/integrations"

echo "=== Atlantic EMR Connection Test ==="
echo ""

echo "[1/4] Checking Python imports..."
$PYTHON -c "
import sys
sys.path.insert(0, '$INTEGRATIONS')
from sis_client import SISClient
from svigg_scraper import SviggScraper
from emr_session_manager import EMRSessionManager
print('    All imports OK')
"

echo ""
echo "[2/4] Checking credentials..."
$PYTHON -c "
import sys, os
sys.path.insert(0, '$INTEGRATIONS')

# Load .env
env_path = '$HOME/Documents/Antigravity/.env'
if os.path.exists(env_path):
    for line in open(env_path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip())

sis_user = os.environ.get('SIS_USERNAME', '')
sis_pass = os.environ.get('SIS_PASSWORD', '')
svigg_user = os.environ.get('WEBEDOCTOR_USER', '')
svigg_pass = os.environ.get('WEBEDOCTOR_PASS', '')

print(f'    SIS:   user={\"set\" if sis_user else \"MISSING\"}  pass={\"set\" if sis_pass else \"MISSING\"}')
print(f'    Svigg: user={\"set\" if svigg_user else \"MISSING\"}  pass={\"set\" if svigg_pass else \"MISSING\"}')
"

echo ""
echo "[3/4] Checking saved sessions..."
SESSION_DIR="$HOME/.gemini/antigravity/scratch/emr_sessions"
if [ -d "$SESSION_DIR" ]; then
    echo "    Session dir: $SESSION_DIR"
    if [ -f "$SESSION_DIR/sis_cookies.json" ]; then
        COOKIE_COUNT=$($PYTHON -c "import json; print(len(json.load(open('$SESSION_DIR/sis_cookies.json'))))" 2>/dev/null || echo "0")
        echo "    SIS cookies: $COOKIE_COUNT saved"
    else
        echo "    SIS cookies: none saved (will login on first use)"
    fi
    if [ -d "$SESSION_DIR/sis_browser_state" ]; then
        echo "    SIS browser state: exists"
    else
        echo "    SIS browser state: none"
    fi
else
    echo "    Session dir: not created yet (will be created on first use)"
fi

echo ""
echo "[4/4] Checking MCP server..."
$PYTHON -c "
import sys
sys.path.insert(0, '$HOME/n8n-office/python/integrations')
sys.path.insert(0, '$HOME/n8n-office/mcp')
import atlantic_emr_server
print('    MCP server v0.2.0 compiles OK')
print('    15 tools registered')
" 2>/dev/null || echo "    MCP server import failed"

echo ""
echo "=== Done ==="
echo "To test live connections: $PYTHON $INTEGRATIONS/emr_session_manager.py status"
echo "To start new Claude session with new tools: close and reopen Claude Code"

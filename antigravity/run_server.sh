#!/bin/bash
# run_server.sh — keeps server.py alive automatically
# Restarts within 2 seconds any time it crashes or stops

cd "$(dirname "$0")"

echo "=== Antigravity Server Auto-Restart Wrapper ==="
echo "Starting server... Press Ctrl+C to stop."

while true; do
    python3 -u server.py
    EXIT_CODE=$?
    echo ""
    echo "[$(date)] Server stopped (exit code $EXIT_CODE). Restarting in 2 seconds..."
    sleep 2
done

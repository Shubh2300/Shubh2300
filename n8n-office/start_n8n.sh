#!/usr/bin/env bash
# start_n8n.sh — launch n8n on localhost:5678 with the office config.
# Usage: ./start_n8n.sh            (foreground, logs to stdout + logs/n8n.log)
#        ./start_n8n.sh --bg       (background, logs to logs/n8n.log, writes PID)
#        ./start_n8n.sh stop       (kill the backgrounded instance via PID file)
#        ./start_n8n.sh status     (is it running? on what PID?)

set -euo pipefail

BASE="/Users/shubh/n8n-office"
ENV_FILE="$BASE/.env"
LOG_DIR="$BASE/logs"
LOG_FILE="$LOG_DIR/n8n.log"
PID_FILE="$BASE/n8n.pid"
N8N_BIN="$BASE/node_modules/.bin/n8n"
NODE_BIN_DIR="$BASE/.local/node/bin"

mkdir -p "$LOG_DIR" "$BASE/data"

# Ensure our pinned Node 22 wins over any system Node.
export PATH="$NODE_BIN_DIR:$PATH"

# Load .env (export every var; ignore comments + blanks).
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# n8n requires the encryption-key file to be chmod 600.
chmod 600 "$ENV_FILE" || true

if [[ ! -x "$N8N_BIN" ]]; then
  echo "ERROR: n8n not installed at $N8N_BIN" >&2
  echo "Run: $BASE/install_n8n.sh" >&2
  exit 1
fi

cmd="${1:-fg}"

case "$cmd" in
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "n8n running (PID $(cat "$PID_FILE")) on http://${N8N_HOST}:${N8N_PORT}/"
      exit 0
    fi
    if pgrep -f "node_modules/.bin/n8n" >/dev/null; then
      echo "n8n running (no PID file) — pgrep matches:"
      pgrep -lf "node_modules/.bin/n8n"
      exit 0
    fi
    echo "n8n not running."
    exit 1
    ;;
  stop)
    if [[ -f "$PID_FILE" ]]; then
      pid="$(cat "$PID_FILE")"
      if kill -0 "$pid" 2>/dev/null; then
        echo "Stopping n8n (PID $pid)…"
        kill "$pid"
        sleep 2
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
      fi
      rm -f "$PID_FILE"
    fi
    pkill -f "node_modules/.bin/n8n" 2>/dev/null || true
    echo "Stopped."
    exit 0
    ;;
  --bg|bg)
    echo "Starting n8n in background → $LOG_FILE"
    nohup "$N8N_BIN" start >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    echo "PID $(cat "$PID_FILE"). Open http://${N8N_HOST}:${N8N_PORT}/"
    ;;
  fg|"")
    echo "Starting n8n in foreground on http://${N8N_HOST}:${N8N_PORT}/"
    echo "Tee'ing to $LOG_FILE — Ctrl-C to stop."
    exec "$N8N_BIN" start 2>&1 | tee -a "$LOG_FILE"
    ;;
  *)
    echo "Usage: $0 [fg|--bg|stop|status]" >&2
    exit 2
    ;;
esac

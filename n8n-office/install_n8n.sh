#!/usr/bin/env bash
# install_n8n.sh — re-runnable installer for n8n on macOS Intel.
# Run this if the initial install was interrupted, or to upgrade n8n.
#
# Prereqs already in place:
#   - Node 22 pinned at /Users/shubh/n8n-office/.local/node/bin/
#   - This directory tree owned by $USER
#
# Common failure mode on Intel Mac: native modules (better-sqlite3, sharp,
# bcrypt) need Xcode CLT to build. If you see "gyp ERR" or "no such file:
# stdlib.h", run:  xcode-select --install   then re-run this script.

set -euo pipefail

BASE="/Users/shubh/n8n-office"
NODE_BIN_DIR="$BASE/.local/node/bin"
LOG="$BASE/logs/install_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$BASE/logs"
cd "$BASE"

export PATH="$NODE_BIN_DIR:$PATH"

echo "==> Node $(node --version) / npm $(npm --version)" | tee "$LOG"
echo "==> Installing n8n into $BASE …" | tee -a "$LOG"

# --omit=optional skips a few platform-specific binaries that often fail on
# Intel Mac (e.g. @swc/core variants). n8n itself does not require them.
# --no-audit / --no-fund cut noise.
npm install n8n \
  --prefix "$BASE" \
  --no-audit \
  --no-fund \
  --omit=optional \
  2>&1 | tee -a "$LOG"

if [[ ! -x "$BASE/node_modules/.bin/n8n" ]]; then
  echo "FAIL: n8n binary not present after install. Check $LOG." >&2
  exit 1
fi

echo "==> Installed $($BASE/node_modules/.bin/n8n --version 2>/dev/null || echo '?')"
echo "==> Next: $BASE/start_n8n.sh"

# atlantic-emr MCP Server

MCP server exposing Atlantic Pain & Wellness EMR read-only operations as
tools callable from any Claude Code session (v0.1.0).

## What this exposes

Seven tools, all read-only:

| Tool | What it does |
|---|---|
| `lookup_patient` | Fuzzy-match a patient by name/phone/email across SIS Complete and Svigg / Dr.Com / WEBeDoctor |
| `check_appointment_book` | Scan intake/surgery dates for a date range (defaults to SIS) |
| `check_billing` | Retrieve billing/ledger summary for a patient across all sources |
| `sis_session_status` | Check whether the SIS browser session is still valid (no PHI returned) |
| `read_audit_log` | Read recent rows from the EMR bridge audit log |
| `clear_cache` | Delete stale cached lookups from the local SQLite cache |
| `health` | Per-component status check (emr_bridge, Antigravity dir, cache DB, SIS state file) |

Write/mutation tools (booking, cancelling appointments, posting payments) are
intentionally not exposed. emr_bridge returns NotImplementedError for those
until a staff sign-off flow exists.

## How to call from a Claude session

Just ask Claude naturally. Examples:

- "Look up patient Jane Smith in SIS" → Claude picks `lookup_patient`
- "What appointments are scheduled between 2026-07-01 and 2026-07-07?" → `check_appointment_book`
- "Check billing status for John Doe across all EMRs" → `check_billing`
- "Is the SIS session still valid?" → `sis_session_status`
- "Show me the last 10 audit log entries" → `read_audit_log`
- "Clear the WEBeDoctor cache" → `clear_cache`
- "Are all EMR bridge components healthy?" → `health`

## Privacy / PHI handling

- `lookup_patient`, `check_appointment_book`, and `check_billing` return
  Protected Health Information. Every tool call is written to the audit log
  at `/Users/shubh/n8n-office/cache/emr_cache.db` (table: `audit_log`).
- The MCP tool descriptions warn Claude explicitly: "do not echo raw output
  to chat or store it outside the audit log."
- The audit log itself hashes patient IDs (SHA-256 + pepper) per HIPAA
  §164.312(b); it is not a second patient index.
- No credentials are stored in this server. The server inherits the
  environment from Claude Code; emr_bridge reads Antigravity's `.env` at
  subprocess time.

## Registration

The server is registered in `~/.claude.json` under `mcpServers`:

```json
"atlantic-emr": {
  "type": "stdio",
  "command": "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
  "args": ["/Users/shubh/n8n-office/mcp/atlantic_emr_server.py"],
  "env": {
    "ANTIGRAVITY_DIR": "/Users/shubh/Documents/Antigravity"
  }
}
```

## How to disable

```bash
claude mcp remove atlantic-emr --scope user
```

Or edit `~/.claude.json` and delete the `atlantic-emr` entry under `mcpServers`.

## Reload note

Claude Code reads MCP config at session start. To pick up this server (or
any changes to it), quit and reopen your Claude Code session.

## Dependencies

- Python 3.14 at `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3`
- `mcp[cli]` SDK (installed via pip3 --user, version 1.28.1)
- `emr_bridge` and `audit_log` from `/Users/shubh/n8n-office/python/integrations/`
- Antigravity repo at `/Users/shubh/Documents/Antigravity` (read-only)
- Cache DB at `/Users/shubh/n8n-office/cache/emr_cache.db`
- SIS session state at `~/.gemini/antigravity/scratch/sis_browser_state.json`

## Live EMR data

Real portal lookups require a valid SIS browser session. Check status with
the `sis_session_status` tool. If the session is expired, staff must
re-authenticate manually via the Antigravity dashboard (the bridge
deliberately does not auto-trigger 2FA from background jobs).

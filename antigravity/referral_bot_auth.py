#!/usr/bin/env python3
"""
referral_bot_auth.py — one-time Gmail OAuth consent for the referral-capture bot.

Mailbox mainlinesurgery@gmail.com is a CONSUMER Gmail account, so a service
account is a dead end. The standalone bot needs its own OAuth Desktop client +
refresh token. This script runs the consent flow ONCE:

  1. You download an OAuth *Desktop app* client JSON from Google Cloud Console
     and save it OUTSIDE the repo (default:
     ~/.gemini/antigravity/scratch/gmail_oauth_client.json).
  2. You run this script. A browser opens; sign in as mainlinesurgery@gmail.com
     and approve read-only Gmail access.
  3. The script stores the authorized token (with refresh token) to
     ~/.gemini/antigravity/scratch/gmail_token.json and prints the exact
     GMAIL_OAUTH_* lines to paste into .env.

Scope: gmail.readonly only — capture never sends or modifies mail.
No secrets are committed; client JSON and token file live in scratch (gitignored)
and are never echoed in full to the terminal.
"""

import os
import sys
import json
import argparse

SCRATCH_DIR = os.environ.get(
    "ANTIGRAVITY_SCRATCH_DIR",
    os.path.expanduser("~/.gemini/antigravity/scratch"),
)
DEFAULT_CLIENT_PATH = os.path.join(SCRATCH_DIR, "gmail_oauth_client.json")
DEFAULT_TOKEN_PATH = os.environ.get(
    "GMAIL_TOKEN_PATH", os.path.join(SCRATCH_DIR, "gmail_token.json")
)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _print_setup_help(client_path: str):
    print("""
────────────────────────────────────────────────────────────────────────────
  Gmail OAuth setup for the referral-capture bot (one-time)
────────────────────────────────────────────────────────────────────────────
Before running this script you need an OAuth *Desktop app* client JSON:

  1. Go to https://console.cloud.google.com/  (sign in as mainlinesurgery@gmail.com
     or the project owner).
  2. Create/select a project → "APIs & Services".
  3. Enable the "Gmail API" for the project.
  4. "OAuth consent screen": User type = External, add mainlinesurgery@gmail.com
     as a Test user (keeps you in testing mode — fine for a single mailbox).
  5. "Credentials" → "Create Credentials" → "OAuth client ID" →
     Application type = "Desktop app". Download the JSON.
  6. Save that JSON to:
         %s
     (outside the repo, already gitignored — never commit it.)

Then re-run:  python3 referral_bot_auth.py
────────────────────────────────────────────────────────────────────────────
""" % client_path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One-time Gmail OAuth consent for the referral bot."
    )
    parser.add_argument(
        "--client", default=DEFAULT_CLIENT_PATH,
        help="path to the downloaded OAuth Desktop client JSON",
    )
    parser.add_argument(
        "--token", default=DEFAULT_TOKEN_PATH,
        help="where to write the authorized token JSON",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="use console (copy/paste) flow instead of opening a browser",
    )
    args = parser.parse_args(argv)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except Exception:
        print("ERROR: google-auth-oauthlib is not installed.\n"
              "  python3 -m pip install google-api-python-client google-auth-oauthlib",
              file=sys.stderr)
        return 2

    if not os.path.exists(args.client):
        print("ERROR: OAuth client JSON not found at:\n  %s" % args.client,
              file=sys.stderr)
        _print_setup_help(args.client)
        return 2

    os.makedirs(os.path.dirname(args.token), exist_ok=True)

    print("Starting Gmail OAuth consent (scope: gmail.readonly)…")
    print("Sign in as mainlinesurgery@gmail.com and approve read-only access.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(args.client, GMAIL_SCOPES)
        if args.no_browser:
            creds = flow.run_console()
        else:
            # port=0 → pick a free localhost port for the redirect.
            creds = flow.run_local_server(port=0, prompt="consent")
    except Exception as e:
        print("ERROR: consent flow failed: %s" % e, file=sys.stderr)
        return 1

    if not creds or not creds.refresh_token:
        print("ERROR: no refresh token returned. Re-run and ensure you grant "
              "consent (the flow requests prompt=consent to force one).",
              file=sys.stderr)
        return 1

    # Persist the full authorized token (includes refresh token) to scratch.
    with open(args.token, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    try:
        os.chmod(args.token, 0o600)
    except OSError:
        pass

    # Pull client id/secret from the token/client JSON to print .env lines.
    client_id = getattr(creds, "client_id", "") or ""
    client_secret = getattr(creds, "client_secret", "") or ""

    print("\n✅ Success. Token saved to:\n  %s\n" % args.token)
    print("────────────────────────────────────────────────────────────────────")
    print("Add these lines to your .env (the bot reads them via os.environ).")
    print("Either of two ways works — env triple OR the token file path:\n")
    print("# Option A — full OAuth triple in .env:")
    print("GMAIL_OAUTH_CLIENT_ID=%s" % client_id)
    print("GMAIL_OAUTH_CLIENT_SECRET=%s" % client_secret)
    print("GMAIL_OAUTH_REFRESH_TOKEN=%s" % creds.refresh_token)
    print("\n# Option B — just point at the token file (already written):")
    print("GMAIL_TOKEN_PATH=%s" % args.token)
    print("────────────────────────────────────────────────────────────────────")
    print("\nThen verify:  python3 referral_bot.py --once")
    print("The bot should report gmail_connected:true in referral_state.json.\n")
    print("NOTE: these are secrets — keep them in .env only (gitignored). "
          "Do not paste them into chat, commits, or screenshots.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

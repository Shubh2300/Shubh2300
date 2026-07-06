#!/bin/bash
# Starts the local freellmapi proxy (pools free LLM provider tiers behind
# one OpenAI-compatible endpoint on http://localhost:3001).
# Dashboard: http://localhost:3001  — manage provider keys + unified key there.
export PATH="$HOME/.local/node-runtime/bin:$PATH"
cd "$HOME/Apps/freellmapi"
exec npm run start -w server

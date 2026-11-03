#!/usr/bin/env bash
# Linux launcher: creates the venv on first run, then starts the bot.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python bot.py

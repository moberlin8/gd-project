#!/bin/bash
# Start Lemieux Telegram Bot
# Usage: ./start_lembot.sh
#
# Requires: LEMIEUX_TELEGRAM_TOKEN in environment or .env file

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

if [ -z "${LEMIEUX_TELEGRAM_TOKEN:-}" ]; then
    echo "❌ LEMIEUX_TELEGRAM_TOKEN not set."
    echo "   1. Talk to @BotFather on Telegram"
    echo "   2. Copy token to $SCRIPT_DIR/.env"
    exit 1
fi

# Install deps if missing (idempotent)
PY=/usr/bin/python3   # system 3.12 — where faiss/sentence-transformers/telegram live
"$PY" -m pip install --break-system-packages --quiet python-telegram-bot 2>/dev/null || true

exec "$PY" "$SCRIPT_DIR/lembot_telegram.py"

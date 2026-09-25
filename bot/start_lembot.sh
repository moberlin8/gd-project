#!/bin/bash
# Start Lemieux GD Telegram Bot (auto-restart on crash)
# Usage: ./start_lembot.sh
#
# Requires: LEMIEUX_TELEGRAM_TOKEN in environment or .env file

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Load .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

# Auto-refresh Nous bearer token from Hermes auth file (JWT expires hourly;
# a stale .env XAI_API_KEY causes silent 401s and the bot falls back to
# extractive-only results with no user-visible error)
NOUS_AUTH="/home/hermes/.hermes/shared/nous_auth.json"
if [ -f "$NOUS_AUTH" ]; then
    NOUS_TOKEN=$(python3 -c "import json; print(json.load(open('$NOUS_AUTH')).get('access_token',''))" 2>/dev/null)
    if [ -n "$NOUS_TOKEN" ]; then
        export XAI_API_KEY="$NOUS_TOKEN"
    fi
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

LOG_FILE="$SCRIPT_DIR/logs/lembot_telegram.log"
PID_FILE="$SCRIPT_DIR/lembot_telegram.pid"
mkdir -p "$(dirname "$LOG_FILE")"

# Auto-restart loop: kill old PID if stale, restart on exit
while true; do
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      kill "$OLD_PID" 2>/dev/null
      sleep 2
    fi
  fi

  "$PY" "$SCRIPT_DIR/lembot_telegram.py" 2>>"$LOG_FILE" >>"$LOG_FILE" &
  echo $! > "$PID_FILE"

  # Block until the bot exits naturally — do NOT kill a healthy process on the next loop.
  wait $! 2>/dev/null
  EXIT_CODE=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot exited (code $EXIT_CODE), restarting in 5s…" >>"$LOG_FILE"
  sleep 5  # Brief pause before restart to avoid spam
done

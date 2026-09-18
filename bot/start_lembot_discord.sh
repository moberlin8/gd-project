#!/bin/bash
# Start Lemieux Discord bot (text only)
# Usage: ./start_lembot_discord.sh
#
# Reads bot/.env for LEMIEUX_DISCORD_TOKEN / LEMIEUX_CHANNEL_IDS.
# XAI_API_KEY is pulled from ~/.hermes/.env if not already in bot/.env.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

set -a
[ -f "$SCRIPT_DIR/.env" ] && . "$SCRIPT_DIR/.env"
if [ -z "${XAI_API_KEY:-}" ] && [ -f "$HOME/.hermes/.env" ]; then
    XAI_API_KEY="$(grep -E '^XAI_API_KEY=' "$HOME/.hermes/.env" | cut -d= -f2- || true)"
fi
set +a

if [ -z "${LEMIEUX_DISCORD_TOKEN:-}" ]; then
    echo "❌ LEMIEUX_DISCORD_TOKEN not set. See $SCRIPT_DIR/.env.example"
    exit 1
fi

exec /usr/bin/python3 "$SCRIPT_DIR/lembot_discord.py" "$@"   # system 3.12 has faiss/discord.py

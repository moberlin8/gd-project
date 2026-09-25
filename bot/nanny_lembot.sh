#!/bin/bash
# Nanny for Lemieux GD Telegram Bot — restart if dead
BOT_DIR="/home/mao/DaveMatt/gd-project/bot"
if ! pgrep -f "lembot_telegram.py" > /dev/null 2>&1; then
    cd "$BOT_DIR" && setsid ./start_lembot.sh >> logs/nanny_lembot.log 2>&1 &
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot was dead — restarted (PID: $!)" >> "$BOT_DIR/logs/nanny_lembot.log"
fi

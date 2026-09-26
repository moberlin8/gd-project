#!/bin/bash
# Nanny for singer-tag inference approval Flask app — restart if dead
APPROVAL_DIR="/home/mao/DaveMatt/gd-project/scripts/inference_approval"
PYTHON="/home/mao/DaveMatt/gd-project/.venv_approval/bin/python3"
PORT=8090

if ! ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
    cd "$APPROVAL_DIR" && APPROVAL_HOST=127.0.0.1 setsid "$PYTHON" app.py >> logs/singer_tag_nanny.log 2>&1 &
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Singer-tag app dead — restarted (PID: $!)" >> "$APPROVAL_DIR/logs/singer_tag_nanny.log"
fi

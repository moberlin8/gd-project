#!/bin/bash
# build_index_overnight.sh — GD RAT FAISS index rebuild wrapper (no LLM)
# Rebuilds the entire FAISS index from gd_comments_combined.json + gd_lyrics.json
# via build_faiss_incremental.py. Safe to run on a schedule (idempotent full rebuild).
# Usage: ./build_index_overnight.sh
set -uo pipefail

PROJECT_DIR="/home/mao/DaveMatt/gd-project"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/build_index_overnight.log"
mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

# Ensure user site-packages are on the path (cron environment is minimal)
export PYTHONPATH="${PYTHONPATH:+${PYTHONPATH}:}/home/hermes/.local/lib/python3.12/site-packages"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting FAISS index rebuild..." >>"$LOG_FILE"
/usr/bin/python3 scripts/build_faiss_incremental.py >>"$LOG_FILE" 2>&1
RC=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Rebuild finished with exit code $RC" >>"$LOG_FILE"
exit $RC

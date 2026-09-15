#!/bin/bash
# run_lyrics_overnight.sh — GD lyrics + interpretations scraper overnight runner
# Runs nightly (Hermes cron, 02:00 UTC) scraping up to N songs per run, resuming
# from the scraper's saved state (data/gd_lyrics_state.json). Mirrors
# run_scraper.sh: PID lock against overlapping runs, timestamped logging to
# scripts/gd_lyrics_overnight.log, and a markdown summary to
# output/gd_lyrics_scraper_summary.md written after each attempt.
#
# Usage: ./run_lyrics_overnight.sh [--limit N] [--delay S] [--max-retries N] [--retry-pause S]
#
# Env overrides:
#   GD_LYRICS_LIMIT        (default 50)
#   GD_LYRICS_DELAY        (default 3.25)
#   GD_LYRICS_MAX_RETRIES  (default 3)
#   GD_LYRICS_RETRY_PAUSE  (default 30)

set -uo pipefail

PROJECT_DIR="/home/mao/DaveMatt/gd-project"
SCRAPER="$PROJECT_DIR/scrapers/gd_lyrics_scraper.py"
LOG_FILE="$PROJECT_DIR/scripts/gd_lyrics_overnight.log"
SUMMARY_FILE="$PROJECT_DIR/output/gd_lyrics_scraper_summary.md"
STATE_FILE="$PROJECT_DIR/data/gd_lyrics_state.json"
DATA_FILE="$PROJECT_DIR/data/gd_lyrics.json"

# Defaults
LIMIT="${GD_LYRICS_LIMIT:-50}"
DELAY="${GD_LYRICS_DELAY:-3.25}"
MAX_RETRIES="${GD_LYRICS_MAX_RETRIES:-3}"
RETRY_PAUSE="${GD_LYRICS_RETRY_PAUSE:-30}"

# CLI overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
        --limit)       LIMIT="$2";       shift 2 ;;
        --delay)       DELAY="$2";       shift 2 ;;
        --max-retries) MAX_RETRIES="$2"; shift 2 ;;
        --retry-pause) RETRY_PAUSE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$PROJECT_DIR/output"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# PID lock: prevent overlapping cron runs (must be defined before use)
PID_FILE="$PROJECT_DIR/scripts/gd_lyrics_scraper.pid"
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        log "Lyrics scraper already running (PID $OLD_PID) — skipping this run"
        exit 0
    else
        # Stale PID file — remove it
        rm -f "$PID_FILE"
    fi
fi
echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

log "========================================"
log "Starting GD lyrics scraper overnight run"
log "  Limit: $LIMIT songs | Delay: ${DELAY}s"
log "  Max retries: $MAX_RETRIES | Retry pause: ${RETRY_PAUSE}s"
log "  Scraper: $SCRAPER"
log "========================================"

cd "$PROJECT_DIR"

attempt=1
SCRAPER_EXIT=0
while [ "$attempt" -le "$MAX_RETRIES" ]; do
    log "--- Attempt $attempt/$MAX_RETRIES ---"
    START_TIME=$SECONDS

    python3 "$SCRAPER" --run --limit "$LIMIT" --delay "$DELAY" 2>&1 | tee -a "$LOG_FILE"
    SCRAPER_EXIT=${PIPESTATUS[0]}
    ELAPSED_MIN=$(( (SECONDS - START_TIME) / 60 ))

    # --- Write summary from current state ---
    python3 - "$STATE_FILE" "$DATA_FILE" "$SUMMARY_FILE" "$SCRAPER_EXIT" "$ELAPSED_MIN" "$attempt" "$LIMIT" "$DELAY" <<'PYEOF'
import json, sys
from datetime import datetime

state_file, data_file, summary_file, exit_code, elapsed_min, attempt, limit, delay = sys.argv[1:9]

try:
    with open(state_file) as f:
        state = json.load(f)
except FileNotFoundError:
    state = {}
try:
    with open(data_file) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}

done = state.get("done", {}) or {}
songs_done = len(done)
songs_total = state.get("songs_total", 0)
coverage = state.get("coverage", {})
last_run = "unknown"
runs = state.get("runs", [])
if runs:
    last_run = runs[-1].get("finished_at", "unknown")

exit_meanings = {
    0: "Completed successfully",
    1: "General error",
    2: "Misuse (bad arguments)",
    130: "Interrupted by SIGINT (Ctrl+C)",
    143: "Terminated by SIGTERM",
}
exit_desc = exit_meanings.get(int(exit_code), f"Unknown (code {exit_code})")

summary = f"""# GD Lyrics Scraper Run — Summary

**Run finished:** {datetime.now().isoformat()}
**Attempt:** {attempt} | **Duration:** {elapsed_min} min
**Exit code:** {exit_code} ({exit_desc})

## Progress
| Metric                 | Value   |
|------------------------|---------|
| Songs completed        | {songs_done} / {songs_total} |
| Songs in output file   | {len(data)} |
| Corpus (songlist)      | {coverage.get('corpus_songs', 'n/a')} |
| Matched dead.net       | {coverage.get('matched_dead_net', 'n/a')} |
| Matched whitegum       | {coverage.get('matched_whitegum', 'n/a')} |
| Last finished run      | {last_run} |

## Notes
- Resumable: completed songs tracked in `data/gd_lyrics_state.json`
- This run processed up to {limit} new songs (limit), delay {delay}s
- Full log: `scripts/gd_lyrics_overnight.log`
"""
with open(summary_file, "w") as f:
    f.write(summary)
print(f"Summary written to {summary_file}")
PYEOF

    if [ "$SCRAPER_EXIT" -eq 0 ]; then
        log "Lyrics scrape completed successfully. Elapsed: ${ELAPSED_MIN} min"
        log "Summary: $SUMMARY_FILE"
        exit 0
    fi

    if [ "$attempt" -lt "$MAX_RETRIES" ]; then
        log "Lyrics scraper exited with code $SCRAPER_EXIT — pausing ${RETRY_PAUSE}s before retry..."
        sleep "$RETRY_PAUSE"
    fi

    attempt=$((attempt + 1))
done

log "⚠️  Lyrics scraper failed after $MAX_RETRIES attempts. Last exit code: $SCRAPER_EXIT"
exit "$SCRAPER_EXIT"

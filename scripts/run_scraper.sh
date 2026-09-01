#!/bin/bash
# run_scraper.sh — GD Comment Scraper 24/7 wrapper with retries
# Runs the 1965-1995 year-by-year sweep with 3.25s delays between requests.
# Runs continuously — no time window restriction. Resumes from saved state.
# Implements retry-on-failure: if the scraper times out or crashes, it pauses
# and restarts automatically, preserving progress via the state file.
#
# Usage: ./run_scraper.sh [--target N] [--delay S] [--max-retries N]
#
# Env:
#   GD_SCRAPER_STATE_FILE  (default: data/scraper_state.json)
#
# After completion, a summary is written to output/scraping_summary.md
# (Discord delivery is handled by the cron auto-deliver platform config.)

set -uo pipefail

PROJECT_DIR="/home/mao/DaveMatt/gd-project"
SCRAPER="$PROJECT_DIR/scrapers/gd_comment_scraper.py"
LOG_FILE="$PROJECT_DIR/scripts/247_scraper.log"
SUMMARY_FILE="$PROJECT_DIR/output/scraping_summary.md"
STATE_FILE="$PROJECT_DIR/data/scraper_state.json"

# Defaults: 3.25s delay (user preference), target sized for 3-hour cron window
DELAY="${GD_SCRAPER_DELAY:-3.25}"
TARGET="${GD_SCRAPER_TARGET:-3000}"
MAX_RETRIES="${GD_SCRAPER_MAX_RETRIES:-3}"
RETRY_PAUSE="${GD_SCRAPER_RETRY_PAUSE:-30}"

# Allow CLI overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --delay)  DELAY="$2";  shift 2 ;;
        --max-retries) MAX_RETRIES="$2"; shift 2 ;;
        --retry-pause) RETRY_PAUSE="$2"; shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$PROJECT_DIR/output"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

log "========================================"
log "Starting GD scraper (24/7 mode with retries)"
log "  Target: $TARGET shows | Delay: ${DELAY}s"
log "  Max retries: $MAX_RETRIES | Retry pause: ${RETRY_PAUSE}s"
log "  Scraper: $SCRAPER"
log "========================================"

cd "$PROJECT_DIR"

attempt=1
while [ $attempt -le $MAX_RETRIES ]; do
    log "--- Attempt $attempt/$MAX_RETRIES ---"

    START_TIME=$SECONDS
    SCRAPER_EXIT=0

    # Run the scraper; it logs to its own gd_scraper.log as well
    python3 "$SCRAPER" --target "$TARGET" --delay "$DELAY" 2>&1 | tee -a "$LOG_FILE"
    SCRAPER_EXIT=${PIPESTATUS[0]}

    ELAPSED_MIN=$(( (SECONDS - START_TIME) / 60 ))
    FINISH_HOUR=$(date +%H)

    # --- Write summary ---
    python3 - "$STATE_FILE" "$SUMMARY_FILE" "$SCRAPER_EXIT" "$ELAPSED_MIN" "$FINISH_HOUR" "$attempt" <<'PYEOF'
import json, sys, time
from datetime import datetime

state_file, summary_file, exit_code, elapsed_min, finish_hour, attempt = sys.argv[1:7]

with open(state_file) as f:
    state = json.load(f)

try:
    with open(state_file.replace("scraper_state.json", "gd_comments_combined.json")) as f:
        combined = json.load(f)
except FileNotFoundError:
    combined = {"metadata": {}}

meta = combined.get("metadata", {})

exit_meanings = {
    0: "Completed successfully",
    1: "General error",
    2: "Misuse (bad arguments)",
    130: "Interrupted by SIGINT (Ctrl+C)",
    143: "Terminated by SIGTERM",
}
exit_desc = exit_meanings.get(exit_code, f"Unknown (code {exit_code})")

summary = f"""# GD Scraper Run — Summary

**Run finished:** {datetime.now().isoformat()}
**Attempt:** {attempt}
**Duration:** {elapsed_min} min
**Exit code:** {exit_code} ({exit_desc})
**Finish time:** {finish_hour}:00 UTC

## Progress
| Metric              | Value   |
|---------------------|---------|
| Total shows attempted | {meta.get('shows_attempted', len(state['processed_ids']))} |
| Shows with comments   | {state['shows_with_comments']} |
| Total comments        | {meta.get('comments_total', state['total_comments'])} |
| Total setlists        | {state['total_setlists']} |
| Next year to scrape   | {state['current_year']} |

## State
- State file: `{state_file}`
- Combined data: `data/gd_comments_combined.json` ({meta.get('comments_total', state['total_comments'])} comments)
- Last save: {state.get('last_save', 'never')}

## Notes
- Scraper resumed from last saved state
- Year-by-year sweep: 1965→1995 (scraper loop exits at year 1995)
- Delay: 3.25s per request (user preference)
- Mode: 24/7 — runs every hour via cron, auto-resumes from saved state
- Full log: `scripts/247_scraper.log`
"""
with open(summary_file, "w") as f:
    f.write(summary)
print(f"Summary written to {summary_file}")
PYEOF

    # Check exit code
    if [ "$SCRAPER_EXIT" -eq 0 ]; then
        log "Scrape run completed successfully. Elapsed: ${ELAPSED_MIN} min"
        log "Summary: $SUMMARY_FILE"
        exit 0
    fi

    # If not the last attempt, pause and retry
    if [ $attempt -lt $MAX_RETRIES ]; then
        log "Scraper exited with code $SCRAPER_EXIT — pausing ${RETRY_PAUSE}s before retry..."
        log "State saved at ${SECONDS}s elapsed. Next attempt will resume from saved state."
        sleep "$RETRY_PAUSE"
    fi

    attempt=$((attempt + 1))
done

log "⚠️  Scraper failed after $MAX_RETRIES attempts. Last exit code: $SCRAPER_EXIT"
log "State was saved on the last graceful shutdown — next cron run will retry."
exit "$SCRAPER_EXIT"

#!/bin/bash
# run_overnight.sh — GD Comment Scraper overnight runner
# Runs between 2am-5am, scraping ~100 shows per night
# Stops automatically at 5am

set -e

PROJECT_DIR="/home/mao/DaveMatt/gd-project"
SCRAPER="$PROJECT_DIR/scrapers/gd_comment_scraper.py"
LOG_FILE="$PROJECT_DIR/scripts/overnight_scraper.log"
STATE_FILE="$PROJECT_DIR/data/scraper_state.json"

# Enforce 2am-5am window
CURRENT_HOUR=$(date +%H)
if [ "$CURRENT_HOUR" -lt 2 ] || [ "$CURRENT_HOUR" -ge 5 ]; then
    echo "$(date): Not in 2am-5am window (current hour: $CURRENT_HOUR). Exiting." >> "$LOG_FILE"
    exit 0
fi

echo "$(date): Starting overnight GD scraper run..." >> "$LOG_FILE"

# Run scraper with config optimized for overnight:
# - 100 shows max per run
# - 3s delay (safe for IA's rate limits)
# - Year-based iteration (resumes from state file)
cd "$PROJECT_DIR" && python3 "$SCRAPER" --target 100 --delay 3 2>&1 | tee -a "$LOG_FILE"

echo "$(date): Overnight run complete." >> "$LOG_FILE"

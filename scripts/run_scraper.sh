#!/bin/bash
# run_scraper.sh — GD Comment Scraper 24/7 wrapper
# Runs the 1965-1995 year-by-year sweep with 3.25s delays between requests.
# Runs continuously — no time window restriction. Resumes from saved state.
#
# Usage: ./run_scraper.sh [--target N] [--delay S]
#
# Env:
#   GD_SCRAPER_STATE_FILE  (default: data/scraper_state.json)
#
# After completion, a summary is written to output/scraping_summary.md
# (Discord delivery is handled by the cron auto-deliver platform config).

set -euo pipefail

PROJECT_DIR="/home/mao/DaveMatt/gd-project"
SCRAPER="$PROJECT_DIR/scrapers/gd_comment_scraper.py"
LOG_FILE="$PROJECT_DIR/scripts/247_scraper.log"
SUMMARY_FILE="$PROJECT_DIR/output/scraping_summary.md"
STATE_FILE="$PROJECT_DIR/data/scraper_state.json"

# Defaults: 3.25s delay (user preference), target sized for 3-hour cron window
# The scraper auto-resumes from the state file and iterates 1965-1995 year-by-year.
DELAY="${GD_SCRAPER_DELAY:-3.25}"
TARGET="${GD_SCRAPER_TARGET:-3000}"

# Allow CLI overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --delay)  DELAY="$2";  shift 2 ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

echo "========================================" >> "$LOG_FILE"
echo "$(date): Starting GD scraper (24/7 mode)" >> "$LOG_FILE"
echo "  Target: $TARGET shows | Delay: ${DELAY}s" >> "$LOG_FILE"
echo "  Scraper: $SCRAPER" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

cd "$PROJECT_DIR"

START_TIME=$SECONDS
# Run the scraper; it logs to its own gd_scraper.log as well
python3 "$SCRAPER" --target "$TARGET" --delay "$DELAY" 2>&1 | tee -a "$LOG_FILE"
SCRAPER_EXIT=${PIPESTATUS[0]}

ELAPSED_MIN=$(( (SECONDS - START_TIME) / 60 ))
FINISH_HOUR=$(date +%H)

# --- Write summary ---
mkdir -p "$PROJECT_DIR/output"

# Extract key stats from the combined data file
python3 - "$STATE_FILE" "$SUMMARY_FILE" "$SCRAPER_EXIT" "$ELAPSED_MIN" "$FINISH_HOUR" <<'PYEOF'
import json, sys
from datetime import datetime

state_file, summary_file, exit_code, elapsed_min, finish_hour = sys.argv[1:6]

with open(state_file) as f:
    state = json.load(f)

with open(state_file.replace("scraper_state.json", "gd_comments_combined.json")) as f:
    combined = json.load(f)

meta = combined.get("metadata", {})

summary = f"""# GD Scraper Run — Summary

**Run finished:** {datetime.now().isoformat()}
**Duration:** {elapsed_min} min
**Exit code:** {exit_code}
**Finish time:** {finish_hour}:00 UTC

## Progress
| Metric              | Value   |
|---------------------|---------|
| Total shows attempted | {meta.get('shows_attempted', len(state['processed_ids']))} |
| Shows with comments   | {state['shows_with_comments']} |
| Total comments        | {state['total_comments']} |
| Total setlists        | {state['total_setlists']} |
| Next year to scrape   | {state['current_year']} |

## State
- State file: `{state_file}`
- Combined data: `data/gd_comments_combined.json` ({combined.get('metadata', {}).get('comments_total', state['total_comments'])} comments)
- Last save: {state.get('last_save', 'never')}

## Notes
- Scraper resumed from last saved state (year {state['current_year'] - 1 if state['current_year'] > 1965 else 1965} → {state['current_year']})
- Year-by-year sweep: 1965→1995 (scraper loop exits at year 1995)
- Delay: 3.25s per request (user preference)
- Mode: 24/7 — runs every hour via cron, auto-resumes from saved state
- Full log: scripts/247_scraper.log
"""
with open(summary_file, "w") as f:
    f.write(summary)
print(f"Summary written to {summary_file}")
PYEOF

echo "$(date): Scrape run complete. Exit: $SCRAPER_EXIT | Elapsed: ${ELAPSED_MIN} min" >> "$LOG_FILE"
echo "$(date): Summary: $SUMMARY_FILE" >> "$LOG_FILE"

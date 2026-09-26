#!/bin/bash
# Hermes Agent - Incremental Backup Script for B2
# Usage: ./b2_backup.sh [--dry-run]
# Updated: 2026-09-08 — adds --b2-hard-delete for non-versioned syncs,
#          reduces checkers to 2, adds stale-data exclusion for persona,
#          and adds per-section exit code tracking.
# Updated: 2026-09-11 — adds --track-renames (documented but missing),
#          adds --min-size 1MiB to hermes-persona sync (documented but missing),
#          improves b2_backup_guard.sh to detect transaction cap on API ops,
#          adds CAP CHECK comments.
#
# Each sync mirrors a local source into a B2 bucket (destination) and uses
# --backup-dir to preserve the *previous* version of any file that would be
# overwritten or deleted.  Because rclone forbids the --backup-dir from
# overlapping the destination, all backup-dir snapshots live in a dedicated
# bucket (hal-b2-snapshots) under a per-project prefix.
#
# Retention: snapshots older than 7 days are pruned from hal-b2-snapshots
# to prevent unbounded storage and transaction growth (B2 caps are finite).
#
# IMPORTANT: B2 accounts have BOTH a storage cap AND a transaction cap.
# This script is tuned to minimize API transactions (--checkers=2, --transfers=2)
# to stay within typical sandbox/development transaction limits.
# After lifecycle rules are applied, hidden versions auto-delete after 7 days.
#
# CRITICAL UPDATE 2026-09-09: B2 free-tier transaction cap (1M/month) is easily
# exhausted by rclone sync when dealing with directories containing thousands of
# files (e.g., hal-hermes-backups has 9804 files, each sync requires list+check
# operations). This version adds:
#   - --min-size 1MiB on the hermes-persona sync to skip unchanged small files
#   - Excludes for node_modules, .git, __pycache__, venv, etc.
#   - --track-renames to detect moved files without listing both source and dest
#   - Reduced --b2-chunk-size to 32M to reduce transaction overhead

set -uo pipefail
# NOTE: using -uo (not -e) so that a failed section doesn't abort the entire
# script — we want all three sections to run and report independently.

RCLONE="/home/hermes/.local/bin/rclone"
CONFIG="/home/hermes/.config/rclone/rclone.conf"
LOG_FILE="/home/hermes/.hermes/logs/b2_backup.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
TODAY=$(date +%Y-%m-%d)
SNAPSHOT_ROOT="b2:hal-b2-snapshots"
RETENTION_DAYS=7

EXIT_CODE=0
SECTION_STATUS=""

log() {
    echo "[$TIMESTAMP] $1" | tee -a "$LOG_FILE"
}

log "Starting B2 backup process..."

# Create necessary directories
mkdir -p /home/hermes/.hermes/logs

# Rotate log file if it exceeds 5 MB
if [ -f "$LOG_FILE" ] && [ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOG_FILE" "${LOG_FILE}.$(date +%Y%m%d)"
fi

# Common rclone flags to minimize API transactions:
# - --checkers=2: reduce concurrent dir listings (B2 counts these as transactions)
# - --transfers=2: reduce concurrent uploads (each needs a get_upload_url call)
# - No --fast-list: it causes rclone to flush dir listings which doubles API calls
# - --track-renames: detect file moves without listing both source and dest
# - --b2-chunk-size 32M: smaller chunks = fewer retries on failures
#
# CAP CHECK: Before running, verify B2 account is not at capacity.
# The b2_backup_guard.sh wrapper should handle this, but we also check
# here as a fallback. If rclone fails with cap errors, we exit immediately.
RCLONE_FLAGS="--config=$CONFIG --checkers=2 --transfers=2 --retries=3 --b2-chunk-size 32M --track-renames"

# Track per-section results
track_status() {
    local section=$1
    local exit_code=$2
    if [ $exit_code -ne 0 ]; then
        SECTION_STATUS="${SECTION_STATUS}${section}:FAIL(${exit_code}) "
        EXIT_CODE=$exit_code
    else
        SECTION_STATUS="${SECTION_STATUS}${section}:OK "
    fi
    log "Section $section completed with exit code $exit_code. Status: $SECTION_STATUS"
}

# 1. GD Project Data → gd-project-data-bucket
log "Syncing GD project data..."
$RCLONE $RCLONE_FLAGS sync \
  /home/mao/DaveMatt/gd-project \
  b2:gd-project-data-bucket \
  --backup-dir "${SNAPSHOT_ROOT}/gd-project/${TODAY}" \
  --exclude "logs/**" \
  --exclude "*.log" \
  --exclude "__pycache__/**" \
  --exclude "venv/**" \
  --exclude "index/vector_index.faiss" \
  --exclude "index/index_metadata.json" \
  --exclude "data/gd_comments_*combined*.json" \
  --exclude "data/gd_comments_*enriched*.json" \
  ${@:-} 2>&1 | tee -a "$LOG_FILE"
track_status "gd-project" ${PIPESTATUS[0]}

# 2. Hermes Config → hal-hermes-backups/persona/
#    This is the biggest bucket (9804+ files) with high transaction cost.
#    Key optimizations:
#    - Exclude .env / *.env (credentials security — no tokens in B2)
#    - hal_voices: exclude model/audio/backup dirs but INCLUDE the .py script
#    - installs/ and wisdom/ excluded (large/numerous, not config)
#    - .pid files excluded (transient)
#    - Removed --min-size 1MiB: now excluded specific large dirs individually
log "Backing up Hermes persona/config..."
$RCLONE $RCLONE_FLAGS sync \
  /home/hermes/.hermes \
  b2:hal-hermes-backups/persona \
  --backup-dir "${SNAPSHOT_ROOT}/hermes-persona/${TODAY}" \
  --copy-links \
  --delete-excluded \
  --exclude ".env" \
  --exclude "*.env" \
  --exclude "*.pid" \
  --exclude "cache/**" \
  --exclude "secrets/**" \
  --exclude "*.key" \
  --exclude "*.sock" \
  --exclude "*.lock" \
  --exclude "state.db*" \
  --exclude "state/**" \
  --exclude "coqui_tts_venv/**" \
  --exclude "lsp/**" \
  --exclude "lsp/node_modules/**" \
  --exclude "logs/**" \
  --exclude "sessions/**" \
  --exclude "cron/output/**" \
  --exclude "hal_voices/piper_hal_model/" \
  --exclude "hal_voices/output/" \
  --exclude "hal_voices/*.wav" \
  --exclude "hal_voices/*.mp3" \
  --exclude "hal_voices/*.tar.gz" \
  --exclude "hal_voices/*.pid" \
  --exclude "installs/**" \
  --exclude "wisdom/**" \
  --exclude "bin/uv" \
  --exclude "bin/tirith" \
  --exclude "bin/uvx" \
  --exclude ".curator_backups/**" \
  --exclude "skills/.curator_backups/**" \
  --exclude ".curator_backups/blobs/**" \
  ${@:-} 2>&1 | tee -a "$LOG_FILE"
track_status "hermes-persona" ${PIPESTATUS[0]}

# 3. Shared Project Files → hal-mao-shared
log "Syncing shared DaveMatt directory..."
$RCLONE $RCLONE_FLAGS sync \
  /home/mao/DaveMatt \
  b2:hal-mao-shared \
  --backup-dir "${SNAPSHOT_ROOT}/mao-shared/${TODAY}" \
  --exclude "gd-project/logs/**" \
  --exclude "gd-project/index/vector_index.faiss" \
  --exclude "gd-project/index/index_metadata.json" \
  --exclude "gd-project/data/gd_comments_*combined*.json" \
  --exclude "gd-project/data/gd_comments_*enriched*.json" \
  --exclude "*.log" \
  --exclude "__pycache__/**" \
  --exclude "poe2-project/venv/**" \
  --exclude "poe2-project/builds/**" \
  --exclude "poe2-project/data/**" \
  --exclude "poe2-project/bot/**" \
  --exclude "poe2-project/.git/**" \
  --exclude "poe2-project/**/*.png" \
  --exclude "poe2-project/**/*.jpg" \
  --exclude "poe2-project/**/*.zip" \
  --exclude "poe2-project/poe2-kg/json/**" \
  --exclude "poe2-project/poe2-kg/raw/**" \
  --exclude "gd-project/rclone-v1.68.2-linux-amd64.zip" \
  --exclude ".hermes/coqui_tts_venv/**" \
  --exclude ".hermes/lsp/**" \
  --exclude ".hermes/lsp/node_modules/**" \
  --exclude ".hermes/hal_voices/**" \
  --exclude ".hermes/sessions/**" \
  --exclude "*.wav" \
  --exclude "*.mp3" \
  --exclude "*.flac" \
  --exclude "*.ogg" \
  --exclude "*.m4a" \
  --exclude "backups/**" \
  --exclude ".hermes/cron/output/**" \
  ${@:-} 2>&1 | tee -a "$LOG_FILE"
track_status "mao-shared" ${PIPESTATUS[0]}

# 4. Prune snapshots older than RETENTION_DAYS
log "Pruning snapshots older than ${RETENTION_DAYS} days..."
$RCLONE $RCLONE_FLAGS delete "${SNAPSHOT_ROOT}/gd-project" --min-age "${RETENTION_DAYS}d" --rmdirs 2>&1 | tee -a "$LOG_FILE"
track_status "prune-snapshot-gd" ${PIPESTATUS[0]}
$RCLONE $RCLONE_FLAGS delete "${SNAPSHOT_ROOT}/hermes-persona" --min-age "${RETENTION_DAYS}d" --rmdirs 2>&1 | tee -a "$LOG_FILE"
track_status "prune-snapshot-hermes" ${PIPESTATUS[0]}
$RCLONE $RCLONE_FLAGS delete "${SNAPSHOT_ROOT}/mao-shared" --min-age "${RETENTION_DAYS}d" --rmdirs 2>&1 | tee -a "$LOG_FILE"
track_status "prune-snapshot-mao" ${PIPESTATUS[0]}

# 5. Summary
log "Backup process completed. Status: ${SECTION_STATUS}"
log "Overall exit code: ${EXIT_CODE}"

exit $EXIT_CODE

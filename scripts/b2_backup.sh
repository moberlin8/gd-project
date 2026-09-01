#!/bin/bash
# Hermes Agent - Incremental Backup Script for B2
# Usage: ./b2_backup.sh [--dry-run]
#
# Each sync mirrors a local source into a B2 bucket (destination) and uses
# --backup-dir to preserve the *previous* version of any file that would be
# overwritten or deleted.  Because rclone forbids the --backup-dir from
# overlapping the destination, all backup-dir snapshots live in a dedicated
# bucket (hal-b2-snapshots) under a per-project prefix.  No new buckets are
# created at runtime; hal-b2-snapshots is a prerequisite (see setup).

set -euo pipefail

RCLONE="/home/hermes/.local/bin/rclone"
CONFIG="/home/hermes/.config/rclone/rclone.conf"
LOG_FILE="/home/hermes/.hermes/logs/b2_backup.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
TODAY=$(date +%Y-%m-%d)
SNAPSHOT_ROOT="b2:hal-b2-snapshots"

log() {
    echo "[$TIMESTAMP] $1" | tee -a "$LOG_FILE"
}

log "Starting B2 backup process..."

# Create necessary directories
mkdir -p /home/hermes/.hermes/logs

# 1. GD Project Data → gd-project-data-bucket
#    Destination: bucket root (live mirror)
#    Backup-dir: hal-b2-snapshots/gd-project/<date> (separate bucket, no overlap)
log "Syncing GD project data..."
$RCLONE --config="$CONFIG" sync \
  /home/mao/DaveMatt/gd-project \
  b2:gd-project-data-bucket \
  --backup-dir "${SNAPSHOT_ROOT}/gd-project/${TODAY}" \
  --transfers=4 \
  --checkers=8 \
  --exclude "logs/**" \
  --exclude "*.log" \
  --exclude "__pycache__/**" \
  --exclude "venv/**" \
  ${@:-} 2>&1 | tee -a "$LOG_FILE" || log "GD project sync failed"

# 2. Hermes Config → hal-hermes-backups/persona/
#    Destination: hal-hermes-backups/persona (live mirror)
#    Backup-dir: hal-b2-snapshots/hermes-persona/<date> (separate bucket, no overlap)
log "Backing up Hermes persona/config..."
$RCLONE --config="$CONFIG" sync \
  /home/hermes/.hermes \
  b2:hal-hermes-backups/persona \
  --backup-dir "${SNAPSHOT_ROOT}/hermes-persona/${TODAY}" \
  --transfers=2 \
  --checkers=4 \
  --exclude "secrets/**" \
  --exclude "*.key" \
  ${@:-} 2>&1 | tee -a "$LOG_FILE" || log "Persona backup failed"

# 3. Shared Project Files → hal-mao-shared
#    Destination: bucket root (live mirror)
#    Backup-dir: hal-b2-snapshots/mao-shared/<date> (separate bucket, no overlap)
log "Syncing shared DaveMatt directory..."
$RCLONE --config="$CONFIG" sync \
  /home/mao/DaveMatt \
  b2:hal-mao-shared \
  --backup-dir "${SNAPSHOT_ROOT}/mao-shared/${TODAY}" \
  --transfers=4 \
  --checkers=8 \
  --exclude "gd-project/logs/**" \
  --exclude "*.log" \
  --exclude "__pycache__/**" \
  ${@:-} 2>&1 | tee -a "$LOG_FILE" || log "Shared directory sync failed"

log "Backup process completed."

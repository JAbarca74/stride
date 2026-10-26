#!/usr/bin/env bash
# Runs the STRIDE daily job once for each paper instance (B, then DIP).
# Called every 15 minutes by cron:
#   */15 * * * * /path/to/stride/scripts/run_stride.sh
#
# Each run is `python -m stride.run_daily --phase auto`:
# the job itself decides which phase is due in New York time and does nothing
# outside the windows, on NYSE holidays, or when the phase already ran today.
# After both runs it makes the daily database backup (scripts/backup_db.sh).
# All output is appended to data/logs/scheduler.log.

set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/scheduler.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"
}

VENV="$ROOT/.venv/bin/activate"
if [ ! -f "$VENV" ]; then
    log "error: no virtual environment at .venv (run: python3 -m venv .venv)"
    exit 1
fi
# shellcheck disable=SC1090
source "$VENV"

FAILED=0
for CONFIG in "config.paper_b.yaml" "config.paper_dip.yaml"; do
    log "start $CONFIG"
    if python -m stride.run_daily --config "$CONFIG" --phase auto >> "$LOG" 2>&1; then
        log "end $CONFIG exit 0"
    else
        CODE=$?
        log "end $CONFIG exit $CODE"
        FAILED=1
    fi
done

if [ -f "$ROOT/scripts/backup_db.sh" ]; then
    bash "$ROOT/scripts/backup_db.sh" >> "$LOG" 2>&1
fi

exit $FAILED

#!/usr/bin/env bash
# Daily copy of the STRIDE databases (data/*.db), keeping the last 30 days.
# Called by scripts/run_stride.sh after both paper instances finish.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/data"
DEST="${1:-$HOME/stride_backups}"
KEEP_DAYS=30
TODAY="$(date '+%Y-%m-%d')"
TARGET="$DEST/$TODAY"

if [ ! -d "$TARGET" ]; then
    shopt -s nullglob
    dbs=("$DATA"/*.db)
    if [ ${#dbs[@]} -gt 0 ]; then
        mkdir -p "$TARGET"
        cp "$DATA"/*.db "$TARGET/"
        echo "backup: ${#dbs[@]} databases copied to $TARGET"
    fi
    shopt -u nullglob
fi

if [ -d "$DEST" ]; then
    # Remove backups older than KEEP_DAYS days (folders named YYYY-MM-DD)
    find "$DEST" -mindepth 1 -maxdepth 1 -type d -name "20[0-9][0-9]-[0-1][0-9]-[0-3][0-9]" -mtime +"$KEEP_DAYS" -exec rm -rf {} +
fi

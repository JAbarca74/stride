#!/usr/bin/env bash
# Registers the cron job on Linux that runs STRIDE paper trading every 15 minutes.
# Usage:
#   bash scripts/install_cron.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/run_stride.sh"
chmod +x "$SCRIPT" "$ROOT/scripts/backup_db.sh"

CRON_LINE="*/15 * * * * $SCRIPT"

# Check if the cron job already exists in user's crontab
CURRENT="$(crontab -l 2>/dev/null || true)"
if echo "$CURRENT" | grep -Fq "$SCRIPT"; then
    echo "STRIDE cron job is already installed:"
    echo "$CURRENT" | grep -F "$SCRIPT"
else
    (echo "$CURRENT"; echo "$CRON_LINE") | crontab -
    echo "Installed STRIDE cron job (every 15 minutes):"
    echo "  $CRON_LINE"
fi

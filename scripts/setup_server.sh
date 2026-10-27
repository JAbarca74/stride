#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 server (Python 3.12) for the STRIDE paper run.
# Usage, from the repository root on the server:
#   bash scripts/setup_server.sh
#
# Before the first real run, copy these two things from the old machine:
#   .env        (paper keys and data key only, never live keys)
#   data/*.db   (so the 40-day count continues instead of restarting)
# Then stop the old scheduler (Windows: Unregister-ScheduledTask "STRIDE paper"),
# otherwise two machines would send orders to the same paper account.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v python3.12 >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3.12 python3.12-venv git
fi

if [ ! -d .venv ]; then
    python3.12 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e .

mkdir -p data/logs

if [ ! -f .env ]; then
    echo "missing .env: copy it from the old machine, then re-run this script" >&2
    exit 1
fi
chmod 600 .env

bash "$ROOT/scripts/install_cron.sh"

echo "check:  python -m stride.run_daily --config config.paper_b.yaml --phase auto"
echo "logs:   tail -f data/logs/scheduler.log"
echo "verdict (after 40 trading days): python -m stride.reporting.daily --verdict --config config.paper_b.yaml"

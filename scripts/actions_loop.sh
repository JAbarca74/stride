#!/usr/bin/env bash
# The paper run on GitHub Actions: ticks every 5 minutes for DURATION seconds.
# One tick = `run_daily --phase auto` for both instances (the job itself decides,
# in New York time, which phase is due), then the databases go to the state repo.
# Needs: .venv or an active Python with stride installed, and STATE_DIR (a clone
# of the private state repo). Used by .github/workflows/paper.yml.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DURATION="${DURATION:-18600}"
END=$(( $(date +%s) + DURATION ))
FAILED=0

while [ "$(date +%s)" -lt "$END" ]; do
    for CONFIG in "config.paper_b.yaml" "config.paper_dip.yaml"; do
        if ! python -m stride.run_daily --config "$CONFIG" --phase auto; then
            echo "tick failed: $CONFIG"
            FAILED=1
        fi
    done
    bash "$ROOT/scripts/save_state.sh" || FAILED=1
    # next 5-minute boundary plus 20 seconds, so a tick never lands before a window opens
    sleep $(( 300 - $(date +%s) % 300 + 20 ))
done

exit $FAILED

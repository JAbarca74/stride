#!/usr/bin/env bash
# The paper run on GitHub Actions: ticks every 5 minutes for DURATION seconds.
# One tick = `run_daily --phase auto` for both instances (the job itself decides,
# in New York time, which phase is due), then `run_mc` for the Monte Carlo portfolio
# (its own hourly job) and `run_risk` for the risk-allocation portfolio (its own daily
# job), then the databases go to the state repo.
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
    # The Monte Carlo paper portfolio (docs/design/mc_forecast.md): a no-op while its config
    # says `enabled: false`. It never marks the run as failed: it alerts by itself, and B
    # and DIP must not depend on an experiment.
    python -m stride.run_mc --config config.paper_mc.yaml || echo "tick failed: config.paper_mc.yaml"
    # The risk-allocation paper portfolio (docs/design/mc_risk_paper.md): the same rules. A
    # no-op while its config says `enabled: false`, and it never marks the run as failed.
    python -m stride.run_risk --config config.paper_risk.yaml || echo "tick failed: config.paper_risk.yaml"
    bash "$ROOT/scripts/save_state.sh" || FAILED=1
    # next 5-minute boundary plus 20 seconds, so a tick never lands before a window opens
    sleep $(( 300 - $(date +%s) % 300 + 20 ))
done

exit $FAILED

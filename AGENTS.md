# AGENTS.md — Instructions for coding agents (Antigravity and others)

Read `CLAUDE.md` and `docs/SPEC.md` first. The same rules apply to every agent working on this repository.

## What you may do

- Pick **one** task from the backlog table in `docs/SPEC.md` section 13 that is assigned to the current phase and not reserved for humans.
- Work on a branch named `task/<ID>-<short-name>` (e.g., `task/T05-price-downloader`).
- Write the code and its tests, then open a pull request titled `<ID>: <task>`.

## What you must not do

- Do not create or edit: `src/stride/risk/leverage.py`, `src/stride/risk/guard.py`, the kill switch, `src/stride/execution/orders.py`, `src/stride/execution/ibkr_broker.py`.
- Do not change values in `config.yaml` or add parameters unless the task says so.
- Do not set `LIVE_CONFIRM` or run with `mode: live`.
- Credentials: data-only API keys (Tiingo) and **paper-trading** keys (Alpaca paper) in `.env` may be used for development and verification. Live-trading credentials are never allowed. Never print, log or commit any key, and confirm `.env` is git-ignored first.
- Do not add dependencies that are not listed in SPEC section 4 without explaining why in the PR.

## Definition of done

- The task's acceptance criterion in SPEC section 13 is met and covered by a test.
- `pytest`, `ruff check .` and `ruff format --check .` pass.
- The PR description lists what changed, how it was tested, and any open question about the spec.

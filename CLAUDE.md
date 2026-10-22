# CLAUDE.md — Working on STRIDE

STRIDE (*Systematic Trend & Risk-Integrated Daily Execution*) is a daily trend-following trading system. The full specification is in `docs/SPEC.md`. Read it before writing code; it is the single source of truth.

## Language

All code, comments, commit messages, docs and pull requests are in **English**.

## Golden rules

1. **The spec wins.** If code and `docs/SPEC.md` disagree, follow the spec. If the spec is ambiguous or seems wrong, stop and ask; never guess.
2. **Pure core.** `src/stride/strategy/` and `src/stride/risk/` are pure functions: no network, no broker, no file I/O, no clock. Inputs in, target positions out.
3. **No look-ahead.** No function may read data dated after the evaluation date. Rolling highs/lows exclude day t (see SPEC section 6).
4. **Tests first-class.** Every new function ships with pytest tests. `pytest` and `ruff check` must pass before a task is done.
5. **Parameters live in `config.yaml`.** Never hardcode strategy or risk numbers. Never change their values unless explicitly asked.
6. **Never touch real money.** Never run with `mode: live` and never set `LIVE_CONFIRM`. Use `mode: backtest` or `mode: paper` only.
   - **Allowed** in `.env` for development and verification: data-only API keys (e.g. Tiingo `DATA_API_KEY`) and **paper-trading** keys (e.g. Alpaca paper).
   - **Never allowed:** live-trading credentials of any kind.
   - Never print, log or commit any key. Check that `.env` is git-ignored before using it, and read keys only through the code (never echo `.env`).
7. **Human-reviewed modules.** Do not create or change `risk/leverage.py`, `risk/guard.py`, the kill switch, `execution/orders.py` or `execution/ibkr_broker.py` without the owner's explicit go-ahead in the current session. Propose a design first.
8. **Small steps.** One backlog task (SPEC section 13) per branch and pull request, with the task ID in the title.

## Commands

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
mypy src
```

## Conventions

- Python 3.12+, type hints everywhere, `from __future__ import annotations`.
- pandas DataFrames indexed by `date` (timezone-naive, NYSE trading days).
- Money in USD as `float`; quantities may be fractional.
- Rule names in logs and signals use the exact constants from SPEC section 6 (`TREND_FILTER`, `STOP_2N`, `EXIT_LL20`, `ENTRY_HH55`, `PYRAMID`, `HOLD`).
- Database: SQLite at `data/stride.db` (git-ignored). Schema per SPEC sections 5 and 10.

## Current phase

Phase 1 — Foundations (SPEC section 12). Backlog tasks T01–T07, T19, T21.

# STRIDE

**Systematic Trend & Risk-Integrated Daily Execution**

A systematic trend-following trading system with volatility-based risk management, built as a 12-month live experiment and technical portfolio project.

STRIDE evaluates a diversified ETF universe (US and international equities, bonds, gold, silver, commodities, the US dollar, bitcoin and ether) once per trading day. It applies rules drawn from long-lived systematic traders — 55/20-day breakouts and pyramiding (the Turtles), a 200-day trend filter (Paul Tudor Jones), risk-based allocation (Ray Dalio), volatility stops (Ed Seykota) and fixed fractional risk (Larry Hite) — and only trades when a signal changes. A dedicated risk manager sizes every position by volatility, computes leverage as the minimum of five limits (up to full Kelly), and can veto any order or stop the system.

## Status

Phase 1 — Foundations. See [`docs/SPEC.md`](docs/SPEC.md) for the full specification, phases and backlog.

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # fill in paper-trading keys only
pytest
```

## Repository guide

| Path | Contents |
| --- | --- |
| `docs/SPEC.md` | Technical specification (source of truth) |
| `config.yaml` | Strategy and risk parameters |
| `src/stride/` | System code |
| `tests/` | Test suite |
| `CLAUDE.md`, `AGENTS.md` | Rules for coding agents |

## Disclaimer

This is an educational research project. Nothing here is financial advice or a promise of returns. Trading involves risk of loss.

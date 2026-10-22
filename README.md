<p align="center">
  <img src="docs/assets/hero.svg" alt="STRIDE: Systematic Trend &amp; Risk-Integrated Daily Execution" width="100%">
</p>

<p align="center">
  <a href="https://github.com/JAbarca74/stride/actions/workflows/ci.yml"><img src="https://github.com/JAbarca74/stride/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/JAbarca74/stride/actions/workflows/security.yml"><img src="https://github.com/JAbarca74/stride/actions/workflows/security.yml/badge.svg" alt="Security"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.12+">
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/lint-ruff-D7FF64?logo=ruff&amp;logoColor=black" alt="Ruff"></a>
  <img src="https://img.shields.io/badge/types-mypy%20strict-2A6DB2" alt="mypy strict">
  <img src="https://img.shields.io/badge/tests-pytest%20%2B%20hypothesis-0A9EDC?logo=pytest&amp;logoColor=white" alt="pytest and Hypothesis">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
</p>

<p align="center">
  A systematic trend-following trading system with volatility-based risk management,<br>
  built as a 12-month live experiment and an open technical portfolio project.
</p>

---

## What it does

STRIDE evaluates a diversified ETF universe (US and international equities, bonds, gold, silver, commodities, the US dollar, bitcoin and ether) **once per trading day** and only trades when a signal changes. Its rules come from long-lived systematic traders:

| Rule | Source | In STRIDE |
| --- | --- | --- |
| 55/20-day breakouts and pyramiding | The Turtles | `ENTRY_HH55`, `EXIT_LL20`, `PYRAMID` |
| 200-day trend filter | Paul Tudor Jones | `TREND_FILTER` |
| Risk-based allocation | Ray Dalio | 1% of equity per 1N of volatility |
| Volatility stops | Ed Seykota | `STOP_2N` |
| Fixed fractional risk | Larry Hite | position size from risk, not conviction |

A dedicated risk manager sizes every position, computes leverage, and can **veto any order or stop the system**.

## How a trading day flows

<p align="center">
  <img src="docs/assets/pipeline.svg" alt="Daily flow: data, strategy, risk, execution, log and reporting. Every order passes the risk gate, which approves or vetoes it." width="100%">
</p>

Data flows one way. Strategy and risk are **pure functions** (no network, no file I/O, no clock), which makes them easy to test and impossible to leak future data into. Only the execution layer talks to a broker, and switching between backtest, paper and live is a single `mode` setting.

## Leverage, with brakes

<p align="center">
  <img src="docs/assets/leverage.svg" alt="Effective leverage is the minimum of five limits: Kelly, volatility target, absolute cap, broker limit and capital limit." width="100%">
</p>

Leverage is recomputed every day as the minimum of five limits, up to full Kelly and never above 4x. Below $2,000 there is no margin: extra exposure can only come from 2x/3x ETFs, so the maximum loss is the amount deposited. A drawdown past −20% halves leverage, and −40% trips a kill switch that closes every position and stops the system until a human resets it.

## Safety by design

- **No real money by accident.** `mode: live` only takes effect with `LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY`; otherwise STRIDE starts in paper mode.
- **Paper keys only in development.** Alpaca keys that are not paper keys are refused.
- **Secrets never leave `.env`.** Keys are read as `SecretStr`, never printed, logged or committed. CI scans every push for leaked secrets.
- **No look-ahead.** Rolling highs and lows exclude the current day, and tests check that no function reads data dated after the evaluation date.
- **Invariant tests for code that moves money.** Leverage, the risk veto and the kill switch are covered by property-based tests with Hypothesis.

## Status

**Phase 1: Foundations.** Data loading, indicators, strategy rules, sizing, the backtester, reports and the risk modules are in review. See [`docs/SPEC.md`](docs/SPEC.md) for the full specification, phases and backlog.

## Quick start

```bash
git clone https://github.com/JAbarca74/stride.git
cd stride
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest                    # no network and no keys needed
```

To download data, copy `.env.example` to `.env` and add a data-only key (Tiingo) and, optionally, **paper-trading** keys. Never put live-trading credentials in `.env`.

```bash
cp .env.example .env
```

## Loading data (Phase 1)

Keys are read from `.env` and never printed. Downloads are incremental and cached in `data/stride.db`.

```bash
python -m stride.data.update prices        # download or extend every series
python -m stride.data.update validate      # SPEC section 5 validations
python -m stride.data.update check-alpaca  # Alpaca free-plan closes vs Tiingo
python -m stride.data.update t21           # synthetic vs real leveraged ETFs
```

## Repository guide

| Path | Contents |
| --- | --- |
| `docs/SPEC.md` | Technical specification (source of truth) |
| `config.yaml` | Strategy and risk parameters |
| `src/stride/` | System code: `data`, `strategy`, `risk`, `execution`, `backtest`, `reporting` |
| `tests/` | Test suite |
| `CLAUDE.md`, `AGENTS.md` | Rules for coding agents |

## Contributing

Issues and focused pull requests are welcome. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first; the risk, kill switch and broker modules are reviewed line by line. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability or something that looks like a leaked key? **Do not open a public issue.** Report it privately as described in [`SECURITY.md`](SECURITY.md).

## License

[MIT](LICENSE) © 2026 Jefferson Abarca

## Disclaimer

This is an educational research project. Nothing here is financial advice, a trading signal or a promise of returns. Trading involves risk of loss, and leverage increases it. Use the code at your own risk.

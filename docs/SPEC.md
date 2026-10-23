# STRIDE — Technical Specification

**STRIDE** — *Systematic Trend & Risk-Integrated Daily Execution*
A systematic trend-following trading system with volatility-based risk management.

- Version: 1.4
- Date: 2026-10-19
- Author: Jefferson Abarca
- Status: Approved for Phase 1 (Foundations)

This document is the single source of truth for the system. If code and spec disagree, the spec wins until it is explicitly changed. Any ambiguity must be raised as a question, never resolved by guessing.

**Changes in 1.4** (owner decisions on the risk-module review): leverage capacity = L_borrow × `max_etf_portfolio_leverage` with a new `allow_margin` parameter (section 7, step 2); a same-day instrument switch is allowed under three conditions (section 7, step 3); a reconciliation break now **halts** the system instead of liquidating it, and only the −40% drawdown liquidates (section 7, step 4).

**Changes in 1.3** (owner decisions on the risk modules): forward-looking `sigma_60` from target weights and an EWMA covariance matrix; the Kelly fallback replaces only the Kelly term, so L may go below 1; the drawdown is measured on a time-weighted index whose peak is stored in the database; reconciliation per symbol with a $1 floor; −40% is a trigger, not a guaranteed maximum loss (section 7).

**Changes in 1.2** (owner decisions on data and leverage): credentials rule for development (section 11); missing bar for day t handled per symbol (section 5); download etiquette: incremental downloads, local cache, backoff on HTTP 429 (section 5); Stooq replaces Alpaca as second source if Alpaca's free closes do not match the official close (section 5); issuer facts in `reference/etf_facts.yaml` and per-ETF expense ratios (sections 7 and 9); ETHU added as 2x candidate for ETHA (section 7); hysteresis on instrument switching (section 7, step 2b) and switch reporting in the backtest (section 9).

**Changes in 1.1** (owner decisions from the Phase 1 review): data validations 1, 3 and 4 clarified and their thresholds moved to `config.yaml` (section 5); daily run moved to 18:00 New York time (section 8); crypto spot history built from 16:00 ET session bars and ratio-spliced onto the ETFs (section 5); data sources fixed (section 5); leveraged ETF filters and synthetic-series formula with FRED DTB3 (sections 7 and 9); CI on GitHub Actions (section 15).

---

## 1. Summary and objective

STRIDE evaluates a portfolio of ETFs once per trading day, using rules taken from sustainable, long-lived traders (the Turtles, Paul Tudor Jones, Ray Dalio, Ed Seykota, Larry Hite). It sends real orders to a broker via API **only when a signal changes**. It is a 12-month live experiment, started with **$100 plus $20 monthly deposits**, built as a technical portfolio project.

**Hypotheses**

- **H1 (primary):** over the 12 live months, STRIDE achieves a net-of-cost Sharpe ratio higher than buy-and-hold SPY and higher than a 60/40 SPY/IEF portfolio.
- **H2 (no overfitting):** live performance stays within ±2 standard deviations of what the backtest predicts for the same period.
- **H3 (leverage):** a parallel paper account of $10,000 running the same signals with full Kelly leverage achieves higher compound growth than the real account without triggering the kill switch. If H3 holds, full Kelly may move to real money in a later stage.

**Out of scope:** guaranteeing profits; offering signals or advice to third parties; intraday or high-frequency trading; any buy/sell decision made by a language model.

---

## 2. Scope, assumptions and constraints

| Parameter | Value | Note |
| --- | --- | --- |
| Starting capital | $100 USD + $20/month | Deposits reinvested by the normal rules; $340 deposited in year 1 |
| Live duration | 12 months | After backtest and paper trading |
| Universe | SPY, EFA, IEF, TLT, GLD, SLV, DBC, UUP, IBIT, ETHA | US and international equities, mid and long bonds, gold, silver, commodities, US dollar, bitcoin, ether |
| Frequency | Daily evaluation after close; execution only on signal | Never buy and sell the same asset on the same day |
| Live broker | Interactive Brokers (IBKR Pro, Fixed pricing) | Accepts Costa Rica residents, no minimum deposit, fractional shares, API |
| Paper broker | Alpaca paper or IBKR paper | Same code, `mode: paper` |
| Data | Free sources only | See section 5 |
| Language | Python 3.12+ | Optional C++ core in a later phase |
| Target leverage | 4.0x (full Kelly) | `kelly_fraction: 1.0`, `max_leverage: 4.0` |
| Effective leverage | min of 5 limits | Recomputed daily, see section 7 |
| Leveraged ETFs | 2x/3x versions when target exposure exceeds equity | No margin, no loan: max loss = amount deposited |
| Parallel paper account | $10,000 simulated, margin enabled, full Kelly | Same signals as the real account (H3) |
| Kill switch | −40% drawdown from peak | Closes all positions and stops the system |

**Leverage constraints the code must respect**

- Below $2,000 of equity there is no margin account. Leverage can only come from leveraged ETFs (section 7, step 2b).
- In a standard US margin account (Reg T), positions held overnight are limited to roughly 2x.
- Reaching 4x would require futures or portfolio margin. The parameter stays at 4.0, but the system must never request more than the broker allows.

**Key assumption:** Kelly figures depend on the strategy's real Sharpe. The value is estimated from the backtest and updated with live data; it is never set by hand.

---

## 3. Architecture and daily flow

Data flows one way every trading day, and no order reaches the broker without passing through the risk manager.

```
 1. Data (data/)        ->  2. Strategy (strategy/)  ->  3. Risk (risk/)
    download OHLCV           SMA200, 55/20 breakout       unit = 1% per 1N
    validate quality         2N stop, pyramiding          L = min of 5 limits <= 4x
    store in SQLite          output: 0..4 units           veto + kill switch at -40%
                                                                |
                                                     only approved orders
                                                                v
 6. Reporting (reporting/) <- 5. Log (SQLite)       <-  4. Execution (execution/)
    monthly HTML report        signals, orders, fills      target vs current position
    vs SPY and 60/40           risk and daily equity       20% band, sells first
    AI summary, read-only      events and alerts           confirm and reconcile
                                                                ^
                                                                | orders and fills
                                                                v
                                                     Broker selected by MODE
                                                     backtest: SimBroker
                                                     paper: Alpaca or IBKR paper
                                                     live: IBKR, real money
```

Strategy and risk are pure functions. Only Execution talks to the broker. Switching between backtest, paper and live is changing `mode`.

---

## 4. Tech stack and repository layout

| Need | Tool |
| --- | --- |
| Data and computation | pandas, numpy |
| Configuration | pydantic + pydantic-settings, `config.yaml` + `.env` |
| Database | SQLite (`data/stride.db`) |
| Paper broker | `alpaca-py` (official SDK) |
| Live broker | IBKR API via IB Gateway (`ib_async`) |
| NYSE calendar | `pandas_market_calendars` |
| Tests | pytest, pytest-cov |
| Charts and reports | matplotlib + HTML templates (jinja2) |
| Daily job | cron on the server (see section 15) |
| Code quality | ruff (lint + format), mypy |

```
stride/
├── config.yaml                  # strategy and risk parameters (versioned)
├── config.paper_kelly.yaml      # parallel paper account (Phase 4)
├── .env.example                 # credential template, no real values
├── pyproject.toml
├── docs/SPEC.md                 # this document
├── CLAUDE.md / AGENTS.md        # instructions for coding agents
├── src/stride/
│   ├── config.py                # load and validate configuration
│   ├── data/
│   │   ├── sources.py           # price download per provider
│   │   ├── store.py             # SQLite read/write
│   │   ├── quality.py           # data validation
│   │   ├── calendar.py          # NYSE trading days
│   │   └── leveraged.py         # leveraged ETF mapping + synthetic series
│   ├── indicators.py            # SMA, ATR, rolling highs/lows, EWMA vol
│   ├── strategy/
│   │   ├── rules.py             # the "personality" rules
│   │   └── signals.py           # target units per asset
│   ├── risk/
│   │   ├── sizing.py            # volatility-based unit sizing (Turtle units)
│   │   ├── leverage.py          # Kelly + vol targeting + caps + leveraged ETFs
│   │   └── guard.py             # hard limits and kill switch (veto)
│   ├── execution/
│   │   ├── broker.py            # abstract Broker interface
│   │   ├── sim_broker.py        # simulated broker for backtest and tests
│   │   ├── alpaca_broker.py
│   │   ├── ibkr_broker.py
│   │   └── orders.py            # current vs target -> orders
│   ├── backtest/
│   │   ├── engine.py
│   │   ├── costs.py
│   │   └── metrics.py
│   ├── reporting/
│   │   ├── report.py            # monthly report
│   │   ├── alerts.py            # Telegram alerts
│   │   ├── correlations.py
│   │   └── llm_summary.py       # optional AI summary (read-only)
│   └── run_daily.py             # entry point of the daily job
├── tests/
└── notebooks/                   # exploration only, never production logic
```

Design rule: `strategy/` and `risk/` are pure functions (input data → target positions), with no network and no broker. Backtest, paper and live therefore run exactly the same logic.

---

## 5. Data module

The system needs daily OHLCV bars adjusted for dividends and splits; everything else is derived from them.

**Candidate sources (all free)**

| Source | Use | Advantage | To verify |
| --- | --- | --- | --- |
| Alpaca Market Data | Daily data in paper/live | Same account as the paper broker | Historical depth and dividend adjustment on the free plan |
| Tiingo (free plan) | Long history for backtest | Includes adjusted close | Free-plan request limits |
| Stooq (CSV) | Fallback | No sign-up | Whether prices are adjusted |

**Sources (decided in 1.1)**

- **Primary: Tiingo**, adjusted history since 2007.
- **Second source for validation 4: Alpaca**, only if its free plan delivers consolidated (SIP) daily bars: request `feed=sip` and `adjustment=raw`. The Basic plan serves SIP history except the latest 15 minutes, so the day's bar is available at the 18:00 run; this must be confirmed with paper keys. If Alpaca only gives the IEX feed, its closes can differ from the official close and would raise false alarms; the second source is then Stooq. **Decided in 1.2:** once the owner's paper keys are in `.env`, Alpaca's free-plan daily closes are checked against the official close (the primary source's unadjusted close); if they do not match, Stooq is implemented as the secondary source.
- Validation 4 compares **unadjusted** closes of recent days: each provider adjusts dividends differently, so comparing adjusted history produces false differences.
- **Download etiquette (1.2):** respect provider rate limits. Downloads are **incremental** (only bars newer than the last stored date, plus a small overlap to pick up revisions), results are **cached locally** in SQLite, and HTTP 429 and transient errors are retried with **exponential backoff** (up to 3 retries).

**Crypto history:** the bitcoin (IBIT) and ether (ETHA) ETFs exist since 2024. For the backtest, use USD spot prices of BTC and ETH from Coinbase Exchange's free public API (BTC-USD from 2015-07-20, ETH-USD from 2016-05-18; Gemini opened in October 2015 and does not help). Each asset enters the backtest once it has enough history for its indicators (about 200 days for `SMA200`).

- **Session bars, not UTC candles.** The UTC daily candle closes at 20:00 ET in summer and 19:00 ET in winter, 3 to 4 hours after the ETF's close, and would give the backtest information the ETF never had and different ATRs. Spot bars are built from 15-minute candles on NYSE trading days only: open = 9:30 ET, high/low = extremes between 9:30 and the session close, close = price at the session close (16:00 ET, or the early close). Weekend gaps then appear in spot exactly as in the ETF, which is the crypto risk this spec wants to measure.
- **Ratio splice (mandatory).** Without scaling, the series would jump from tens of thousands of dollars (BTC) to tens of dollars (ETF) and break `SMA200`, `HH55` and `N` for months. Spot prices before the switch are multiplied by `ETF close / spot 16:00 close` on the ETF's first full trading day; from that day on, the ETF is used. Raw spot bars are stored untouched; the spliced series is built by a separate function. Because position size uses `N / price`, scaling changes no signal outside the splice point.
- The `source` column marks where the series switches.

**Data contract** (`prices` table in SQLite)

| Column | Type | Rule |
| --- | --- | --- |
| `symbol` | text | One of the universe symbols (or a leveraged ETF from the map) |
| `date` | ISO date | NYSE trading day; unique key with `symbol` |
| `open`, `high`, `low`, `close` | real | Adjusted; `low <= open, close <= high` |
| `volume` | integer | >= 0 |
| `source` | text | Provider that delivered the row (or `synthetic`) |
| `fetched_at` | UTC timestamp | Download time |

**Mandatory validations** (`data/quality.py`). Validations 1 and 2 stop the run if any fails; validation 3 acts per symbol (below); validation 4 never stops the run. Thresholds live in `config.yaml` under `data.quality`; they are data parameters, not strategy parameters, so changing them does not restart the experiment.

1. No missing bars in the last 300 **NYSE trading days** (from the T07 calendar). For assets with less history (e.g., ETHA, BITX), the window is all available history.
2. No daily change above 25% without an event flag (split or provider error), within the same window of the last 300 NYSE trading days as validation 1. Older genuine moves (e.g., ETH's +53% on 2017-03-13) do not stop today's run.
3. The latest bar is **day t**, the evaluation day: the signal uses the close of t and executes at the open of t+1. At the end of the run window (section 8):
   - if the **primary** source lacks bar t for a symbol, **that symbol is blocked** for the day (the position is kept, no orders are sent) and an alert is sent;
   - if the primary source lacks bar t for **all** symbols, the whole run is skipped and an alert is sent;
   - if only the **secondary** source lacks bar t, it is an alert only.
4. Two sources are compared on **unadjusted** closes of recent days:
   - on the historical sample, a difference above 0.5% only logs an alert;
   - if the **latest bar** of a symbol differs by more than 2% between sources, orders for **that symbol** are blocked that day; the rest of the portfolio trades normally. This covers the dangerous case: trading on a wrong price for today.

**Anti-look-ahead rule:** no function in `strategy/` may read data dated after the evaluation date. A test injects fake future prices and checks that past signals do not change.

---

## 6. Strategy engine: the "personality"

Each asset goes through the same state machine every day after the close. The output is a target number of units between 0 and 4. **Long only, never short.**

**Indicators** (all computed with data up to and including the close of day t)

| Name | Definition | Origin |
| --- | --- | --- |
| `SMA200` | Simple average of the last 200 closes | Paul Tudor Jones |
| `HH55` | Highest high of the previous 55 days (excluding t) | Turtles, system 2 |
| `LL20` | Lowest low of the previous 20 days (excluding t) | Turtles, system 2 |
| `N` | 20-day ATR (Wilder / exponential average of true range) | Turtles |
| `vol60` | 60-day annualized volatility (EWMA) | Volatility targeting |

**Rules per asset, in priority order**

1. **Trend filter:** if `close < SMA200`, target units = 0 and everything open is closed. (`TREND_FILTER`)
2. **Protective stop (Seykota):** if `close < last_entry − 2N`, target units = 0. (`STOP_2N`)
3. **Channel exit:** if `close < LL20`, target units = 0. (`EXIT_LL20`)
4. **Entry:** with no position, if `close > HH55` and the filter passes, target units = 1. (`ENTRY_HH55`)
5. **Pyramiding:** with an open position, add 1 unit each time `close` rises 0.5N above the last entry, up to 4 units. (`PYRAMID`)
6. **No change:** otherwise keep current units. (`HOLD`)

Every rule that fires is logged with its name, so every order has a traceable explanation.

**Unit size (Larry Hite / Turtles):** each unit risks 1% of equity against a 1N move.

```
unit_usd = (risk_per_unit * equity / N) * price
```

**Exposure caps (before leverage)**

| Cap | Limit | Reason |
| --- | --- | --- |
| Per asset | 4 units | Turtle rule |
| Equities group (SPY, EFA) | 6 units | Highly correlated |
| Bonds group (IEF, TLT) | 6 units | Highly correlated |
| Metals group (GLD, SLV) | 6 units | Highly correlated |
| Crypto group (IBIT, ETHA) | 4 units | Highly correlated, 70–80% historical drawdowns |
| Total portfolio | 12 units | Turtle rule per direction |

DBC (commodities) and UUP (dollar) share no group: they tend to move differently from the rest and add diversification. When signals exceed a cap, the most recent units are cut first (last in, first out).

All numbers in this section are parameters in `config.yaml`. They are frozen before the live period and not changed during the 12 months; if changed, the experiment restarts.

---

## 7. Risk management and leverage

Leverage is computed every day as the minimum of five limits. The risk manager can veto any order.

**Step 1 — Kelly estimate.** From the daily returns of the unlevered strategy (backtest plus live history, 3-year window):

```
L* = (mu − r_f) / sigma^2
```

where `mu` is the expected annual return, `r_f` the risk-free rate and `sigma` the annual volatility. With `kelly_fraction = 1.0` (full Kelly), the target is L* as is.

**Step 2 — Effective leverage**

```
L = min( kelly_fraction * L*,
         max_portfolio_vol / sigma_60,
         max_leverage,
         L_capacity )      # 1.4: L_capacity = L_borrow × max_etf_portfolio_leverage
```

| Limit | Default | Rule |
| --- | --- | --- |
| `kelly_fraction` | 1.0 | Full Kelly, project decision |
| `max_portfolio_vol` | 0.60 | Max annual portfolio vol; with Sharpe 0.6, full Kelly implies about 60% |
| `max_leverage` | 4.0 | Absolute cap |
| `L_capacity` | L_borrow × `max_etf_portfolio_leverage` (2.5) | 1.4: L_borrow is the margin leverage read from the broker (about 2.0 under Reg T) only when `allow_margin` is true and equity ≥ $2,000; otherwise 1.0 (no loan) |
| `allow_margin` | false | The real account never borrows: capacity is 2.5x whatever the balance, never a margin call, maximum loss = what was deposited. The paper Kelly account sets it to true: capacity = min(4.0, Reg T × 2.5), and the guard's 25% excess-liquidity check sets the practical limit |
| `dd_delever_at` | −20% | If drawdown exceeds this, L is multiplied by 0.5 until a new equity high |

**Decided in 1.3:**

- **`sigma_60` is forward-looking:** σ = √(w′Σw) × √252, where w are today's **target weights without leverage** (target dollars / equity) and Σ is the **EWMA covariance matrix** of daily returns (span 60, the same EWMA as `vol60`). This reflects a new position the same day and accounts for correlations. With no positions σ = 0 and the vol-target limit does not apply (no division by zero).
- **Kelly fallback:** if L* is negative or history has fewer than 250 days, only the **Kelly term** is replaced by 1.0. The other limits still apply, so L may be **below 1** (that is the purpose of the vol target).
- **Order of operations:** L = min(limits); then, while the drawdown deleveraging is active, L × 0.5.
- **Drawdown basis:** a **time-weighted return index** that excludes deposits and withdrawals; drawdown = 1 − index / historical maximum. The historical maximum is **stored in the database** and survives restarts. It is reset only by an explicit human action recorded in the `events` table, when the `KILL_SWITCH` file is deleted.
- **Capacity (1.4, replaces the 1.3 "below $2,000" rule):** two kinds of leverage are kept apart. *Borrowing* (margin) is the cash usable per dollar of equity: 1.0 in a cash account. *Built-in* leverage (leveraged ETFs) is the exposure per dollar invested, up to 2.5x on average. Total capacity is their product, `L_borrow × max_etf_portfolio_leverage`, so crossing $2,000 never lowers the available leverage. Step 2b fits the cash in `L_borrow × equity`.

**Step 2b — Leveraged ETFs (no margin).** When the gross target exposure E exceeds equity P and no margin is available, the system reaches it with 2x/3x ETF versions:

1. Compute k = E / P. If k <= 1, use the normal ETFs.
2. If k > 1, each asset with a leveraged version uses the smallest multiple m such that total cash (sum of target / m) fits in P.
3. Assets without a liquid leveraged version stay at 1x.
4. If it still does not fit, scale all positions down proportionally.
5. Portfolio leverage through this route never exceeds `max_etf_portfolio_leverage` (2.5).
6. Each asset is held in exactly one instrument at a time. Switching between normal and leveraged versions counts as a sell plus a buy and respects the rebalance band and the no-same-day rule.
   - **Hysteresis (1.2)**, parameters in `config.yaml` → `leverage_switch`: switch to a leveraged version only if k >= `enter_k` (1.25); switch back to the normal ETF only if k <= `exit_k` (0.95); keep an instrument at least `min_hold_days` (20) trading days unless the position is closed. Between the two thresholds the current instrument is kept.
   - Only instruments that pass the filters below are candidates. If the 2x version fails and the 3x passes (e.g. TLT: UBT fails, TMF passes), the asset may go directly from 1x to 3x.
7. Signals, N and stops are always computed on the **underlying** asset, never on the leveraged ETF, so switching instruments never changes a signal.

**Candidate instrument map** (verified in task T21; issuer facts in `reference/etf_facts.yaml`)

For Phase 1, public data is enough: under SEC rule 6c-11 issuers publish on their website the 30-day median bid-ask spread, the expense ratio and net assets. Per instrument, `reference/etf_facts.yaml` (versioned) records `expense_ratio`, `aum_usd`, `median_spread_30d`, `as_of` and the source URL; expense ratio and assets are pre-filled from SEC EDGAR filings where possible and the rest is collected by the owner (agents never accept website disclaimers or terms). A leveraged ETF is used only if its **30-day median spread is at most 0.20%** and its **net assets are at least $50 million**; one that fails is dropped and its asset trades at 1x (or at the other multiple, if that one passes). IBKR-specific checks (permissions for leveraged products, fractional-share support per ETF, actual commission) are done in Phase 4 with the paper account, never with real credentials.

| Asset | Normal | 2x | 3x |
| --- | --- | --- | --- |
| S&P 500 | SPY | SSO | UPRO |
| Long bonds | TLT | UBT | TMF |
| Gold | GLD | UGL | — |
| Silver | SLV | AGQ | — |
| Bitcoin | IBIT | BITX | — |
| Ether | ETHA | ETHU (added in 1.2) | — |
| EFA, IEF, DBC, UUP | Normal | No known liquid version, stay at 1x | — |

Leveraged ETFs reset daily: in sideways markets they lose value even if the underlying ends flat. The trend filter reduces this because it exits when there is no trend, but the backtest must measure it (section 9).

**Step 3 — Risk manager veto** (`risk/guard.py`). An order is rejected if it:

- would open a short position;
- exceeds 60% of equity;
- would be the 13th order of the day (max 12);
- would buy an asset sold the same day, or vice versa, counted by **underlying** (SPY, SSO and UPRO are one asset). **Exception (1.4):** an instrument switch (e.g. SPY → SSO) may sell and buy the same day only if (1) the orders are marked as an instrument switch by the leverage module, (2) the underlying's net exposure changes by less than the rebalance band, and (3) the sell goes first. Any other combination, such as an exit by signal followed by an entry in the leveraged version, stays forbidden. In a cash account, buying with the proceeds of an unsettled sale is allowed; the 20-day minimum hold prevents selling those shares before the original sale settles;
- would leave margin excess liquidity below 25% of the broker requirement;
- comes on a day whose data failed section 5 validations 1–2;
- is for a symbol blocked by section 5 validation 3 (primary source lacks bar t) or validation 4 (latest bar differs between sources).
- comes while the system is **halted** by a reconciliation break (step 4); the kill-switch liquidation is the only exception;
- would sell a symbol that is not held, or more than is held (a 1-cent rounding tolerance, usable once per position).

**Step 4 — Kill switch.** Evaluated once a day, at the 18:00 run on the close. If the time-weighted index falls 40% from its historical maximum, the system:

1. cancels pending orders;
2. closes all positions at market at the next open;
3. writes the `KILL_SWITCH` file and sends an alert;
4. does not trade again until a human deletes that file by hand.

**Two levels (1.4).** A reconciliation break does not liquidate: when the system is not sure what it holds, selling blind can make things worse and costs about 1% per order.

| Trigger | Action |
| --- | --- |
| Drawdown of −40% (time-weighted index) | **Kill switch:** liquidate everything at the next open, using the positions **the broker reports**; write `KILL_SWITCH` |
| Reconciliation break on two consecutive days (any symbols) | **Halt:** freeze the system (no new orders, positions kept), write `HALT`, alert, and wait for a person to resolve it |

During a halt the drawdown kill switch stays armed: liquidating with the broker's positions is safe even when the system is out of sync. Breaks on different symbols on consecutive days count, because they suggest a system problem rather than an isolated case. A person resolves a halt by fixing the reconciliation, deleting `HALT` by hand and recording it (`resolve-halt`); a halt does not reset the drawdown peak.

**Reconciliation break.** A symbol breaks when |broker value − system value| exceeds the larger of 1% of the symbol's value and $1 (the floor avoids firing on rounding of fractional shares in a small account). A symbol the broker holds and the system does not expect, or the reverse, is also a break. The thresholds live in `config.yaml` (`risk.reconciliation_tolerance`, `risk.reconciliation_floor_usd`).

**−40% is a trigger, not a guaranteed maximum loss.** A fall during the day, or a gap at the next open when the positions are closed, can push the real loss beyond 40%.

Equity is computed from **closing prices × the broker's positions + cash**, exactly as in the backtest, and compared with the equity the broker reports; any difference is logged.

---

## 8. Execution and broker connection

The same code runs in simulation, paper and live; only the `Broker` implementation loaded by `mode` changes.

**`Broker` interface** (`execution/broker.py`)

| Method | Returns |
| --- | --- |
| `get_account()` | Equity, cash, buying power, margin requirement |
| `get_positions()` | Quantity and average price per symbol |
| `submit_order(symbol, side, notional, type, limit_price)` | Order ID |
| `get_order(order_id)` | Status, filled quantity, average price, commission |
| `cancel_all()` | Number of cancelled orders |
| `is_market_open(date)` | Whether NYSE is open that day |

Implementations: `SimBroker` (backtest, tests), `AlpacaBroker` (paper), `IBKRBroker` (paper and live).

**Modes**

| `mode` | Broker | Real money |
| --- | --- | --- |
| `backtest` | `SimBroker` | No |
| `paper` | Alpaca paper or IBKR paper | No |
| `live` | IBKR | Yes; also requires `LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY` in `.env` |

**Parallel paper account:** a second system instance runs the same daily job with `config.paper_kelly.yaml` ($10,000 simulated, margin enabled, `kelly_fraction: 1.0`) and its own database `data/paper_kelly.db`. Same signals; only capital and available leverage differ.

**Order flow**

1. `orders.py` compares broker positions with targets and computes the dollar difference per symbol.
2. A difference below 20% of the target weight, or below $1, creates no order (rebalance band).
   - **Cost-aware resizing (owner request, 2026-10-23):** a top-up buy on a position whose rule is `HOLD` (the target moved only because equity, L or N moved) is skipped when its commission plus spread exceeds `execution.max_resize_cost_pct` (0.5%) of the order. Sells (the drawdown deleveraging included), entries, pyramid adds, exits and switch legs are never skipped.
3. Sells go before buys, to free cash.
4. Orders are sent for the next day's open: limit orders at previous close ±0.5%, or market-on-open if the broker does not accept limits on fractional orders.
5. Every order passes `risk/guard.py` before submission.
6. Mid-session, the system checks each order's status; unfilled limits are cancelled at the close and re-evaluated next day.
7. At the end, reconciliation: broker positions vs expected (see kill switch).

**Monthly deposits:** when account cash increases without a sale that explains it, the system records a deposit in `cash_flows`. On the next run, the higher equity enlarges target positions and the deposit is invested by the normal rules; with no active signals it stays in cash. **The system never buys just because money arrived.**

**Daily schedule** (New York time)

| Time | Action |
| --- | --- |
| 18:00 | Download data (retrying until 20:00 if the bar for today is missing), validate, signals, risk, orders for tomorrow. Free providers publish the adjusted close some time after 16:00; if the bar for today is still missing at 20:00, no trading that day and an alert |
| 09:25 | Submit orders for the open |
| 12:00 | Check fills |
| 16:05 | Cancel pending orders and reconcile |

All broker calls retry with exponential backoff (max 3) and **never resend an unconfirmed order** without first confirming the previous one does not exist, to avoid duplicates.

---

## 9. Backtester and validation

The backtest uses the same `strategy/` and `risk/` logic as live, with `SimBroker` filling at the open of the day after the signal.

**Simulation assumptions**

| Element | Model |
| --- | --- |
| Signal | Close of day t |
| Execution | Open of day t+1 |
| Spread and slippage | 5 bps per side (10 bps for crypto), configurable per symbol |
| Commission | IBKR Pro Fixed: `min(max(0.005 * shares, 1.00), 0.01 * notional)` per order |
| Risk-free rate | 3-month T-bill, FRED series `DTB3` (free); the value known at t−1 is used on day t, never a later one |
| Margin interest | Risk-free rate + 1.5% per year on borrowed amount |
| Uninvested cash | Earns the risk-free rate |
| Dividends | Included via adjusted prices |
| Leveraged ETFs | Before launch, synthetic daily return = m × underlying return − (m − 1) × (DTB3 + spread) / 252 − annual fee / 252, where the annual fee is the ETF's own net expense ratio from `reference/etf_facts.yaml` (`costs.leveraged_etf_fee` is only a fallback when the value is missing, and using it logs a warning). The spread is not invented: it is calibrated per asset on the period where both the real ETF and the underlying exist, and the synthetic series must differ from the real one by less than 2% per year |
| Deposits | $20 on the first trading day of each month, as in live |
| Period | From 2007 for traditional ETFs and from 2015 for crypto (spot price before their ETFs existed) to today |

Note on commissions: with small orders, IBKR's 1%-of-trade-value cap is the binding term, so **every order costs about 1%**. This is the dominant cost of the real account and must be modeled exactly.

**Validation protocol**

1. **Walk-forward:** 5-year windows to estimate parameters and 1 year out of sample, rolling yearly. Only out-of-sample results are reported.
2. **Sensitivity:** change each parameter ±20% (e.g., 55 → 44 and 66 days). If Sharpe drops more than 30%, the parameter is fragile and is logged as a risk.
3. **Block bootstrap:** 5,000 resamples of daily returns in 20-day blocks for confidence intervals on Sharpe and max drawdown.
4. **Anti-look-ahead test:** see section 5.
5. **Leverage comparison:** run with fixed L at 1x, 2x, 3x and 4x, and with the dynamic L of section 7.

**Always-reported metrics**

| Metric | Definition |
| --- | --- |
| CAGR | Compound annual growth rate (time-weighted) |
| Volatility | Annualized std of daily returns |
| Sharpe | (Return − risk-free) / volatility |
| Sortino | Like Sharpe, downside volatility only |
| Max drawdown | Largest peak-to-trough fall |
| Calmar | CAGR / max drawdown |
| Turnover | Annual traded amount / average equity |
| Total costs | Spread + commissions + interest + leveraged ETF fees, % per year |
| Average exposure | Average leverage used |
| Instrument switches | Number of normal/leveraged switches and their total cost (spread + commissions), per asset and in total |
| Benchmarks | Buy-and-hold SPY; 60/40 SPY/IEF rebalanced monthly |

Quality alarm: an out-of-sample Sharpe above 1.5 is treated as a sign of a methodological error and audited before continuing (look-ahead, costs, survivorship bias, badly adjusted data).

---

## 10. Logs, reports and monitoring

Every decision is stored in SQLite, including days with no trades; this log is the experiment's evidence.

| Table | One row per | Key columns |
| --- | --- | --- |
| `runs` | Daily run | date, mode, status (ok/error/no data), duration, code version (git hash) |
| `signals` | Asset and day | indicators, rule fired, current and target units |
| `risk_decisions` | Day | L*, effective L, binding limit, drawdown, vetoes applied |
| `orders` | Order sent | symbol, side, amount, type, limit price, originating rule |
| `fills` | Execution | price, quantity, commission, slippage vs previous close |
| `equity` | Day | equity, cash, gross exposure, leverage |
| `cash_flows` | Deposit or withdrawal | date, amount, type, how it was detected |
| `events` | Event | type (alert, error, kill switch, reconciliation), detail |

With monthly deposits, returns are measured as **time-weighted return (TWR)**: deposits never count as gains. Reports also show total deposited vs balance, to see the dollar gain.

**Monthly report** (`reporting/report.py`, HTML)

- Equity curve vs SPY and 60/40.
- Section 9 metrics for the month and cumulative.
- Trade list with the rule that originated each trade.
- Backtest-vs-reality gap: simulated vs real return for the same period, and average slippage.
- Leverage used and which limit bound it each day.
- Correlation matrix of the universe (3-year daily returns; flag pairs above 0.7).

**AI summary (optional):** `llm_summary.py` reads the report and writes what the system did and why. Read-only database access; it can never create orders or change parameters. Off by default.

**Alerts** (Telegram) when: a daily run fails or does not run; data fails validation; the risk manager vetoes an order; reconciliation differs; drawdown crosses −20% or the kill switch fires.

---

## 11. Configuration and security

Parameters live in `config.yaml` (versioned); credentials in `.env` (never versioned). Reference files are in the repository root.

**Security rules**

- `.env`, `data/` and `KILL_SWITCH` are in `.gitignore` from the first commit.
- No credential appears in logs, reports, error messages or AI prompts.
- API keys are created with minimum permissions: trading yes, withdrawals and transfers no.
- `config.py` validates types and ranges at startup; an out-of-range value (e.g., `max_leverage: 10`) stops the system.
- `live` mode requires the exact `LIVE_CONFIRM` value; without it, the system starts in `paper` and says so in the log.
- Coding agents (Claude Code, Antigravity) never run in `live` mode and never receive live-trading credentials. They may use data-only API keys (Tiingo) and paper-trading keys (Alpaca paper) placed in `.env` by the owner, for development and verification only, and must never print, log or commit any key.

---

## 12. Phases and exit criteria

No phase starts until the previous one meets its criterion; real money enters only in Phase 5.

1. **Foundations:** repository, configuration, database, data download.
   - Exit: data for the 10 assets loaded (from 2007; crypto from 2015) with all section 5 validations green.
2. **Strategy and risk:** indicators, rules, unit sizing, leverage and risk manager, as pure functions.
   - Exit: test coverage >= 90% in `strategy/` and `risk/`, and the anti-look-ahead test passing.
3. **Backtest:** engine, costs, metrics and full validation protocol.
   - Exit: out-of-sample Sharpe above SPY's and below 1.5, stable sensitivity, documented results.
4. **Paper trading (2 months minimum) and start of the parallel Kelly account:** daily job running with `mode: paper`.
   - Exit: 40 consecutive trading days without failures, clean reconciliation, and orders identical to what the backtest would produce for those days.
5. **Live (12 months):** `mode: live` with $100 + $20/month at IBKR and frozen parameters.
   - Success criterion: H1, H2 and H3 evaluated in the final report; the parallel Kelly account runs the same 12 months.
6. **Optional, C++ core:** rewrite the backtest engine with pybind11.
   - Exit: results identical to the Python engine within 1e-9 and a measured speedup.
7. **Optional, research:** scalping simulator that demonstrates, with real spreads, commissions and latency, the break-even win rate of seconds-scale strategies.

---

## 13. Task backlog for coding agents

Rules for agents:

- One task per branch and per pull request, with the ID in the title (`T05: price downloader`).
- Every new function ships with tests in `tests/`; `pytest` and `ruff check` must pass.
- Do not change values in `config.yaml` or create new parameters unless asked.
- Never use real credentials, never run `mode: live`, and never touch the "do not assign" files.
- If this spec is ambiguous, leave the question in the pull request instead of inventing.

| ID | Task | Files | Acceptance criterion | Phase |
| --- | --- | --- | --- | --- |
| T01 | Folder structure, `pyproject.toml`, ruff and pytest config | root, `src/stride/`, `tests/` | `pytest` and `ruff check` run with no errors | 1 |
| T02 | `.gitignore` and `.env.example` per section 11 | `.gitignore`, `.env.example` | `.env`, `data/` and `KILL_SWITCH` ignored | 1 |
| T03 | Load and validate `config.yaml` with pydantic | `config.py` | A test rejects `max_leverage: 10`; another accepts the reference file | 1 |
| T04 | `prices` table and upsert in SQLite | `data/store.py` | Inserting the same row twice does not duplicate it | 1 |
| T05 | Daily price downloader for one symbol | `data/sources.py` | Returns a DataFrame with the section 5 contract columns | 1 |
| T06 | The 4 data validations | `data/quality.py` | One synthetic-data test per failing validation | 1 |
| T07 | NYSE calendar: `is_trading_day(date)` | `data/calendar.py` | 2026-11-26 (Thanksgiving) returns `False` | 1 |
| T08 | Indicators: SMA, ATR, rolling highs/lows excluding t, EWMA vol | `indicators.py` | Tests against hand-computed values on a 30-day series | 2 |
| T09 | Section 6 state machine | `strategy/rules.py`, `strategy/signals.py` | One test per rule, each with its rule name in the output | 2 |
| T10 | Unit sizing and exposure caps | `risk/sizing.py` | Tests for each cap; trimming removes the most recent unit first | 2 |
| T11 | Cost model: spread, IBKR commission, margin interest | `backtest/costs.py` | Tests with known amounts, including the 1% cap on small orders | 3 |
| T12 | Section 9 metrics | `backtest/metrics.py` | Tests against a series with known results | 3 |
| T13 | `SimBroker` filling at the open of t+1 | `execution/sim_broker.py` | An order sent on t fills at the `open` of t+1 with spread applied | 3 |
| T14 | Anti-look-ahead test | `tests/test_lookahead.py` | Injecting fake future prices changes no past signal | 3 |
| T15 | HTML report with equity curve and metrics table | `reporting/report.py` | Generates an HTML file from a test database | 3 |
| T16 | Telegram alerts | `reporting/alerts.py` | Sends a test message with a test bot | 4 |
| T17 | Daily job script and cron setup guide | `run_daily.py`, `README.md` | `python -m stride.run_daily --mode backtest` finishes without errors | 4 |
| T18 | `AlpacaBroker` for paper only | `execution/alpaca_broker.py` | Reads account and positions in paper; refuses to start with non-paper keys | 4 |
| T19 | Daily BTC/ETH history from a free public API, spliced with IBIT/ETHA | `data/sources.py` | Continuous series with no gaps; `source` marks the spot-to-ETF switch | 1 |
| T20 | Correlation matrix of daily returns across the 10 assets | `reporting/correlations.py` | 3-year matrix in the report; flags pairs above 0.7 | 3 |
| T21 | Verify candidate leveraged ETFs and build synthetic series | `data/leveraged.py` | Liquidity/spread/fee table; synthetic vs real differs < 2%/yr where both exist | 1 |
| T22 | `cash_flows` table and time-weighted return | `data/store.py`, `backtest/metrics.py` | With $20 deposits and 0% return, TWR is 0% | 3 |
| T23 | Separate config and database for the parallel Kelly paper account | `config.paper_kelly.yaml`, `run_daily.py` | Both instances run the same day without sharing database or credentials | 4 |

**Do not assign to agents** (written or reviewed line by line by a human): `risk/leverage.py`, `risk/guard.py`, the kill switch, `execution/orders.py`, `execution/ibkr_broker.py`, enabling `mode: live`, and any change to risk parameters.

---

## 14. Known risks and limitations

The biggest risk is full Kelly itself: it maximizes growth in theory, but with typical drawdowns of 60–80%, so the −40% kill switch will likely fire at some point.

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Full Kelly with overestimated mu | If real Sharpe is lower, full Kelly over-bets and grows less than lower leverage | L* recomputed with live data; report compares against fixed 2x (half Kelly) |
| Kill switch vs full Kelly | Normal full-Kelly drawdowns exceed −40%; the system may stop within the year | Deliberate: not losing the account takes priority; the event is a documented result |
| $100 capital | No margin; commissions weigh heavily | Leverage module validated in backtest and paper with larger simulated capital |
| Broker commissions | IBKR's 1% cap on small orders costs ~$20/yr (~7% of a ~$300 account) | Modeled exactly in the backtest; low turnover by design; evaluate commission-free brokers |
| Broker maintenance minimum | An IBKR page says accounts under $100 may be closed | Verify the rule for the account's entity before depositing |
| One year is short | 12-month result is mostly noise | H2 evaluates consistency with the backtest, not just profit |
| Backtest overfitting | Simulated results better than real ones | Walk-forward, sensitivity, bootstrap, Sharpe > 1.5 alarm |
| Free data quality | Badly adjusted prices create false signals | Section 5 validations and two-source comparison |
| Operational failures | Server down, no internet, API outage | Free cloud server; alerts on missed runs; no trading if anything fails |
| Broker API changes | Rejected orders or incomplete data | Isolated `Broker` interface; integration tests in paper before each change |
| Crypto price gaps | BTC/ETH trade 24/7, the ETF does not: a weekend crash opens far below the stop | 4-unit crypto cap and volatility sizing (smaller dollar positions) |
| Leveraged ETF decay | Lose value in sideways markets; ~1% annual fee | Used only when target exposure exceeds capital; trend filter; measured with synthetic series |
| Deposits hiding losses | Balance can rise from $20 deposits while the strategy loses | TWR and total-deposited-vs-balance report |
| Taxes and regulation (Costa Rica) | Tax treatment of foreign-broker gains is unclear | Keep statements; consult an accountant before year end |
| Use for third parties | Publishing signals could fall under SUGEVAL regulation | The system trades only the owner's account; code and results are what is public |

Nothing in this document is financial advice or a promise of returns; the experiment may end with losses.

---

## 15. Deployment (proposed, zero cost)

| Piece | Tool | Cost |
| --- | --- | --- |
| Code | Private GitHub repository | $0 |
| CI | GitHub Actions on Ubuntu, Python 3.12: pytest, ruff check, ruff format --check, mypy on every PR and push to `main` (1–2 min per run; Windows runners count double on private repos). No merge with CI red | $0 |
| 24/7 server | Oracle Cloud "Always Free" VM (ARM; 2 OCPU / 12 GB since June 2026) | $0 |
| Broker connection | IB Gateway in Docker with IBC auto-login and daily auto-restart | $0 |
| Daily job | cron on the server | $0 |
| Alerts | Telegram bot | $0 |
| Backups | Daily database copy to a private location | $0 |
| AI summary | Off by default | $0 |

Notes:

- Oracle reduced its free tier in June 2026 without public notice; everything runs in Docker with daily backups so moving servers takes about an hour.
- IB Gateway periodically requires approving a login in IBKR Mobile (2FA). If not approved, the system does not trade that day and alerts — it fails safe.
- Alternative: if Alpaca accepts Costa Rica residents for live accounts, no gateway is needed and the job can run on GitHub Actions (2,000 free minutes/month on private repos; STRIDE needs ~250).

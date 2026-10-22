# Design proposal: leverage, risk guard and kill switch

**Status: approved by the owner on 2026-10-21, with the decisions below (SPEC 1.3).** Each module goes in its own PR, which the owner reviews line by line before merging.

Covers `src/stride/risk/leverage.py`, `src/stride/risk/guard.py` and the kill switch (`src/stride/risk/kill_switch.py` for the decision, plus a small I/O part in execution). All parameters already exist in `config.yaml`; none are added or changed.

## Principles

- **Pure decisions, thin I/O.** All three modules are pure functions: inputs in, a decision out, no network, no file, no clock. Reading the broker, writing the `KILL_SWITCH` file and sending alerts happen in the execution layer, which only *acts* on the decisions.
- **Every decision is explainable.** Each function returns a dataclass with the value, the limit or rule that bound it, and the inputs used. That is what goes into the `risk_decisions` table (SPEC section 10).
- **Fail safe.** Any missing or non-finite input gives the most conservative result: L = 1.0 (no leverage), an order vetoed, or the kill switch fired.

## 1. `risk/leverage.py`

### 1a. Kelly estimate (SPEC 7, step 1)

```
kelly_estimate(daily_returns, risk_free) -> KellyEstimate(l_star, mu, sigma, days)
```

- `daily_returns`: the **unlevered** strategy's daily returns, backtest plus live, over the last 3 years (756 trading days).
- `mu` = mean daily excess return × 252; `sigma` = std of daily returns × √252; `L* = mu / sigma²`.
- If there are fewer than `min_history_days_for_kelly` (250) days, or L* ≤ 0, the step returns "no estimate" and **L = 1.0** (SPEC: "If L* is negative or history has fewer than 250 days, L = 1.0").

### 1b. Effective leverage (SPEC 7, step 2)

```
effective_leverage(kelly, sigma_60, broker_leverage, equity, drawdown, delever_active, risk)
    -> LeverageDecision(leverage, binding_limit, limits: dict[str, float], delever_active)
```

`L = min(kelly_fraction · L*, max_portfolio_vol / sigma_60, max_leverage, L_broker, L_capital)`. The binding limit is the name of the smallest term (`KELLY`, `VOL_TARGET`, `MAX_LEVERAGE`, `BROKER`, `CAPITAL`).

- `L_broker`: read from the broker by the execution layer and passed in. In the backtest, `SimBroker` cash only means 1.0, or a configured Reg T value for the Kelly paper account.
- `L_capital`:
  - equity ≥ `margin_min_equity` ($2,000): margin is allowed, so `L_capital = L_broker`;
  - below $2,000: **no margin**, and leverage only through leveraged ETFs, so `L_capital = max_etf_portfolio_leverage` (2.5).
- **Drawdown deleveraging:** if the drawdown from the equity high exceeds `dd_delever_at` (20%), L is multiplied by 0.5. The flag `delever_active` stays on **until a new equity high**; the caller passes it in and stores the returned value.
- L is never above `max_leverage` (4.0) and never below 1.0 when an estimate exists. **Question 2** is about cases where L < 1.

### 1c. Leveraged ETF route (SPEC 7, step 2b)

```
choose_instruments(targets_usd, equity, current, facts, filters, switch, today)
    -> dict[asset, InstrumentChoice(symbol, multiple, cash_usd, reason)]
```

1. `k = E / P` (gross target / equity). If k ≤ 1, use the normal ETFs.
2. **Eligibility:** only versions that pass `leveraged_etf_filters` (spread and AUM from `reference/etf_facts.yaml`). TLT can go straight from 1x to TMF at 3x when UBT fails.
3. **Hysteresis (`leverage_switch`):**
   - move to a leveraged version only if k ≥ `enter_k` (1.25);
   - move back to the normal ETF only if k ≤ `exit_k` (0.95);
   - in between, keep the current instrument;
   - keep each instrument at least `min_hold_days` (20) trading days, unless the position is closed.
4. For each asset with an eligible version, pick the **smallest multiple m** such that total cash (Σ target / m) fits in P.
5. If it still does not fit, scale every position down proportionally.
6. Portfolio leverage on this route is capped at `max_etf_portfolio_leverage` (2.5).
7. Signals, N and stops stay on the **underlying**. This module only picks which instrument holds the exposure.

A switch is reported as a sell plus a buy, so it goes through the rebalance band, the no-same-day rule and the guard. The backtest counts switches and their cost (SPEC 9).

## 2. `risk/guard.py` (SPEC 7, step 3)

```
check_order(order, context) -> GuardDecision(approved: bool, reasons: list[str])
```

`context` holds the account (equity, margin requirement, excess liquidity), the orders already approved today, the positions, the data status and the symbols blocked today. Every applicable veto is listed, not just the first:

| Reason | Rule (values from `config.yaml`) |
| --- | --- |
| `SHORT` | the order would leave a negative position |
| `ORDER_TOO_LARGE` | notional > `max_order_pct_equity` (60%) of equity |
| `TOO_MANY_ORDERS` | it would be order number `max_orders_per_day` + 1 (the 13th) |
| `SAME_DAY_REVERSAL` | it buys a symbol sold today or sells one bought today, including the underlying's other instrument |
| `MARGIN_BUFFER` | excess liquidity after the order < `margin_buffer` (25%) × broker requirement |
| `DATA_FAILED` | today's data failed section 5 validations 1–2 |
| `SYMBOL_BLOCKED` | the symbol is blocked by validation 3 or 4 (`quality.blocked_symbols`) |
| `KILL_SWITCH` | the kill switch is active |

A non-finite figure from the broker vetoes the order under the rule it makes uncheckable: a NaN or infinite holding gives `SHORT`, equity gives `ORDER_TOO_LARGE`, and excess liquidity or margin requirement (or a negative requirement) gives `MARGIN_BUFFER` on buys.

`check_orders(orders, context)` runs them in execution order (sells first). Each approved order updates the context, so the 12-order limit and the same-day rule count correctly. Every veto raises an alert.

## 3. Kill switch (SPEC 7, step 4)

Pure decision in `risk/kill_switch.py`:

```
kill_switch_decision(equity_history, kill_switch_dd, reconciliation_history) -> KillDecision(fire, reason)
```

- **Drawdown:** fire if equity is ≥ `kill_switch_dd` (40%) below its all-time high.
- **Reconciliation:** fire if broker and system positions differ by more than 1% on **two consecutive days**.

Actions, in the execution layer, in this order:
1. cancel pending orders;
2. send market-on-open sells for every position at the next open (these bypass the guard's size and count limits, but not the long-only check);
3. write the `KILL_SWITCH` file (git-ignored) with the reason and timestamp;
4. alert.

At startup, if `KILL_SWITCH` exists, the system does nothing but log and alert. **Only a human deletes the file.** No code path removes it.

## Tests planned (≥ 90% coverage, SPEC phase 2)

- Kelly: hand-computed L* on a known series; fewer than 250 days → 1.0; negative L* → 1.0.
- Each limit binding in turn (5 cases); the delever flag on, kept, and reset at a new high; equity below and above $2,000.
- ETF route: k ≤ 1; the smallest m fits; proportional scaling; the 2.5 cap; filters excluding UBT; hysteresis at 1.24/1.25 and 0.95/0.96; min hold of 20 days; closing a position resets the hold.
- Guard: one test per veto, several vetoes together, the 13th order, same-day reversal across instruments.
- Kill switch: −39.9% vs −40%; one day vs two days of mismatch; file present at startup blocks everything.

## Questions for the owner

1. **`sigma_60`:** the volatility of the **unlevered portfolio's** daily returns with the same EWMA as `vol60` (span 60, see PR #12)?
2. **L < 1:** if `max_portfolio_vol / sigma_60` is below 1, should L go below 1 (cutting exposure), or is 1.0 the floor? The SPEC's "L = 1.0" applies only when L* has no estimate.
3. **Drawdown basis:** for both the 20% delever and the 40% kill switch, is drawdown measured on **time-weighted** equity (deposits excluded) or on the raw balance? I propose time-weighted, so $20 deposits never hide a drawdown.
4. **Reconciliation:** is the 1% measured on each symbol's dollar value, or on total portfolio value?
5. **Kill switch timing:** evaluated on the daily close (at the 18:00 run), not intraday?

Once these are answered and this document is approved, I will write the three modules with their tests, each in its own PR for line-by-line review.

## Decisions (owner, 2026-10-21)

1. **`sigma_60` is forward-looking:** σ = √(w′Σw) × √252, with w the target weights of today without leverage and Σ the EWMA covariance matrix (span 60, zero-mean, the same weighting as `vol60`). With no positions σ = 0 and the vol-target limit does not apply.
2. **L may go below 1.** The "L* negative or < 250 days" fallback replaces only the Kelly term with 1.0; every other limit still applies. The 0.5 deleveraging is applied after the minimum.
3. **Drawdown on a time-weighted index** (deposits and withdrawals excluded): drawdown = 1 − index / historical maximum. The maximum is stored in the database (`risk_state`), survives restarts, and is reset only by an explicit human action recorded in `events` when the `KILL_SWITCH` file is deleted.
4. **Reconciliation per symbol with a floor:** a break when |broker − system| > max(1% of the symbol's value, $1), or when a symbol is held on only one side. Two consecutive days of breaks fire the kill switch. Thresholds go in `config.yaml` (`risk.reconciliation_tolerance: 0.01`, `risk.reconciliation_floor_usd: 1.0`).
5. **Evaluated at the 18:00 close run.** −40% is a trigger, not a guaranteed maximum loss (intraday falls and opening gaps can exceed it). Equity = closes × broker positions + cash, compared with the broker's reported equity; differences are logged.

**Tests:** besides the planned cases, property-based tests with `hypothesis` check invariants over thousands of random scenarios: L never exceeds any applicable limit or 4.0, there are never short positions, no approved order exceeds 60% of equity or the daily count, and the kill switch always fires at or beyond −40%.

**Interpretation to confirm in the leverage PR:** below $2,000 the broker's margin limit (1.0 for a cash account) is not applied, because leverage there comes only from leveraged ETFs, not from margin. Otherwise L_broker = 1.0 would make the ETF route impossible.

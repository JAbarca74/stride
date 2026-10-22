# Open spec questions, collected from the PRs

Status: **for the owner to decide**. Each question was left open in a merged PR; the code already does something, described under "Current code". Each has a proposed answer. Accepted answers become a SPEC 1.5 change; anything that changes `config.yaml` parameters or a human-reviewed module (`risk/leverage.py`) is done only after your go-ahead.

Questions about `orders.py` and `ibkr_broker.py` are in [`execution_modules.md`](execution_modules.md) (Q1-Q8), not repeated here. Questions already answered by SPEC 1.2-1.4 (per-ETF fees, ETHU, TLT 1x -> 3x, the five risk-module questions, breaks on different symbols) are left out.

## Main questions

### 1. `vol60`: exact EWMA definition and its config key (#12, SPEC 1.3)

**Current code.** `indicators.ewma_volatility(close, span)`: zero-mean EWMA of squared daily simple returns, weights `(1 - a)^k` with `a = 2 / (span + 1)`, normalized over the available history (pandas `adjust=True`), annualized with `sqrt(252)`, undefined until `span` returns exist. `risk/leverage.ewma_covariance` uses the same weighting, so one asset gives the same number. The span is not in `config.yaml`: the caller passes it to the indicator, and `leverage.py` has `VOL_SPAN = 60` as a module constant, which goes against CLAUDE.md rule 5.

**Proposal.**
- Write the definition into SPEC section 6: *"`vol60`: annualized EWMA volatility of daily simple returns, zero mean, span 60 (a = 2/61, half-life about 21 days), weights normalized over the available history, defined after 60 returns."* Zero mean is the RiskMetrics convention: over 60 days the mean return is noise and subtracting it adds error.
- Add **`risk.vol_span: 60`** to `config.yaml` (under `risk`, because its only consumer is `sigma_60` in the vol-target limit). Adding a key with the value already in use changes no result, so the experiment does not restart. `leverage.py` then reads `risk.vol_span` instead of `VOL_SPAN`: that is a one-line change to a human-reviewed module, so it needs your go-ahead and a line-by-line review.

### 2. Which `N` the 2N stop uses (#13)

**Current code.** Day t's `N`: `close < last_entry - stop_n * N_t`. The pyramid step also uses `N_t`.

**Proposal: the `N` known at the last entry**, stored with `last_entry` in the position state (`entry_n`), for both the stop and the 0.5N pyramid step. This is what the Turtles did: the stop is fixed when a unit is added and moves up only with each new unit. With `N_t`, the stop moves every day for no price reason: a drop in volatility raises it and can stop out a position whose price did not move, and a volatility spike lowers it just when risk is highest. Using `entry_n` reads no data after t, so the anti-look-ahead test is unaffected. This changes `strategy/rules.py` (not human-reviewed) but changes a rule, so it needs a SPEC 1.5 line first.

Related sub-questions from the same PR:
- *Entry price:* keep `last_entry` = **close of the signal day**, not the fill price. That keeps strategy pure and makes backtest, paper and live produce the same signals (needed for H2 and the Phase 4 exit criterion "orders identical to the backtest").
- *Pyramiding after a gap:* keep **one unit per day**. Several units in one go after a gap would buy at the top of a jump; with about 1% commission per order on the real account, fewer and smaller adds are also cheaper.

### 3. Day count for interest (#15)

**Current code.** `backtest/costs.py`: actual/360 per calendar day for both margin interest and interest on idle cash.

**Proposal: keep actual/360 for both.** It is the money-market convention for USD that IBKR applies to interest, and DTB3 is itself quoted on a 360-day discount basis, so rate and accrual use the same convention. Add one line to the SPEC 9 cost table: *"Interest accrues per calendar day, actual/360."* The difference with actual/365 is 1.4% of the interest, well below other uncertainties.

### 4. Margin in `SimBroker` for the Kelly account (#18, SPEC 1.4)

**Current code.** `SimBroker` is cash only (`margin_requirement = 0`), which matches the real account (`allow_margin: false`). The parallel $10,000 Kelly account (SPEC 8, H3) has `allow_margin: true`, so its backtest needs borrowing.

**Proposal: add margin to `SimBroker` as an opt-in, in its own task before T23**, enabled only when the config has `allow_margin: true`:
- cash may go negative down to the borrowing limit `L_borrow x equity` (the same `L_borrow` that `leverage.py` uses);
- negative cash pays DTB3 + 1.5%/yr, actual/360, through the existing `costs.margin_interest`;
- `get_account()` reports `margin_requirement` and excess liquidity so the guard's 25% buffer check is exercised in the backtest. Maintenance requirement: 25% of long ETF value, and 25% times the leverage multiple for leveraged ETFs (FINRA rule 4210 treatment; to verify against IBKR's actual requirement in Phase 4 paper);
- if excess liquidity goes negative the simulator force-sells (sells first, largest positions first) and logs a `margin_call` event, so the backtest shows how often the guard failed to prevent one.

The real-account backtest is unchanged (`allow_margin: false`).

## Smaller questions

| # | PR | Question | Current code | Proposal |
| --- | --- | --- | --- | --- |
| 5 | #15 | Leveraged ETF fees in the cost model | Not charged in `costs.py`: they are inside real ETF prices and inside the synthetic series | Agree. Report them in "Total costs" as an estimate (`expense_ratio x average value held`), labeled as already in the returns, so they are not counted twice |
| 6 | #16 | Sharpe denominator | Standard deviation of excess returns | Keep: it is the standard definition; with daily DTB3 the difference is negligible. Clarify the SPEC 9 formula |
| 7 | #17 | When a deposit enters the TWR | During day t: `r_t = (E_t - F_t) / E_{t-1} - 1` | Keep: a deposit is detected at the run and sits in cash until the next one (SPEC 8), so it earns nothing on day t |
| 8 | #18 | Limit fill price in `SimBroker` | Limit price plus spread | The limit is the final price (a limit never fills worse). Model the spread in the trigger instead: a buy fills only if `low <= limit x (1 - spread)`, a sell only if `high >= limit x (1 + spread)`. More conservative on whether it fills, exact on the price |
| 9 | #14 | Tie-break when two assets add units on the same day and a cap is full | Signal order (`seq`) | Keep a deterministic rule, but use one tied to risk: cut the asset with the higher `vol60` first. Needs a SPEC 6 line; affects only ties |
| 10 | #20 | Correlation window (3 years) and 0.7 flag as constants | Module constants in `reporting/correlations.py` | Move to `config.yaml` under a `reporting:` block. They are reporting parameters, so changing them never restarts the experiment |

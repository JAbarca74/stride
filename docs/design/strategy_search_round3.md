# Pre-registered strategy search, round 3: SPY at 1.5x (owner choice, 2026-10-23)

**Written before this candidate was run on the data.**

## Why

Rounds 1 and 2 (six candidates) all failed. None beat SPY out of sample over 2012–2026. After those results the owner was offered four options:

- A: passive SPY;
- B: risk parity as part of STRIDE;
- C: SPY at 1.5x with drawdown scaling;
- D: a different data set.

The owner chose **C**, aiming at about 20% a year.

This is not a discovered edge. It is a bet that US equities keep rising, amplified 1.5 times. It is still measured with the same protocol, so the risk is visible before any paper or real money.

## Candidate (fixed here)

| Name | Mode | Rule |
| --- | --- | --- |
| `spy_levered` | `hold` | Always hold SPY with weight 1. The weight times **1.5x** becomes the exposure. Above 1x the leveraged-ETF route uses SSO (2x) with the rest in cash. Rebalanced on the first trading day of each month. |

- **Leverage:** 1.5x only (`ROUND_3_LEVERAGE`). Other levels are not tried.
- **Drawdown scaling:** chosen in sample (on or off), as in rounds 1 and 2. When on, it uses the configured `experiments.dd_scale_*` values: full leverage up to −5%, shrinking linearly to 25% of it at −20%.
- **Unchanged safety:** the guard, the order split, the cost-aware resizing and the −40% kill switch all stay as they are.

## Test and pass criteria (unchanged from rounds 1 and 2)

The test is validation section 10: walk-forward with the drawdown scaling chosen in sample, plus a full-period run with scaling on and off.

To pass, all of these must hold:

- out-of-sample Sharpe above SPY's;
- out-of-sample CAGR above SPY's;
- a deflated Sharpe ratio of at least 0.95, counting every trial of all three rounds;
- an out-of-sample maximum drawdown of no more than 40%.

## Expected outcome, written down in advance

- CAGR above SPY's in rising years.
- A Sharpe ratio close to SPY's or below it, because leverage does not improve Sharpe and adds costs.
- A deflated Sharpe ratio well below 0.95.
- Drawdown scaling on: it cuts the 2020 and 2022 losses, but keeps leverage low until equity makes a new high, which costs part of each rebound.
- Drawdown scaling off: a 2008-like or 2020-like crash at 1.5x is close to the −40% kill switch.

**So this candidate is expected to fail the pass criteria.**

## What happens if it fails

The owner may still run it in **paper trading**, as an informed choice, with these limits:

1. Paper only, never live: SPEC 9 and CLAUDE.md rule 6 do not change.
2. Paper trading needs T17 (`run_daily.py`) and the human-reviewed `execution/orders.py`. Neither exists yet, and the owner must approve both explicitly in a session (CLAUDE.md rule 7).
3. After 6 months of paper trading, compare the paper results with this backtest. Real money is a separate decision for the owner. The code must never take it.

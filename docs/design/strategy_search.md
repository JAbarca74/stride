# Pre-registered strategy search (owner request, 2026-10-23)

**Written before any of these strategies was run on the data.** It fixes what is tested and what counts as a pass, so the goal posts cannot move after the results are known.

## Why

The breakout strategy (SPEC 6) failed Phase 3 on real data. Its out-of-sample Sharpe was 0.29, or 0.43 with the leverage chosen in sample (validation section 9). SPY's was 0.83 over 2012–2026. No variant reached the owner's goal of 30% a year within a 25% drawdown. The leverage rotation (Gayed and Bilello) also failed, because 3x exposure trips the −40% kill switch in fast crashes.

The owner asked for a strategy that beats SPY, aiming at 30% a year. Testing ideas until one "wins" on the same data finds luck, not skill. So this search is limited to three published, monthly strategies, fixed in advance, and corrected for the number of trials.

## Candidates (fixed here, not tuned afterwards)

| Mode | Source | Rule, evaluated on the first trading day of each month with data up to that day |
| --- | --- | --- |
| `dual_momentum` | Antonacci, *Dual Momentum Investing* (2014), "GEM" | Pick the better of SPY and EFA by 12-month return. Hold it if that return beats the T-bill rate over the same 12 months; otherwise hold IEF. |
| `relative_strength` | Faber, "Relative Strength Strategies for Investing" (2010) | Rank the 10 ETFs of the universe by 12-month return, skipping the last month. Hold the top 3 with equal weights, each only if it beats the T-bill rate; empty slots go to IEF. |
| `vol_managed` | Moreira and Muir, "Volatility-Managed Portfolios" (2017) | Hold SPY with weight `target_vol / realized volatility` over the last 21 days (annualized), at most 3x. `target_vol` = 20%. |

All three use the existing safety: the guard, the order split, the cost-aware resizing, the −40% kill switch, and the drawdown scaling when it is chosen. Exposure above 1x is reached with the eligible leveraged ETFs (SSO and UPRO for SPY). EFA and IEF have none, so they stay at 1x.

## Test (validation section 10)

For each candidate:

1. **Walk-forward with the leverage chosen in sample:** 1x, 1.5x or 2x, with drawdown scaling on or off. The choice is made from the 5 in-sample years only, as in section 9, and only the out-of-sample years are reported (2012 to 2026).
2. **Deflated Sharpe ratio** (Bailey and López de Prado, 2014) of the out-of-sample returns. The number of trials is every configuration evaluated in sections 7 to 10, so the more variants we look at, the higher the bar.
3. **Full period** at each leverage level, with and without drawdown scaling, for the 30% goal table.

## Pass criteria (all must hold)

- Out-of-sample Sharpe above SPY's over the same days.
- Out-of-sample CAGR above SPY's over the same days.
- Deflated Sharpe ratio of at least 0.95.
- Out-of-sample maximum drawdown no deeper than 40% (the kill switch level).

The 30% goal (CAGR ≥ 30% with max drawdown ≤ 25%) is reported for every run but is not a pass criterion. If no candidate passes, the search ends and the result is reported as it is. No fourth candidate is added afterwards without a new pre-registration.

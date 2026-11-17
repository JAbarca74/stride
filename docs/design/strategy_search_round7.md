# Pre-registered strategy search, round 7: the owner's 25-day mean reversion (2026-11-17)

**Written before these candidates were run on the data.** Exploratory: the 2012-2026 out-of-sample period has already been read by rounds 1 to 6 (15 candidates), so a pass here would still carry selection bias; the deflated Sharpe ratio counts every earlier trial for that reason.

## Why

On 2026-11-08 the owner described a different idea from the dip rule: look at the last 25 days, buy when the price is far below its recent mean, and sell when the rebound takes it back above the mean. On 2026-11-17 the owner asked to try "every possibility to reach about +25% a year". Round 6 (`qqq_core_dip`) reached 24.4% out of sample but failed the deflated Sharpe ratio (0.590 against 0.95). This round swaps only the sleeve signal of the two best earlier candidates for the owner's mean-reversion rule, so the comparison is one change at a time.

## The signal (fixed here)

`mean_reversion`, per sleeve asset, from closes only (no look-ahead: the mean and the standard deviation use the 25 closes before day t, not day t itself):

- **in** when the close is at least 2.0 standard deviations below the mean of the prior 25 closes;
- **out** when the close is at or above the mean of the prior 25 closes;
- no stop, no take-profit target, no trend filter;
- full share, no Kelly (as the dip rule).

New experiment keys, defaults equal to the values above: `allocation.mr_window: 25`, `allocation.mr_entry_z: 2.0`. Changing nothing in the running paper instances (their signals are `dip`).

## Candidates (fixed here)

| Name | Same as | Only change |
| --- | --- | --- |
| `crypto_mr` | round 5 `crypto_dip`: 80% SPY core, 20% sleeve split IBIT / ETHA, daily rebalance, drawdown scaling as in round 5 | sleeve signal `mean_reversion` |
| `qqq_core_mr` | round 6 `qqq_core_dip`: QQQ core 0.80 x 1.5, 20% crypto sleeve, daily rebalance, 252-day drawdown window | sleeve signal `mean_reversion` |

Leverage level 1x only for both (the core leverage of `qqq_core_mr` comes from `core_leverage`, as in round 6). Nothing else is tuned; no other window or threshold is tried.

## Test and pass criteria (unchanged)

The test is validation section 10. The deflated Sharpe ratio counts every trial of rounds 1 to 7. To pass, a candidate's out-of-sample results must meet all four:

- a Sharpe ratio above SPY's;
- a CAGR above SPY's;
- a deflated Sharpe ratio of at least 0.95;
- a maximum drawdown no deeper than 40%.

## Expected outcome, written down in advance

- A 2-sigma drop below a 25-day mean happens far more often than a 25% drop below a 90-day high, so the sleeve trades more often, holds for days rather than months, and misses most of the large crypto rallies that made `crypto_dip` look good (it sells at the mean, before the trend). Expected: a lower CAGR than the dip versions, about 13-16% for `crypto_mr` and 18-23% for `qqq_core_mr`, with a similar or slightly lower maximum drawdown.
- Expected to **fail** the deflated Sharpe ratio, like every earlier round: with 17 trials the bar is high, and the change does not add a new source of return.
- If either candidate passed, it would still be exploratory and would need its own paper instance and the owner's approval before any use.

## Result (local prices to 2026-10-23, code at commit 2251e48, run 2026-11-17): FAIL, both

Validation section 10, 2012-2026 walk-forward, out of sample only:

| | OOS CAGR | OOS Sharpe | OOS max drawdown | Deflated Sharpe | Pass |
| --- | --- | --- | --- | --- | --- |
| SPY | 14.9% | 0.825 | - | - | - |
| `crypto_dip` (round 5, same run) | 22.1% | 1.286 | 28.8% | 0.832 | FAIL |
| `qqq_core_dip` (round 6, same run) | 26.0% | 1.140 | 37.1% | 0.611 | FAIL |
| `crypto_mr` | **7.9%** | 0.535 | 34.8% | 0.036 | FAIL |
| `qqq_core_mr` | **15.6%** | 0.791 | 31.6% | 0.159 | FAIL |

Full period from 2007 with the drawdown scaling on: `crypto_mr` 4.8% a year (Sharpe 0.35, max drawdown 28.4%), `qqq_core_mr` 13.8% (Sharpe 0.74, max drawdown 34.3%). Without the scaling, 2008 trips the kill switch for both, as for the dip versions.

- Both fail every criterion except the maximum drawdown; `qqq_core_mr` beats SPY's CAGR only by 0.7 points and not on Sharpe.
- The expectation written above was too optimistic (13-16% and 18-23%): selling at the 25-day mean exits the crypto rallies far earlier than the dip rule's trailing exit, and the 2-sigma entries in 2018 and 2022 kept buying into falling markets (2018 -15.7% and -16.0%, 2022 -17.7% and -22.2% for the two candidates).
- The deflated Sharpe ratio now counts 17 candidates; the round 6 figure for `qqq_core_dip` moved from 0.590 to 0.611 with the newer data, still far from 0.95.

**Conclusion.** The owner's 25-day mean-reversion idea, as pre-registered, is worse than the dip rule on the same portfolios. The best measured portfolio is still `qqq_core_dip` (paper instance B): about 22-26% a year in these tests, with drawdowns of 34-37% and a deflated Sharpe ratio that does not support it statistically. No new paper instance is proposed.

## Exploratory, not pre-registered (2026-11-17)

Measured on the same local data with `run_daily --mode backtest` (2007-2026, deposits as each instance), labelled exploratory because the period was already read:

| Variant | CAGR | Sharpe | Max drawdown | Note |
| --- | --- | --- | --- | --- |
| B as it runs (`config.paper_b.yaml`) | 22.7% | 1.08 | 34.3% | reference |
| B with the T23 Kelly leverage and margin (10,000 USD) | -1.2% | -0.15 | 40.9% | kill switch fired |
| DIP as it runs | 13.6% | 0.93 | 28.4% | reference |
| DIP with the T23 Kelly leverage and margin (the Kelly paper account) | 18.4% | 0.81 | 31.1% | 16.7x turnover a year |

More leverage on the already levered B portfolio ends in the kill switch. On the unlevered DIP portfolio, full Kelly adds return with more volatility and costs; that is what the Kelly paper account (H3) now measures forward.

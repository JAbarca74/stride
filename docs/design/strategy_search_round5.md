# Pre-registered strategy search, round 5: a 20% crypto sleeve (owner choice, 2026-10-23)

**Written before these candidates were run on the data.**

## Why

The owner asked to put 20% of the account in crypto, not only Bitcoin, sized with Kelly. They also proposed buying only after a deep fall, holding through further falls, and selling no earlier than about 30% above what was invested. Past that point they asked to keep holding while the trend points up, in case the gain reaches 40% or 50%. Round 4 (SPY and QQQ at 1.5x or 2x) is being measured at the same time. This round does not depend on its result.

## Universe

The crypto sleeve holds **IBIT (Bitcoin) and ETHA (Ethereum)**. Before these ETFs existed, their prices are spliced with Coinbase spot prices: Bitcoin from 2015 and Ethereum from 2016. Other coins (Solana, XRP) are left out: their price history is too short (Solana from 2020, and −96% in 2022) and they have no ETF with years of history.

**Hindsight warning:** Bitcoin went from about $300 in 2015 to over $100,000. Any rule that holds it looks good for that reason alone. Every candidate is therefore compared with simply holding the same sleeve (`crypto_hold`). The deflated Sharpe ratio counts every trial of rounds 1 to 5.

## Candidates (fixed here)

Every candidate holds **80% in SPY** (the core) and a **20% crypto sleeve**, split equally between IBIT and ETHA (10% each at most). There is no leverage (1x only). A coin whose signal is off leaves its part of the sleeve in cash, which earns interest.

| Name | Signal | Sizing | Rebalance |
| --- | --- | --- | --- |
| `crypto_hold` | Always in (the benchmark for the sleeve) | Full share | Monthly |
| `crypto_trend` | In while the close is above its 200-day average | Half Kelly | Daily |
| `crypto_breakout` | Turtle 55/20 on closes: in after a close above the prior 55-day high, out after a close below the prior 20-day low | Half Kelly | Daily |
| `crypto_range` | In after a close at or below the prior 20-day low, out after a close at or above the prior 20-day high | Half Kelly | Daily |
| `crypto_dip` (owner idea) | In once the close is **25% below the highest close of the prior 90 days**. The target is a close **30% above the close it was bought at** (the owner's example: 10,000 invested, falling to 7,000, sold no earlier than 13,000). Once the target is reached it keeps holding while the rise goes on, and sells on the first close below the lowest close of the prior 20 days. No stop-loss before the target. | Full share | Daily |

**Half Kelly:** each coin's weight is its part of the sleeve times `0.5 × (annual excess return / annual variance)`, both estimated from its last 504 daily returns (2 years) and clipped to [0, 1].

The dip candidate uses the full share, with no Kelly. After a crash the trailing two-year return is often negative, so Kelly would be 0 and the rule would never buy the dips it is meant to buy.

The signal state is computed from the last 705 days of closes, which is the Kelly window plus the 200-day average. A position opened before that window is treated as the window's own history implies.

Every other part of the engine is unchanged: the guard, the order split, the cost-aware resizing and the −40% kill switch.

## Test and pass criteria (unchanged)

The test is validation section 10: walk-forward with the drawdown scaling chosen in the 5 in-sample years, plus full-period runs with and without scaling.

To pass, all four must hold out of sample:

- a Sharpe ratio above SPY's;
- a CAGR above SPY's;
- a deflated Sharpe ratio of at least 0.95;
- a maximum drawdown no deeper than 40%.

The results should also be compared with `crypto_hold`, though that comparison is not a pass criterion.

## Expected outcome, written down in advance

- `crypto_hold`:
  - a CAGR above SPY's from 2016 on, because Bitcoin went up so much;
  - deeper drawdowns in 2018 and 2022;
  - a Sharpe ratio near SPY's.
- `crypto_trend` and `crypto_breakout`: sleeve drawdowns cut from about −80% to about −30%, so the total drawdown stays close to SPY's. They have the best chance of passing on Sharpe. The deflated Sharpe ratio is the open question.
- `crypto_range`: likely to fail. It buys every new low in 2018 and 2022.
- `crypto_dip`:
  - fewer, larger wins, of +30% or more;
  - in 2018 and 2022 it buys at −25% and then holds through falls of −70% to −80% for a year or more before a +30% exit, because it has no stop;
  - expected to fail on Sharpe.
- Before about 2017 the sleeve stays mostly in cash, waiting for the 504 days of history Kelly needs (crypto data starts in 2015 and 2016). This costs some return in the early out-of-sample years.

## Result (prices to 2026-10-23, commit 4c7eaeb)

**The owner's own rule, `crypto_dip`, is the best result of the five rounds, and the closest to passing.** It held 80% SPY plus a 20% crypto sleeve, with the dip rule and no leverage.

| Strategy | OOS CAGR | OOS Sharpe | OOS max drawdown | Deflated Sharpe | Pre-registered pass |
| --- | --- | --- | --- | --- | --- |
| SPY | 14.9% | 0.825 | — | — | — |
| `crypto_hold` | 18.6% | 0.961 | 34.9% | 0.472 | FAIL |
| `crypto_trend` | 13.0% | 0.888 | 26.0% | 0.419 | FAIL |
| `crypto_breakout` | 14.2% | 0.992 | 25.9% | 0.578 | FAIL |
| `crypto_range` | 6.5% | 0.436 | 27.6% | 0.029 | FAIL |
| `crypto_dip` | **22.4%** | **1.304** | **28.8%** | **0.905** | FAIL (deflated Sharpe only) |

`crypto_dip` met three of the four pass criteria:

- its Sharpe ratio was above SPY's (1.304 against 0.825);
- its CAGR was above SPY's (22.4% against 14.9%);
- its maximum drawdown was 28.8%, within the 40% limit.

It missed only the deflated Sharpe ratio: 0.905 against 0.95, after 14 candidates and every configuration of sections 7 to 10. Over the full period from 2007, with drawdown scaling on, it returned 13.7% a year with a Sharpe of 0.935 and a 28.4% maximum drawdown. That is the best continuous-account result of all rounds. Without scaling, every round 5 run tripped the −40% kill switch in 2008, before crypto data exists, because the 80% SPY core fell that far on its own.

The pre-registered expectation ("expected to fail on Sharpe") was wrong for `crypto_dip`. Range trading failed, as expected.

Caveats the owner should keep in view:

- **Bubble years drive much of the gain.** 2017 alone returned +194% out of sample (Ethereum went from about $8 to about $750), and 2020–2021 added more. Without another crypto bubble the edge may be much smaller.
- **Hindsight:** crypto was chosen knowing it rose a lot. Early Ethereum prices from 2016 to 2017 come from a small, young market.
- **The dip rule's parameters were set by the owner's intuition,** and changed twice before the run (a 5% target, then 30%, then holding past the target). No one has yet checked whether nearby values work as well, or whether 25%/90/30% is a lucky peak.

Proposed next step (needs the owner's OK): a pre-registered robustness check of `crypto_dip` alone. It would vary each parameter by ±20%, run the sleeve with 2017 excluded, and try other start years. Only if the result holds across these would the rule go to paper trading.

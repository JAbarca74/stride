# Pre-registered strategy search, round 6: option B, QQQ core at 1.5x plus the dip sleeve (owner choice, 2026-10-24)

**Written before this candidate was run on the data.**

## Why

Two earlier candidates each did well on one side:

| Round | Candidate | Portfolio | OOS CAGR | OOS Sharpe |
| --- | --- | --- | --- | --- |
| 4 | `qqq_levered_12m` | QQQ held at 1.5x or 2x, with the 12-month drawdown window | 21.0% | 0.823 |
| 5 | `crypto_dip` | 80% SPY plus a 20% crypto sleeve using the owner's dip rule | 22.4% | 1.30 |

The owner chose to test option B, which combines them, aiming at the original goal of about 30% a year.

## Candidate (fixed here)

The candidate is `qqq_core_dip`, built on the `sleeve` mode:

- **Core:** QQQ, with weight 0.80 × **1.5** (new key `allocation.core_leverage`). That is 120% of equity in QQQ exposure.
- **Sleeve:** 20% crypto, split equally between IBIT and ETHA, using the round 5 dip rule unchanged:
  - buy once the close is 25% below the 90-day high;
  - no stop;
  - a target of +30% over the buy close;
  - after that, sell on the first close below the prior 20-day low;
  - full share, no Kelly.
- **Rebalancing:** daily.
- **Drawdown scaling:** on or off, chosen in sample. When on, the window is 252 days, as in round 4.
- **Leverage level:** 1x only. The 1.5x is applied to the core alone, through `core_leverage`.

How the leverage is reached (SPEC 7, unchanged `choose_instruments`):

- With crypto held, the total exposure is 1.4 × equity, at or above `enter_k` 1.25. The leveraged route is then used:
  - QQQ is held through QLD (2x);
  - the crypto is held through BITX and ETHU (2x) for the same exposure.
- With crypto out, the exposure is 1.2 × equity, between `exit_k` and `enter_k`. Each instrument is then kept as it is.
- If no leveraged instrument is held yet, 1.2x does not fit in the cash, so the exposure is scaled down to fit (about 1x).

The kill switch stays at −40%. The QQQ hindsight warning of round 4 still applies.

## Test and pass criteria (unchanged)

The test is validation section 10. The deflated Sharpe ratio counts every trial of rounds 1 to 6.

To pass, the candidate's out-of-sample results must meet all four of these:

- a Sharpe ratio above SPY's;
- a CAGR above SPY's;
- a deflated Sharpe ratio of at least 0.95;
- a maximum drawdown no deeper than 40%.

## Expected outcome, written down in advance

- **Out of sample:**
  - CAGR of about 25–32%, the highest of all rounds;
  - Sharpe of about 1.0–1.2, between rounds 4 and 5;
  - maximum drawdown of about 35–40%, with 2022 the risk, when QQQ and crypto fell together.
- **Full period from 2007:** 2008 at 1.2x QQQ probably trips the kill switch unless the scaling is on. With scaling on, roughly 10–14% a year.
- **Deflated Sharpe ratio:** probably below 0.95, because each round raises the bar.

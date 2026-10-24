# Pre-registered strategy search, round 2 (owner request, 2026-10-23)

**Written before any of these strategies was run on the data.** Same rules as round 1 (`docs/design/strategy_search.md`): what is tested and what counts as a pass are fixed here and do not move afterwards.

## Why a second round

Round 1 failed on real data (prices to 2026-10-22, validation section 10). Out-of-sample over 2012–2026:

| Strategy | Sharpe | CAGR | Max drawdown | Deflated Sharpe |
| --- | --- | --- | --- | --- |
| SPY | 0.827 | 15.0% | — | — |
| dual_momentum | 0.435 | 7.2% | 37.5% | 0.105 |
| relative_strength | 0.431 | 9.6% | 65.6% | 0.085 |
| vol_managed | 0.625 | 13.5% | 38.3% | 0.254 |

Two lessons shape this round:

1. The universe has almost no equities. Most of what it holds (metals, commodities, the dollar, bonds) lost to SPY over the period.
2. Leverage above 2x, or 3x reached by a volatility weight, trips the −40% kill switch in a fast crash and ends the run. In the full-period runs every version without drawdown scaling ended with a negative CAGR.

The owner's goal is about 20% a year. This round adds equities and caps leverage at 1.5x. It does not raise it.

## Hindsight warning

QQQ returned about 18% a year over 2012–2026. Picking it now, knowing that, is hindsight bias. Any candidate that holds it will look good for that reason alone. The deflated Sharpe ratio, which counts every trial of rounds 1 and 2, is the guard against this. Passing requires beating SPY out of sample, not only matching QQQ's luck.

## Search universe (loaded for the search only, never traded by the configured run)

`experiments.search_universe`: QQQ, IWM, XLK, XLV, XLF, XLE, XLI. EEM and EFA already have data. `experiments.search_leveraged_etfs`: QQQ → QLD (2x), TQQQ (3x). These pass the SPEC 7 filters only once `reference/etf_facts.yaml` has their issuer facts; until then QQQ stays at 1x.

## Candidates (fixed here, not tuned afterwards)

Every candidate is evaluated on the first trading day of each month, with data up to that day.

| Name | Mode | Rule |
| --- | --- | --- |
| `sector_momentum` | `relative_strength` (Faber 2010 on equities) | Rank SPY, QQQ, IWM, EFA, EEM, XLK, XLV, XLF, XLE and XLI by 12-month return, skipping the last month. Hold the top 3 with equal weights, each only if it beats T-bills over the same span; empty slots go to IEF. |
| `momentum_vol` | dual momentum with a volatility weight (Antonacci, then Moreira and Muir) | Pick the best of SPY, QQQ, IWM and EFA by 12-month return. If it beats T-bills, hold it with weight `0.20 / realized volatility` over 63 days, at most 2.0; otherwise hold IEF at 1.0. |
| `risk_parity` | inverse-volatility weights with a volatility target | Weights in SPY, QQQ, TLT and GLD proportional to 1 / volatility over 63 days. The whole portfolio is scaled so its realized volatility over the same 63 days is 15%, with a total weight of at most 2.0. |

The volatility window is 63 days, not 21: round 1 showed that one month of calm data can push the weight to its cap right before a crash.

## Test (validation section 10, after round 1)

Same as round 1, with one change: **leverage levels 1x and 1.5x only** (`ROUND_2_MAX_LEVERAGE`).

1. Walk-forward with the leverage and drawdown scaling chosen in the 5 in-sample years; only the out-of-sample years (2012 to 2026) are reported.
2. Deflated Sharpe ratio of the out-of-sample returns. The trials are every configuration of sections 7 to 10, including all of round 1.
3. Full period at each level, with and without drawdown scaling.

## Pass criteria (all must hold; unchanged)

- Out-of-sample Sharpe above SPY's over the same days.
- Out-of-sample CAGR above SPY's over the same days.
- Deflated Sharpe ratio of at least 0.95.
- Out-of-sample maximum drawdown no deeper than 40%.

The owner's target, about 20% CAGR, is reported for every run but is not a pass criterion. If no candidate passes, round 2 ends and the result is reported as it is. A third round needs its own pre-registration.

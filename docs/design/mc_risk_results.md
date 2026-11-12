# Risk allocation, gate R2: the daily walk-forward

Phase 4 of `docs/design/mc_risk_allocation.md`. Run on 2026-11-12 with `scripts/research/mc_risk_evaluate.py` on the daily research database (20 assets, 2007-01-03 to 2026-11-11). Outputs: `docs/results/mc_risk/`.

Every figure is a measurement on past data with simulated fills. None is an expectation.

## 1. Verdict

**Gate R2 is met in both set-ups, by the criteria fixed in the design.** What that is worth differs a lot between the two, so both are stated.

| Set-up | Selected on 2008-2016 | Selection | Holdout (2017 to 2026-11) | Gate |
| --- | --- | --- | --- | --- |
| `U20`, 20 assets, 10,000 USD | `IV_trend` | +35.5%, max drawdown 8.8% | +63.9%, max drawdown 9.3% | met |
| `S6`, 6 assets, 1,000 USD | `IV_dd` | +73.6%, max drawdown 7.6% | +8.4%, max drawdown 15.4% | met |

- **`U20` / `IV_trend` is the result that holds up.** Inverse-volatility weights with the 200-day trend brake: positive in both periods (3.4% and 5.2% a year), drawdown under 10%, no kill switch, through 2008, 2020 and 2022. Its four neighbours are positive in the holdout.
- **`S6` / `IV_dd` passes the letter of the gate and is not a success.** Its drawdown brake reached the 15% floor on 2022-09-27 and the account has been in cash since: +8.4% in 9.8 years. The gate had no criterion against that. Selecting on 2008-2016 did not pick a good variant for 2017 onward in this set-up (section 4).
- **It is not more profit than being invested in the recent years.** From 2023-01 to 2026-11 `U20` / `IV_trend` made +24.7% (max drawdown 7.9%). In the same years the current `paper_mc` rule made +32.3% in its backtest (17.5%) and an equal-weight buy-and-hold of the universe +54.4% (11.2%) (`docs/design/mc_backtest_results.md`; different capital and bar size, so the comparison is rough). What this approach adds is that it kept making money through 2008, 2020 and 2022 without getting near the kill switch; the unbraked variants did not.
- **The brake does the work, not the Monte Carlo simulation.** Sizing with a plain 60-day volatility gave the same results as sizing with the simulated one, and removing the simulated loss cap changed nothing for the selected variants (section 5).

## 2. What was run

| Item | Value |
| --- | --- |
| Variants | 3 allocators (`EW`, `IV`, `ERC`) x 3 brakes (`none`, `trend`, `dd`), as pre-registered |
| Set-ups | `U20`: the 20 assets, 10,000 USD. `S6`: QQQ, EFA, TLT, GLD, USO, VNQ (the lowest measured spread of each class), 1,000 USD |
| Periods | selection 2008-01-01 to 2016-12-31; holdout 2017-01-01 to 2026-11-11; each run starts in cash |
| Simulation | the unchanged simulator, no direction, 252-session window, 2,000 joint paths, every session |
| Weights | recomputed on the first session of each month from the 20-session simulated risk; as-of universe |
| Limits | 25% per asset; 40% QQQ + IWM, 35% GLD + SLV + CPER, 30% IBIT + ETHA; no leverage; simulated 5-session loss (1% tail) at most 8%; minimum order 150 USD; rebalance band 20%; 12 orders a day |
| Between rebalances | only reductions (loss cap, drawdown brake) and orders the daily limit pushed to the next day |
| Fills | next session open, spread and IBKR Pro Tiered commission of `config.yaml` |
| Kill switch | 20% from the peak at a close: sell everything, stop for good |
| Cash | earns nothing |
| Seed | 20261112 |

Selection rule and gate: the docstring of the script and section 6 of the design. Costs and the reference of check 4 are read on the holdout; checks 1 and 2 on both periods.

**Settled before the full run** (the first is recorded in the design, section 11; the second was found in a smoke test on four months of 2008 and fixed before any full result existed):

- A weight removed by a cap, the minimum order or the trend brake stays in cash.
- The minimum order applies to the monthly targets and to each order. A brake that shrinks a held position below 150 USD does not sell the whole position. The first version did, and at 1,000 USD a 2% drawdown emptied the account.

## 3. `U20`: 20 assets, 10,000 USD

| variant | period | return | CAGR | vol | max DD | return / DD | trades | costs / year | avg exposure | kill switch |
|---|---|---|---|---|---|---|---|---|---|---|
| EW_none | selection | -16.9% | -2.0% | 5.6% | 21.5% | -0.79 | 50 | 0.03% | 8% | FIRED 2008-10-03 |
| EW_trend | selection | +31.5% | +3.1% | 8.3% | 12.2% | 2.59 | 339 | 0.27% | 59% | no |
| EW_dd | selection | -11.7% | -1.4% | 3.6% | 15.6% | -0.75 | 103 | 0.06% | 6% | no |
| IV_none | selection | -17.7% | -2.1% | 4.3% | 21.5% | -0.82 | 49 | 0.04% | 8% | FIRED 2008-10-08 |
| IV_trend | selection | +35.5% | +3.4% | 7.0% | 8.8% | 4.02 | 406 | 0.31% | 60% | no |
| IV_dd | selection | -12.5% | -1.5% | 3.4% | 15.3% | -0.82 | 103 | 0.06% | 7% | no |
| ERC_none | selection | +20.6% | +2.1% | 5.2% | 12.3% | 1.68 | 273 | 0.15% | 85% | no |
| ERC_trend | selection | +22.7% | +2.3% | 4.6% | 6.5% | 3.47 | 368 | 0.27% | 51% | no |
| ERC_dd | selection | +7.8% | +0.8% | 4.1% | 9.9% | 0.79 | 280 | 0.16% | 67% | no |
| EW_none | holdout | -3.3% | -0.3% | 6.1% | 21.1% | -0.15 | 69 | 0.04% | 32% | FIRED 2020-03-17 |
| EW_trend | holdout | +60.0% | +4.9% | 8.0% | 15.7% | 3.83 | 446 | 0.31% | 65% | no |
| EW_dd | holdout | +33.3% | +3.0% | 7.3% | 14.3% | 2.33 | 387 | 0.18% | 62% | no |
| IV_none | holdout | +104.1% | +7.5% | 8.7% | 18.5% | 5.61 | 424 | 0.21% | 96% | no |
| IV_trend | holdout | +63.9% | +5.1% | 5.9% | 9.3% | 6.84 | 556 | 0.36% | 65% | no |
| IV_dd | holdout | +52.0% | +4.3% | 6.3% | 13.4% | 3.88 | 531 | 0.26% | 72% | no |
| ERC_none | holdout | +48.9% | +4.1% | 4.9% | 12.2% | 4.01 | 313 | 0.15% | 78% | no |
| ERC_trend | holdout | +33.7% | +3.0% | 3.6% | 5.7% | 5.86 | 484 | 0.29% | 53% | no |
| ERC_dd | holdout | +40.9% | +3.5% | 4.1% | 10.2% | 4.00 | 327 | 0.16% | 68% | no |

Year by year (the first year of each period starts in cash):

| year | EW_none | IV_none | ERC_none | EW_trend | IV_trend | ERC_trend | IV_dd |
|---|---|---|---|---|---|---|---|
| 2008 | -16.9% | -17.7% | +1.4% | -4.2% | +0.2% | +7.1% | -12.5% |
| 2009 | +0.0% | +0.0% | +0.7% | +15.6% | +10.5% | -0.4% | +0.0% |
| 2010 | +0.0% | +0.0% | +8.3% | +7.0% | +5.8% | +3.3% | +0.0% |
| 2011 | +0.0% | +0.0% | +1.9% | +2.2% | +3.3% | +4.0% | +0.0% |
| 2012 | +0.0% | +0.0% | +2.8% | -3.3% | -1.8% | -1.5% | +0.0% |
| 2013 | +0.0% | +0.0% | +0.1% | +4.3% | +5.8% | +0.3% | +0.0% |
| 2014 | +0.0% | +0.0% | +5.4% | +4.8% | +5.7% | +5.7% | +0.0% |
| 2015 | +0.0% | +0.0% | -7.4% | -2.0% | -1.6% | -1.5% | +0.0% |
| 2016 | +0.0% | +0.0% | +6.7% | +4.8% | +3.7% | +3.9% | +0.0% |
| 2017 | +10.8% | +11.1% | +4.7% | +8.5% | +8.7% | +3.9% | +10.7% |
| 2018 | -5.3% | -3.0% | -0.5% | -3.9% | -2.7% | -1.2% | -3.3% |
| 2019 | +14.9% | +13.7% | +8.4% | +3.4% | +5.1% | +4.9% | +10.3% |
| 2020 | -19.7% | +1.5% | +2.5% | +4.8% | +7.0% | +5.9% | -6.4% |
| 2021 | +0.0% | +15.5% | +4.5% | +14.9% | +13.5% | +4.9% | +8.7% |
| 2022 | +0.0% | -3.7% | -4.0% | -0.7% | -2.6% | -1.2% | -4.3% |
| 2023 | +0.0% | +7.6% | +3.9% | -2.1% | -0.2% | -0.7% | +3.8% |
| 2024 | +0.0% | +11.5% | +8.7% | +6.7% | +8.1% | +6.0% | +8.9% |
| 2025 | +0.0% | +12.7% | +6.8% | +9.8% | +6.9% | +1.0% | +9.0% |
| 2026 | +0.0% | +8.9% | +6.2% | +8.2% | +8.1% | +6.4% | +7.4% |

Reading:

- **Without a brake, holding the universe dies in 2008.** `EW_none` and `IV_none` fired the kill switch in October 2008, and `EW_none` again in March 2020. The simulated loss cap did not prevent it, as gate R1 anticipated.
- **The drawdown brake avoids the kill switch by stopping first.** `EW_dd` and `IV_dd` reached their 15% floor in October and November 2008 and stayed in cash for the remaining eight years of the period (-11.7% and -12.5%).
- **All three trend variants are positive in both periods**, with drawdowns of 5.7% to 15.7% and an average exposure of 51% to 65%.
- `IV_none` has the highest holdout return (+104.1%) and was killed in the selection period: the same rule, two periods, opposite outcomes.

## 4. `S6`: 6 assets, 1,000 USD

| variant | period | return | CAGR | vol | max DD | return / DD | trades | costs / year | avg exposure | kill switch |
|---|---|---|---|---|---|---|---|---|---|---|
| EW_none | selection | -15.2% | -1.8% | 4.6% | 21.9% | -0.69 | 12 | 0.06% | 8% | FIRED 2008-10-08 |
| EW_trend | selection | +34.2% | +3.3% | 9.2% | 12.0% | 2.86 | 110 | 0.56% | 68% | no |
| EW_dd | selection | -7.8% | -0.9% | 3.9% | 15.3% | -0.51 | 16 | 0.07% | 8% | no |
| IV_none | selection | +79.6% | +6.7% | 8.4% | 11.2% | 7.13 | 45 | 0.22% | 86% | no |
| IV_trend | selection | +47.9% | +4.4% | 7.4% | 10.0% | 4.78 | 99 | 0.52% | 67% | no |
| IV_dd | selection | +73.6% | +6.3% | 7.4% | 7.6% | 9.66 | 36 | 0.18% | 73% | no |
| ERC_none | selection | +51.5% | +4.7% | 6.1% | 11.0% | 4.69 | 59 | 0.29% | 66% | no |
| ERC_trend | selection | +29.7% | +2.9% | 5.2% | 10.0% | 2.96 | 85 | 0.44% | 47% | no |
| ERC_dd | selection | +48.6% | +4.5% | 5.4% | 7.0% | 6.95 | 42 | 0.21% | 56% | no |
| EW_none | holdout | +7.1% | +0.7% | 6.3% | 22.7% | 0.31 | 14 | 0.06% | 32% | FIRED 2020-03-19 |
| EW_trend | holdout | +78.0% | +6.0% | 8.9% | 15.0% | 5.19 | 128 | 0.60% | 72% | no |
| EW_dd | holdout | +5.9% | +0.6% | 5.2% | 15.1% | 0.39 | 20 | 0.09% | 26% | no |
| IV_none | holdout | +20.0% | +1.9% | 6.1% | 20.9% | 0.96 | 41 | 0.18% | 44% | FIRED 2022-09-27 |
| IV_trend | holdout | +60.2% | +4.9% | 7.4% | 12.2% | 4.92 | 125 | 0.59% | 64% | no |
| IV_dd | holdout | +8.4% | +0.8% | 4.4% | 15.4% | 0.55 | 33 | 0.15% | 32% | no |
| ERC_none | holdout | +116.7% | +8.2% | 8.0% | 19.3% | 6.05 | 68 | 0.31% | 78% | no |
| ERC_trend | holdout | +46.0% | +3.9% | 7.1% | 11.5% | 4.01 | 138 | 0.65% | 60% | no |
| ERC_dd | holdout | +44.4% | +3.8% | 5.1% | 13.0% | 3.41 | 42 | 0.19% | 50% | no |

Year by year:

| year | EW_none | IV_none | ERC_none | EW_trend | IV_trend | ERC_trend | IV_dd |
|---|---|---|---|---|---|---|---|
| 2008 | -15.2% | -0.5% | +10.5% | +0.5% | +4.0% | +8.6% | +1.3% |
| 2009 | +0.0% | +9.3% | +1.1% | +13.4% | +5.8% | +0.2% | +10.5% |
| 2010 | +0.0% | +16.6% | +5.5% | +7.7% | +11.4% | +3.9% | +14.4% |
| 2011 | +0.0% | +10.6% | +11.3% | +3.8% | +10.8% | +7.9% | +11.3% |
| 2012 | +0.0% | +9.8% | +5.0% | +1.8% | +2.4% | +3.6% | +5.0% |
| 2013 | +0.0% | +4.5% | +2.3% | +4.4% | +4.9% | +3.5% | +4.1% |
| 2014 | +0.0% | +9.4% | +8.6% | +7.3% | +6.8% | +6.0% | +7.8% |
| 2015 | +0.0% | -1.2% | -4.4% | -5.3% | -5.8% | -5.7% | -0.8% |
| 2016 | +0.0% | +3.3% | +3.6% | -2.5% | +0.9% | -0.8% | +4.1% |
| 2017 | +13.8% | +12.3% | +15.2% | +9.3% | +9.8% | +11.6% | +12.3% |
| 2018 | -7.0% | -3.4% | -3.4% | -2.8% | -6.1% | -6.0% | -1.7% |
| 2019 | +25.9% | +11.1% | +11.4% | +8.0% | +13.3% | +11.2% | +9.8% |
| 2020 | -19.7% | +14.3% | +17.4% | +6.3% | +15.3% | +17.1% | -0.7% |
| 2021 | +0.0% | +8.3% | +4.6% | +16.8% | +9.7% | +1.5% | +6.5% |
| 2022 | +0.0% | -19.5% | -14.2% | -8.0% | -9.7% | -5.2% | -15.4% |
| 2023 | +0.0% | +0.0% | +10.4% | +3.1% | +3.4% | +0.5% | +0.0% |
| 2024 | +0.0% | +0.0% | +9.4% | +5.1% | +4.0% | +3.5% | +0.0% |
| 2025 | +0.0% | +0.0% | +19.4% | +9.8% | +8.8% | +6.2% | +0.0% |
| 2026 | +0.0% | +0.0% | +15.0% | +14.2% | +2.7% | +0.4% | +0.0% |

Reading:

- The variant selected on 2008-2016 (`IV_dd`, best return over drawdown: 9.66) had 0.55 in the holdout. The rank correlation between the two periods across the 9 variants is 0.15 (0.47 in `U20`): in this set-up the selection period says little about the next one.
- 2022 is what hurts here: `IV_none` fired the kill switch on 2022-09-27, the same day `IV_dd` reached its floor, and both lost 15% to 20% that year.
- The three trend variants are again positive in both periods (+29.7% to +78.0%), but none of them was the selected one, so that is an observation, not a result of the gate.
- Costs are 0.4% to 0.7% of capital a year for the trend variants at this size, against 0.3% to 0.4% in `U20`.

## 5. The pre-registered extras

**Sizing with a plain 60-day volatility instead of the simulated one** (`IV60_none` against `IV_none`):

| set-up | period | variant | return | max DD | kill switch |
|---|---|---|---|---|---|
| U20 | selection | IV_none | -17.7% | 21.5% | FIRED 2008-10-08 |
| U20 | selection | IV60_none | -21.4% | 24.3% | FIRED 2008-10-10 |
| U20 | holdout | IV_none | +104.1% | 18.5% | no |
| U20 | holdout | IV60_none | +110.5% | 19.1% | no |
| S6 | selection | IV_none | +79.6% | 11.2% | no |
| S6 | selection | IV60_none | +78.4% | 9.7% | no |
| S6 | holdout | IV_none | +20.0% | 20.9% | FIRED 2022-09-27 |
| S6 | holdout | IV60_none | +19.6% | 20.6% | FIRED 2022-09-21 |

The two behave the same: the same kill switches within days of each other, returns within six points or less. On this evidence the simulated volatility adds nothing to a trailing one for sizing.

**Without the portfolio loss cap** (`_nocap`), for the selected variants and for `IV_none`:

| set-up | period | variant | return | max DD | kill switch |
|---|---|---|---|---|---|
| U20 | selection | IV_trend | +35.5% | 8.8% | no |
| U20 | selection | IV_trend_nocap | +35.5% | 8.8% | no |
| U20 | holdout | IV_trend | +63.9% | 9.3% | no |
| U20 | holdout | IV_trend_nocap | +63.9% | 9.3% | no |
| S6 | selection | IV_dd | +73.6% | 7.6% | no |
| S6 | selection | IV_dd_nocap | +73.6% | 7.6% | no |
| S6 | holdout | IV_dd | +8.4% | 15.4% | no |
| S6 | holdout | IV_dd_nocap | +8.4% | 15.4% | no |
| U20 | selection | IV_none | -17.7% | 21.5% | FIRED 2008-10-08 |
| U20 | selection | IV_none_nocap | -17.7% | 21.5% | FIRED 2008-10-08 |
| U20 | holdout | IV_none | +104.1% | 18.5% | no |
| U20 | holdout | IV_none_nocap | +112.5% | 19.0% | no |

The cap never bound for the selected variants (identical results). Where it did bind (`IV_none` in `U20`, 3.9% of the holdout sessions) it cost 8 points of return over the period and took half a point off the drawdown.

**Spot crypto as a proxy before the ETFs** (`U20X`), for the trend variants:

| set-up | period | variant | return | max DD | kill switch |
|---|---|---|---|---|---|
| U20 | selection | IV_trend | +35.5% | 8.8% | no |
| U20X | selection | IV_trend | +37.0% | 8.8% | no |
| U20 | selection | EW_trend | +31.5% | 12.2% | no |
| U20X | selection | EW_trend | +35.0% | 12.2% | no |
| U20 | holdout | IV_trend | +63.9% | 9.3% | no |
| U20X | holdout | IV_trend | +72.8% | 9.3% | no |
| U20 | holdout | EW_trend | +60.0% | 15.7% | no |
| U20X | holdout | EW_trend | +155.8% | 18.8% | no |

With the proxies the equal-weight trend variant makes much more in the holdout, because it holds bitcoin and ether in the years before the ETFs existed. It is a proxy, not something that could have been bought as an ETF, and it is reported for sensitivity only.

**Calibration of what was actually held.** For the 22 runs that were invested almost the whole period, the 5-session loss of the held weights went beyond their own simulated 5% quantile in 4.2% to 5.9% of the sessions and beyond the 1% quantile in 1.2% to 2.1% (`held_exceed_q05`, `held_exceed_q01` in `risk_runs.csv`). That matches gate R1: the 1% tail is thicker than simulated for these weights too.

## 6. What the 36 runs say together

- Trend brake: 12 runs (3 allocators x 2 set-ups x 2 periods), 12 positive, none killed, lowest return +22.7%, deepest drawdown 15.7%.
- No brake or drawdown brake: 24 runs, 6 killed, 7 negative, and 5 of the 12 drawdown-brake runs ended in cash for good.
- The range across variants is still wide: -17.7% to +35.5% (`U20`, selection), -3.3% to +104.1% (`U20`, holdout), -15.2% to +79.6% (`S6`, selection), +5.9% to +116.7% (`S6`, holdout). The allocator matters less than the brake, and which allocator is best changes with the period.
- No out-of-sample Sharpe is above 1.5 (highest: 1.03), so the SPEC alarm does not apply.

## 7. Limits

- **The trend brake is a direction signal.** The design admitted it as the one answer to assets with a negative carry. It is the 200-day rule of the SPEC, not something found here, but the result of this document is mostly a result about that rule on this universe.
- 9 variants were tried and one selected; the selected one is optimistically biased. The holdout was read once.
- The holdout was not fully blind: the equal-weight buy-and-hold of 2017-2026 was known. The trend variants were not.
- The universe was chosen in 2026 (hindsight). One data source before 2016.
- Cash earns nothing in the runs; the trend variants hold 28% to 53% in cash on average, so an interest rate would add to them, and it is not measured.
- Spread is a constant 5 bps (10 for crypto); thin assets cost more in reality. Fills are at the adjusted open of the next session.
- `U20` uses 10,000 simulated USD. The real account size is 1,000 USD, where the pre-registered selection did badly (section 4).
- The drawdown brake as pre-registered is absorbing; a version that can re-enter was not tested.

## 8. What this suggests (decisions for the owner)

1. If a paper instance is wanted, the only candidate the gate supports is `U20` / `IV_trend` with 10,000 simulated USD, as a separate instance (design section 8). It needs its own short design and approval: phase 5 is not started.
2. At 1,000 USD the pre-registered selection (`IV_dd`) is not worth running. The trend variants were positive at that size too, but choosing one now would be a choice made after seeing the holdout, and it would have to be labelled as such.
3. Not tested, and exploratory if tried: handing the weight of the assets the trend brake removes to the others, and paying interest on cash.

## 9. Reproduce

```bash
python scripts/research/mc_daily_data.py     # the daily research database
python scripts/research/mc_risk_evaluate.py  # all runs and the gate (about 1 minute on 12 cores)
```

Outputs go to `reports/mc_risk/` (git-ignored, including the equity curves); the committed copies are in `docs/results/mc_risk/`:

| File | Content |
| --- | --- |
| `r2_summary.json` | settings and the checks of the gate for each set-up |
| `risk_runs.csv` | every run: the 9 variants and the extras, both periods, the three set-ups |
| `risk_yearly.csv` | return of each calendar year for every run |

Code: `src/stride/forecast/risk_walkforward.py` (the account is the existing `Trader`, driven unchanged), tests in `tests/test_risk_walkforward.py`. The run is deterministic given the data and the seed.

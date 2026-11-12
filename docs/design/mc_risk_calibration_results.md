# Risk allocation, gate R1: risk calibration on daily data

Phase 2 of `docs/design/mc_risk_allocation.md`. Run on 2026-11-12 with `scripts/research/mc_daily_calibration.py` on the daily research database of phase 1 (20 assets, Tiingo, 2007-01-03 to 2026-11-11). Outputs: `docs/results/mc_daily/r1_*.csv` and `r1_summary.json`.

Every figure here is a measurement on past data. None is an expectation, and no allocation result exists yet: this gate only asks whether the simulated risk can be trusted as a control.

## 1. Verdict

**Gate R1 is met.** All eight checks pass, in the selection period (2008-2016) and in the holdout (2017 to 2026-11).

| Check | Selection | Holdout | Limit |
| --- | --- | --- | --- |
| Coverage of the 90% interval, 5 sessions, assets pooled | 0.902 | 0.896 | 0.87 to 0.93 |
| Coverage of the 90% interval, 20 sessions, assets pooled | 0.900 | 0.894 | 0.87 to 0.93 |
| Equal-weight portfolio, 5-session loss beyond the simulated 5% quantile | 5.1% | 4.4% | at most 7.5% |
| Equal-weight portfolio, 5-session loss beyond the simulated 1% quantile | 1.6% | 1.6% | at most 2% |

What passing means and what it does not:

- The simulated volatility, tails and correlations of this universe hold on 19 years of daily data, including 2008, for a typical evaluation.
- **The 1% tail is underestimated.** A loss beyond the simulated 1% quantile happened 1.6 times as often as the model says, in both periods. It is inside the limit, and the bootstrap intervals contain 1% (0.9% to 2.5% and 0.8% to 2.6%), but the point estimate is on the same side twice.
- **In a crash the model is late.** In 2020 the 5-session loss went beyond the simulated 1% quantile in 14 of 253 evaluations (5.5%), and by up to 2.9 times its size. Section 4.
- Passing R1 says nothing about return. It only allows phase 3 and 4 to use the simulated risk for sizing and for the loss cap.

## 2. What was run

| Item | Value |
| --- | --- |
| Simulator | the unchanged `standardize` and `simulate` of `src/stride/forecast/`, one slot per session, through `src/stride/forecast/daily_risk.py` |
| Direction | none: drift shrink 0, the null model |
| Window, volatility span, mean run | 252, 60 and 2 sessions |
| Paths | 2,000 joint paths per evaluation (the same historical dates for every asset in a path) |
| Evaluations | every session from 2008-01-03: 4,740 with a 5-session outcome |
| Universe | as-of: an asset enters after 252 sessions of its own history (16 assets in 2008, 18 from 2013, 20 since 2025) |
| Portfolio | equal weight over the as-of universe, bought at the close of the evaluation and held; no costs, no rebalance |
| Intervals | block bootstrap over calendar months, 2,000 resamples, 95% |
| Seed | 20261112 (the seed of each evaluation derives from it and the row) |

**One point of the design was ambiguous and was settled before the run.** Section 6 of the design says "per asset: the simulated 90% interval contains the outcome 87% to 93% of the time". The gate pools all assets, as the 30-minute gate of `docs/design/mc_forecast.md` did, and the asset-by-asset figures are reported next to it (section 3). Read asset by asset at 20 sessions, four cells fall outside the range.

## 3. Coverage of the 90% interval

| Period | Horizon | Cases | Coverage | 95% interval |
| --- | --- | --- | --- | --- |
| selection | 5 | 38,165 | 0.902 | 0.892 to 0.911 |
| selection | 20 | 38,165 | 0.900 | 0.881 to 0.917 |
| holdout | 5 | 45,310 | 0.896 | 0.886 to 0.906 |
| holdout | 20 | 45,010 | 0.894 | 0.877 to 0.910 |
| 2008-2009 | 5 | 7,951 | 0.910 | 0.887 to 0.931 |
| 2008-2009 | 20 | 7,951 | 0.908 | 0.851 to 0.951 |
| 2020 | 5 | 4,554 | 0.879 | 0.807 to 0.937 |
| 2020 | 20 | 4,554 | 0.858 | 0.738 to 0.943 |

Asset by asset (`r1_coverage_by_asset.csv`):

- At 5 sessions every asset is inside 0.87 to 0.93 in both periods (0.885 to 0.916 in the selection period, 0.884 to 0.926 in the holdout).
- At 20 sessions four cells are outside: SLV 0.856 and FXI 0.932 in the selection period; ETHA 0.818 and IBIT 0.848 in the holdout. The two crypto funds have 308 and 440 evaluations, less than two years, so those two are weak evidence either way.

## 4. Portfolio loss

How often the realized return of the equal-weight portfolio fell below the simulated quantile (`r1_portfolio_losses.csv`). Nominal is 5% and 1%.

| Period | Horizon | Quantile | Evaluations | Exceedance | 95% interval |
| --- | --- | --- | --- | --- | --- |
| selection | 5 | 5% | 2,266 | 5.1% | 3.8% to 6.6% |
| selection | 5 | 1% | 2,266 | 1.6% | 0.9% to 2.5% |
| holdout | 5 | 5% | 2,474 | 4.4% | 3.0% to 5.9% |
| holdout | 5 | 1% | 2,474 | 1.6% | 0.8% to 2.6% |
| selection | 20 | 5% | 2,266 | 5.5% | 2.8% to 8.6% |
| selection | 20 | 1% | 2,266 | 1.6% | 0.4% to 3.5% |
| holdout | 20 | 5% | 2,459 | 3.6% | 1.6% to 5.8% |
| holdout | 20 | 1% | 2,459 | 1.3% | 0.1% to 3.0% |

The stress windows, reported apart as the design says (they do not gate; the samples are one episode each and the intervals are wide):

| Window | Horizon | Quantile | Evaluations | Exceedance | 95% interval |
| --- | --- | --- | --- | --- | --- |
| 2008-2009 | 5 | 5% | 504 | 5.2% | 2.1% to 9.0% |
| 2008-2009 | 5 | 1% | 504 | 1.8% | 0.0% to 4.0% |
| 2008-2009 | 20 | 1% | 504 | 4.0% | 0.0% to 11.3% |
| 2020 | 5 | 5% | 253 | 8.3% | 0.0% to 17.1% |
| 2020 | 5 | 1% | 253 | 5.5% | 0.0% to 12.3% |
| 2020 | 20 | 1% | 253 | 9.5% | 0.0% to 25.1% |

The breaches of the 1% quantile at 5 sessions (`r1_breaches_by_year.csv`): 76 in 4,740 evaluations, in 32 different months. The median breach was 1.26 times the simulated quantile and the largest 2.95 times. By year: 9 in 2008, none in 2009, 8 in 2011, 14 in 2020, 7 in 2022, 5 in 2025; the other years have 6 or fewer.

The worst five, all at the start of a crash, when the volatility filter had not caught up yet:

| Evaluation | Simulated 1% quantile | Realized 5-session return |
| --- | --- | --- |
| 2020-03-11 | -11.6% | -15.6% |
| 2020-03-05 | -7.1% | -14.8% |
| 2008-10-03 | -9.8% | -13.3% |
| 2020-03-10 | -10.4% | -13.1% |
| 2020-03-09 | -12.2% | -12.8% |

## 5. What this means for the next phases

These are consequences for the design of phases 3 and 4, not results of them.

1. **Holding the universe at equal weight would have fired the kill switch.** The equal-weight portfolio lost more than 20% over 20 sessions in 15 evaluations, all in 2008 and 2020 (worst: -25.9% and -26.9%). A fully invested portfolio without a working brake does not survive those two years under a 20% limit. This is measured on a buy-and-hold over 20 sessions, without costs.
2. **The 8% loss cap would have been active in the bad years, and only then.** The simulated 1% loss at 5 sessions of the equal-weight portfolio was above 8% in 9.7% of all evaluations: 26% of 2008, 38% of 2009, 30% of 2011, 67% of 2020, 16% of 2025 and never in 2022. So the cap scales exposure down when volatility is already high.
3. **The cap reacts after the first losses, not before.** The largest breaches come in the first days of a crash (2020-03-05: simulated -7.1%, realized -14.8%). A cap sized on the simulated 1% quantile limits the loss of a normal bad week; it does not limit the first week of a crash. Phase 4 must show what the drawdown brake and the trend brake add on top of it, and the kill switch result in 2008 and 2020 is the test.
4. **The tail is about 1.6 times thicker than simulated.** Phase 4 keeps the pre-registered cap (1% quantile, 8%) as designed. Reading it as "a 5-session loss above 8% about once in 60 weeks" is closer to the measurement than "once in 100".

## 6. Limits of this test

- The portfolio is the equal-weight one. The inverse-volatility and equal-risk portfolios of phase 3 weigh the assets differently and their calibration is not measured here; phase 4 reports the same exceedance for the weights actually held.
- One data source before 2016 (phase 1 found no second source for 2008).
- The universe was chosen in 2026 (design section 1, limit 5).
- Evaluations overlap (a 5-session outcome every session), which is why the intervals resample whole months. The stress windows are single episodes: their intervals start at 0.
- 2,000 paths: the sampling error of a simulated 1% quantile is not negligible; it adds noise to single evaluations, not a bias to the frequencies.
- No trading, costs or rebalancing are involved in this gate.

## 7. Reproduce

```bash
python scripts/research/mc_daily_data.py          # phase 1: the daily research database
python scripts/research/mc_daily_calibration.py   # gate R1 (about 20 seconds on 12 cores)
```

Outputs go to `reports/mc_daily/` (git-ignored); the committed copies are in `docs/results/mc_daily/`:

| File | Content |
| --- | --- |
| `r1_summary.json` | settings and the eight checks of the gate |
| `r1_coverage.csv`, `r1_coverage_by_asset.csv` | section 3 |
| `r1_portfolio_losses.csv`, `r1_breaches_by_year.csv` | section 4 |
| `r1_portfolio_cases.csv` | one line per evaluation and horizon: simulated quantiles and realized return of the portfolio |

The per-asset case table (`r1_asset_cases.csv`, about 170,000 lines) is written to `reports/mc_daily/` and not committed. The run is deterministic given the data and the seed. Code: `src/stride/forecast/daily_risk.py`, tests in `tests/test_forecast_daily_risk.py`.

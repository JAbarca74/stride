# Monte Carlo portfolio (`paper_mc`): backtest and calibration results

Phase 5 of `docs/handoff_montecarlo.es.md`. Run on 2026-11-12 with the code of this repository (module `src/stride/forecast/`, scripts `scripts/research/mc_data.py`, `mc_evaluate.py`, `mc_diagnostics.py`). The raw outputs are committed in `docs/results/mc/`.

## 1. Verdict

**The calibration gate of `docs/design/mc_forecast.md` section 8 is not met, so `paper_mc` is built but disabled** (`enabled: false` in `config.paper_mc.yaml`).

- **Direction has no demonstrated edge.** No forecaster (12 tested: 3 history windows x two drift strengths and two analog variants) beats the null model, the forecaster with no direction at all, in the Brier score. In the holdout (2023 to November 2026) none has a 95% interval above zero at any horizon from 1 hour to 20 days. Of the 96 forecaster x period x horizon cells only 3 have one, all of them full-drift forecasters at the 1-hour horizon in the selection period (+0.011 to +0.013); the same forecasters score +0.001 in the holdout, and an edge at 1 hour is smaller than the cost of any trade. The best forecaster at 5 days is statistically indistinguishable from the null (skill -0.002, 95% interval [-0.006, +0.001]); at 20 days it is clearly worse (-0.011, [-0.022, -0.0002]).
- **Risk is well modeled.** The simulated 90% interval contains the outcome 85% to 91% of the time at every horizon for the 126- and 252-day windows (83% to 89% for the 63-day one), so the volatility, tail and slot-of-the-day model works. The 5-day probabilities are honest where they are mostly used: when the model said 54% the outcome was 54%, and when it said 61% the outcome was 63%. What the model cannot do is tell *which* assets will go up.
- **The trading rules built on those probabilities are erratic, not robust.** Across 12 rules the 4-year holdout ranges from -20% to +32% in total return, neighbouring rules differ by tens of points, and 12 of the 24 runs hit the 20% kill switch (section 5). The rule that won the selection period (`H140_p65_k0.25`) returned +32% in the holdout, but its neighbour with a slightly lower threshold (`p60`) returned +29% and the next one (`p55`) lost 19%: that spread is noise-sized, not skill-sized.
- **Being invested explains most of it.** An equal-weight buy-and-hold of the same assets, with the same costs, made +33% (2017-2022) and +54% (holdout) (section 6). The selected rule beat it in the selection period (+68%) and lagged it in the holdout (+32%, with a deeper drawdown: 17.5% against 11.2%).

Recommendation: do not enable the current model. Section 8 lists what the evidence suggests trying next; the infrastructure (30-minute data, the `Trader`, the hourly job, the walk-forward) is reusable for any new forecaster.

## 2. What was run

| Item | Value |
| --- | --- |
| Universe | the 20 assets approved on 2026-11-11 (`docs/design/mc_universe.md`) |
| Data | Alpaca SIP, adjusted, 30-minute bars, regular session only, 2016-01-04 to 2026-11-11 (`data/mc_research.db`, git-ignored) |
| Grid | 7 slots per full session (9:30-10:00, then hourly to the close; early closes have fewer): 19,054 slots. A return is the log change between consecutive slot closes; the first slot of a day contains the overnight gap |
| Selection period | 2017-01-01 to 2022-12-31 (2016 is the warm-up of the longest window) |
| Holdout | 2023-01-01 to 2026-11-11 (read once, with the configuration chosen on the selection period) |
| Forecasters | window 63, 126 or 252 days x {drift 0.5, drift 1.0, analog 0.5, analog 1.0} = 12, each compared with the null model (shrink 0) of the same window |
| Paths | 2,000 per asset and evaluation (1,000 joint paths for the portfolio loss cap in analog mode) |
| Horizons | 1, 7, 35 and 140 slots = 1 hour, 1 day, 5 days, 20 days |
| Evaluation points (stage A) | every 5th session, at the 11:00-12:00 slot and at the close (1,092 evaluations x 20 assets) |
| Evaluation points (stage B) | every slot (hourly), as in the live job |
| Round-trip cost in the forecast | commission and spread of `config.yaml` (IBKR Pro Tiered, 5 bps per side, 10 bps for crypto) for a typical order of 250 USD |
| Trading fills | an order decided after slot k fills at the open of slot k+2 (next session open after the close), with spread and commission |
| Trading limits | 25% per asset; joint caps 40% US equity, 35% metals, 30% crypto; no leverage; simulated 5-day portfolio loss (1% tail) below 8%; minimum order 150 USD; 12 orders a day; no buy and sell of one asset the same day; kill switch at a 20% drawdown from the peak at a session close |
| Starting capital | 1,000 USD per run |
| Seed | 20261112 (the seed of each evaluation derives from it and the row) |

**Stage A (forecast quality)** compares, for every evaluation, asset and horizon, the probability the model gave to "the asset ends above its starting price" (`p_up`) with what happened. Measures: Brier score and Brier skill against the null model (positive = better than saying nothing about direction; the interval is a bootstrap over calendar months, because overlapping horizons are not independent), reliability (probability said against frequency observed, in 10 bins), and coverage of the simulated 90% interval. The forecaster per decision horizon is the one with the best skill on the **selection** period only.

**Stage B (trading)** plays the entry/exit rule and the weights through time with the `Trader` class (the same one the live job drives), for 12 rules: decision horizon H in {35, 140} x entry probability in {0.55, 0.60, 0.65} x Kelly fraction in {0.25, 0.5}; the exit probability is the entry one minus 0.10. The rule is chosen on the selection period (best return over drawdown among runs where the kill switch did not fire).

**Gate** (design section 8), for each decision horizon, on the holdout: (1) every reliability bin with at least 200 cases within 5 points of the diagonal; (2) coverage of the 90% interval between 0.87 and 0.93; (3) Brier skill against the null model above 0 with its 95% interval excluding 0. All three are required.

## 3. Stage A: forecast quality

### The gate

| horizon | forecaster | cases | skill vs null [95% CI] | skill > 0, CI above 0 | max bin gap (<= 0.05) | 90% interval coverage (0.87-0.93) | gate |
|---|---|---|---|---|---|---|---|
| 35 slots (5 days) | drift05_252 | 7260 | -0.0022 [-0.0055, +0.0012] | NO | 0.046 | 0.899 | FAIL |
| 140 slots (20 days) | drift05_252 | 7140 | -0.0106 [-0.0220, -0.0002] | NO | 0.198 NO | 0.905 | FAIL |

The 5-day forecaster passes the reliability and coverage tests and fails the skill test: it is calibrated but carries no information about direction beyond the null. The 20-day forecaster fails reliability and skill and passes coverage: its probabilities are too extreme at 20 days (what it rated 0.3-0.4 happened 57% of the time, what it rated 0.6-0.7 happened 55% of the time).

### Skill of every forecaster

Skill = 1 - Brier(model) / Brier(null), higher is better; the "CI low" columns are the lower end of the 95% interval and must be above 0 to count. `skill vs base rate` uses the hindsight frequency of "up" for each asset and period as the reference, a harder bar that no real forecaster can see in advance. Sorted by selection skill.

| forecaster | horizon | skill selection | CI low (selection) | skill holdout | CI low (holdout) | skill vs base rate (holdout) | mean P(up) said | freq up |
|---|---|---|---|---|---|---|---|---|
| drift05_252 | 35 | -0.0007 | -0.0051 | -0.0022 | -0.0055 | -0.0075 | 0.528 | 0.536 |
| drift05_126 | 35 | -0.0032 | -0.0084 | -0.0042 | -0.0093 | -0.0141 | 0.530 | 0.537 |
| drift05_63 | 35 | -0.0060 | -0.0126 | -0.0096 | -0.0180 | -0.0302 | 0.531 | 0.536 |
| drift10_252 | 35 | -0.0082 | -0.0167 | -0.0084 | -0.0145 | -0.0138 | 0.536 | 0.536 |
| analog05_252 | 35 | -0.0092 | -0.0148 | -0.0148 | -0.0211 | -0.0201 | 0.527 | 0.536 |
| analog05_63 | 35 | -0.0099 | -0.0177 | -0.0149 | -0.0235 | -0.0356 | 0.528 | 0.536 |
| analog05_126 | 35 | -0.0109 | -0.0178 | -0.0126 | -0.0206 | -0.0227 | 0.528 | 0.537 |
| analog10_252 | 35 | -0.0162 | -0.0254 | -0.0207 | -0.0292 | -0.0261 | 0.535 | 0.536 |
| drift10_126 | 35 | -0.0174 | -0.0283 | -0.0161 | -0.0282 | -0.0262 | 0.539 | 0.537 |
| analog10_126 | 35 | -0.0243 | -0.0362 | -0.0238 | -0.0362 | -0.0340 | 0.537 | 0.537 |
| drift10_63 | 35 | -0.0318 | -0.0453 | -0.0354 | -0.0529 | -0.0565 | 0.541 | 0.536 |
| analog10_63 | 35 | -0.0341 | -0.0479 | -0.0396 | -0.0556 | -0.0608 | 0.538 | 0.536 |
| drift05_252 | 140 | -0.0107 | -0.0220 | -0.0106 | -0.0220 | -0.0415 | 0.544 | 0.559 |
| analog05_252 | 140 | -0.0115 | -0.0232 | -0.0154 | -0.0267 | -0.0464 | 0.544 | 0.559 |
| analog05_126 | 140 | -0.0232 | -0.0398 | -0.0238 | -0.0402 | -0.0753 | 0.548 | 0.560 |
| drift05_126 | 140 | -0.0233 | -0.0404 | -0.0235 | -0.0402 | -0.0749 | 0.549 | 0.560 |
| analog05_63 | 140 | -0.0304 | -0.0535 | -0.0379 | -0.0633 | -0.1414 | 0.550 | 0.559 |
| drift05_63 | 140 | -0.0319 | -0.0548 | -0.0371 | -0.0635 | -0.1406 | 0.551 | 0.559 |
| drift10_252 | 140 | -0.0437 | -0.0652 | -0.0333 | -0.0538 | -0.0649 | 0.559 | 0.559 |
| analog10_252 | 140 | -0.0445 | -0.0680 | -0.0380 | -0.0585 | -0.0698 | 0.559 | 0.559 |
| drift10_126 | 140 | -0.0803 | -0.1155 | -0.0700 | -0.1038 | -0.1238 | 0.565 | 0.560 |
| analog10_126 | 140 | -0.0803 | -0.1120 | -0.0699 | -0.1023 | -0.1236 | 0.564 | 0.560 |
| analog10_63 | 140 | -0.1116 | -0.1505 | -0.1141 | -0.1614 | -0.2252 | 0.564 | 0.559 |
| drift10_63 | 140 | -0.1128 | -0.1581 | -0.1146 | -0.1654 | -0.2258 | 0.565 | 0.559 |

Reading: at 5 and 20 days every forecaster sits below zero. The two best at 5 days (`drift05_252`, `drift05_126`) are within noise of zero; stronger drift (`drift10_*`) and the analog generator are worse, and every forecaster gets worse as the horizon grows. A constant forecast of the base rate (the market went up about 55% of the time at 5 days) would have beaten the null, which is why the base-rate skill column is also negative: the models add disagreement between assets that is not informative.

### Other horizons and coverage

| forecaster | horizon (slots) | skill selection | skill holdout | coverage90 holdout |
|---|---|---|---|---|
| analog05_252 | 1 | -0.0250 | -0.0323 | 0.867 |
| analog05_252 | 7 | -0.0156 | -0.0155 | 0.880 |
| analog05_252 | 35 | -0.0092 | -0.0148 | 0.894 |
| analog05_252 | 140 | -0.0115 | -0.0154 | 0.904 |
| drift05_126 | 1 | +0.0001 | +0.0003 | 0.895 |
| drift05_126 | 7 | +0.0008 | -0.0021 | 0.896 |
| drift05_126 | 35 | -0.0032 | -0.0042 | 0.893 |
| drift05_126 | 140 | -0.0233 | -0.0235 | 0.890 |
| drift05_252 | 1 | +0.0004 | +0.0000 | 0.897 |
| drift05_252 | 7 | +0.0016 | -0.0011 | 0.897 |
| drift05_252 | 35 | -0.0007 | -0.0022 | 0.899 |
| drift05_252 | 140 | -0.0107 | -0.0106 | 0.905 |
| drift10_252 | 1 | +0.0109 | +0.0009 | 0.897 |
| drift10_252 | 7 | +0.0016 | -0.0036 | 0.896 |
| drift10_252 | 35 | -0.0082 | -0.0084 | 0.900 |
| drift10_252 | 140 | -0.0437 | -0.0333 | 0.895 |

At 1 hour and 1 day the skill is essentially zero in the holdout (+0.001 and -0.001 at best). The exceptions are the three selection-period cells of the full-drift forecasters at 1 hour (+0.011 to +0.013, interval above zero), which did not repeat in the holdout. Coverage of the simulated 90% interval is 0.87 to 0.91 for these four forecasters at every horizon.

### Reliability of the chosen forecaster (holdout)

Probability said against frequency observed, per bin. A positive gap means the thing happened more often than said.

| horizon | bin | cases | said | happened | gap |
|---|---|---|---|---|---|
| 35 | 0.3-0.4 | 11 | 0.391 | 0.636 | +0.245 |
| 35 | 0.4-0.5 | 1860 | 0.471 | 0.517 | +0.046 |
| 35 | 0.5-0.6 | 5074 | 0.544 | 0.537 | -0.007 |
| 35 | 0.6-0.7 | 315 | 0.614 | 0.625 | +0.011 |
| 140 | 0.2-0.3 | 1 | 0.294 | 1.000 | +0.706 |
| 140 | 0.3-0.4 | 260 | 0.372 | 0.569 | +0.198 |
| 140 | 0.4-0.5 | 1778 | 0.460 | 0.542 | +0.082 |
| 140 | 0.5-0.6 | 3295 | 0.551 | 0.570 | +0.019 |
| 140 | 0.6-0.7 | 1717 | 0.635 | 0.549 | -0.086 |
| 140 | 0.7-0.8 | 89 | 0.719 | 0.618 | -0.101 |

## 4. Why the direction signal is weak (hypotheses, not findings)

- Momentum measured over 3 to 12 months of 30-minute returns is a noisy estimate of the drift; the shrinkage tested (50% and 100%) did not make it informative.
- The analog generator only conditions the first run of each path (about 2 days); it cannot express a multi-week effect, and it is worse than plain drift.
- At 1 hour to 5 days, asset returns are close to unpredictable from their own past; the documented effects (time-series momentum, short-term reversal) are small relative to the noise of 20 assets over 10 years.
- The period 2017-2026 is mostly rising; the equity premium alone makes "up" the more frequent outcome, which a forecaster with no skill (the null) gets wrong by a constant 5 points, not by asset.

## 5. Stage B: trading

All 24 runs (12 rules x 2 periods). `H140`/`H35` is the decision horizon, `pNN` the entry probability, `kX` the Kelly fraction. "Days in cash" is the share of sessions with less than 1% invested. A fired kill switch stops that run for good, so a run that was killed early shows a short, flat tail.

| rule | period | final equity | return | CAGR | vol | Sharpe | max DD | trades | costs USD | avg exposure | days in cash | kill switch |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| H35_p55_k0.25 | selection | 1386 | +38.6% | +5.6% | 12.7% | 0.44 | 19.1% | 227 | 110 | 24% | 56% | no |
| H35_p55_k0.5 | selection | 1285 | +28.5% | +4.3% | 12.3% | 0.35 | 21.7% | 136 | 65 | 24% | 61% | FIRED |
| H35_p60_k0.25 | selection | 1016 | +1.6% | +0.3% | 5.9% | 0.05 | 15.0% | 36 | 17 | 5% | 82% | no |
| H35_p60_k0.5 | selection | 1114 | +11.4% | +1.8% | 7.4% | 0.24 | 16.5% | 24 | 12 | 8% | 78% | no |
| H35_p65_k0.25 | selection | 1000 | +0.0% | +0.0% | 0.0% | 0.00 | 0.0% | 0 | 0 | 0% | 100% | no |
| H35_p65_k0.5 | selection | 1000 | +0.0% | +0.0% | 0.0% | 0.00 | 0.0% | 0 | 0 | 0% | 100% | no |
| H35_p55_k0.25 | holdout | 1093 | +9.3% | +2.3% | 10.1% | 0.23 | 20.7% | 47 | 25 | 18% | 67% | FIRED |
| H35_p55_k0.5 | holdout | 1021 | +2.1% | +0.5% | 12.1% | 0.05 | 22.6% | 37 | 18 | 21% | 64% | FIRED |
| H35_p60_k0.25 | holdout | 920 | -8.0% | -2.1% | 9.8% | -0.22 | 20.9% | 6 | 3 | 8% | 79% | FIRED |
| H35_p60_k0.5 | holdout | 926 | -7.4% | -2.0% | 9.8% | -0.20 | 20.9% | 6 | 3 | 8% | 79% | FIRED |
| H35_p65_k0.25 | holdout | 1000 | +0.0% | +0.0% | 0.0% | 0.00 | 0.0% | 0 | 0 | 0% | 100% | no |
| H35_p65_k0.5 | holdout | 1000 | +0.0% | +0.0% | 0.0% | 0.00 | 0.0% | 0 | 0 | 0% | 100% | no |
| H140_p55_k0.25 | selection | 987 | -1.3% | -0.2% | 8.0% | -0.03 | 20.1% | 363 | 152 | 30% | 68% | FIRED |
| H140_p55_k0.5 | selection | 1032 | +3.2% | +0.5% | 8.2% | 0.06 | 20.2% | 309 | 139 | 30% | 68% | FIRED |
| H140_p60_k0.25 | selection | 990 | -1.0% | -0.2% | 7.4% | -0.02 | 21.1% | 59 | 25 | 26% | 68% | FIRED |
| H140_p60_k0.5 | selection | 978 | -2.2% | -0.4% | 7.5% | -0.05 | 21.5% | 46 | 20 | 27% | 68% | FIRED |
| H140_p65_k0.25 | selection | 1677 | +67.7% | +9.0% | 12.2% | 0.74 | 18.3% | 37 | 18 | 56% | 18% | no |
| H140_p65_k0.5 | selection | 926 | -7.4% | -1.3% | 6.9% | -0.19 | 20.2% | 18 | 9 | 23% | 64% | FIRED |
| H140_p55_k0.25 | holdout | 814 | -18.6% | -5.2% | 6.4% | -0.81 | 20.1% | 517 | 222 | 39% | 53% | FIRED |
| H140_p55_k0.5 | holdout | 803 | -19.7% | -5.5% | 6.7% | -0.82 | 21.3% | 526 | 224 | 40% | 52% | FIRED |
| H140_p60_k0.25 | holdout | 1289 | +28.9% | +6.8% | 12.9% | 0.53 | 12.9% | 370 | 164 | 73% | 10% | no |
| H140_p60_k0.5 | holdout | 1285 | +28.5% | +6.7% | 12.3% | 0.55 | 11.6% | 373 | 163 | 74% | 9% | no |
| H140_p65_k0.25 | holdout | 1323 | +32.3% | +7.5% | 12.7% | 0.60 | 17.5% | 25 | 12 | 53% | 28% | no |
| H140_p65_k0.5 | holdout | 1320 | +32.0% | +7.5% | 12.7% | 0.59 | 17.5% | 25 | 12 | 54% | 28% | no |

What the table says:

- **Kill switch.** 12 of the 24 runs fired it. The 20% limit is tight for a portfolio that holds 5 to 6 concentrated positions in volatile assets (silver, oil, natural gas, crypto): a normal bad month reaches it.
- **Instability.** Rules that differ by one notch in a parameter differ by tens of points of return (`H140_p55` -19%, `p60` +29%, `p65` +32% in the holdout). Picking the winner among 12 on the selection period picked a good one in the holdout by luck as much as by design (`H35_p55_k0.25`, chosen for the 5-day horizon, made +9% and fired the kill switch).
- **Costs.** The rules that trade a lot (`H140_p55`: 500+ trades in the holdout) pay 220 USD in commission and spread on 1,000 USD, about 6% a year; `p65` pays 12 USD. At 250 USD per order the 0.35 USD minimum commission is 14 bps per side, plus 5 to 10 bps of spread.
- **No look-ahead.** The walk-forward passes only rows up to the evaluation to the forecast; `tests/test_walkforward.py` rewrites every later bar and checks that no forecast changes.

## 6. Diagnostic: what the assets did by themselves

Not a benchmark and not a goal (the owner's objective is absolute profit): it only shows how much of a rule's result is just being invested. An equal-weight buy-and-hold of the assets listed at the start of each period, bought once with the same costs:

| period | assets | final equity | return | CAGR | max DD |
|---|---|---|---|---|---|
| selection | 18 | 1334 | +33.4% | +4.9% | 28.3% |
| holdout | 18 | 1544 | +54.4% | +12.0% | 11.2% |

(18 assets: IBIT and ETHA did not exist in these periods' starts.) Per-asset annualized return, volatility, drawdown and the share of the time each asset was up after 5 and 20 sessions are in `docs/results/mc/asset_stats.csv`. Highlights: QQQ was up after 20 sessions 65-68% of the time; the long-bond ETF TLT lost money in both periods; natural gas had an annualized log return of -0.42 in the holdout with an 83% drawdown; ETHA -0.22 since its launch.

## 7. Limits of this evaluation

- One regime: a 10-year, mostly rising sample with a single crash (2020) and one bond/equity drawdown (2022). 30-minute data starts in 2016, so 2008 is not covered.
- 12 trading rules and 15 forecasters were tried; the winner of 12 is optimistically biased. Only the holdout counts, and it is 3.8 years.
- The spread is a constant (5 bps, 10 bps for crypto), while quotes measured on 2026-11-10 were 0.6 bps for QQQ, 0.5 for GLD, 3.4 for UUP and 9.8 for UNG (`docs/design/mc_universe.md`). Real costs for the thin assets would be higher.
- Stage A evaluates every 5th session at two slots, with 2,000 paths; probabilities have a sampling error of about 1 point.
- Fills use the open of a slot; a real order sent a few minutes after the data is published would fill later and at a different price (15-minute data delay of the Basic plan).
- The `analog` generator is a first version (nearest-neighbour start, then the ordinary bootstrap).
- Weights come from `kappa x edge / variance`; the mean in `edge` is the least reliable number of the simulation, which is why the sizing is erratic.

## 8. What the evidence suggests trying (for the next analysis)

In order of expected value; all can reuse the data, the `Trader` and the walk-forward.

1. **Allocate by risk, not by predicted direction.** The volatility model is calibrated (88-91% coverage). A portfolio that holds the assets with weights from simulated risk (volatility targeting, risk parity with the joint paths, the existing portfolio loss cap as the control) needs no forecast of direction and trades rarely. The buy-and-hold of this universe made +33% and +54% in the two periods with 28% and 11% drawdowns; a risk-weighted version with a trend or drawdown brake could keep most of the return and lower the drawdown. This is the closest to the "maximum profit with the kill switch as the limit" objective that the data supports.
2. **Use the daily history for multi-day horizons.** For 5 and 20 days the 30-minute data adds nothing; `data/stride.db` has daily bars since 2007 for 36 symbols (2008 and 2020 included). The same forecasters and the same gate on 19 years of daily data would be a far stronger test, and also supports the cross-sectional question below.
3. **Cross-sectional ranking.** Test "which of the 20 assets will do better than the others over the next 20 days" (relative momentum) instead of "will this asset go up"; the equity premium cancels out and the ranking is what the portfolio actually needs.
4. **Fewer and larger positions.** At 1,000 USD the 0.35 USD minimum commission and the 150 USD minimum order dominate costs. Lower turnover (only `p65`-like thresholds), or a different execution venue (Alpaca charges no commission; the cost model of `paper_mc` was set to IBKR on 2026-11-11 and could be revisited for this instance) changes the economics more than a better forecast.
5. **Calibrate before sizing.** Replace `kappa x edge / variance` by a sizing that uses only calibrated quantities (probability bins and the simulated loss tail), or shrink the drift toward the unconditional base rate before sizing.
6. **Mean reversion at 1 day** (the idea behind the DIP sleeve) has not been tested in this framework; a generator that conditions on a recent drop is the natural second analog.
7. **Save the case table.** Stage A did not store the per-case lines; storing them would allow skill by asset class, by volatility regime and by slot of the day.

## 9. Reproduce

```bash
python scripts/research/mc_data.py        # 30-minute bars of the 20 assets into data/mc_research.db (about 15 minutes)
python scripts/research/mc_evaluate.py    # stages A and B (about 22 minutes on 12 cores); --smoke for a 3-minute test
python scripts/research/mc_diagnostics.py # the context tables of section 6
```

Outputs go to `reports/mc_eval/` (git-ignored); the committed copies are in `docs/results/mc/`:

| File | Content |
| --- | --- |
| `summary.json` | run time, periods, chosen forecasters, the gate, the holdout reliability tables |
| `quality_metrics.csv` | stage A: skill, interval, bin gap, coverage per forecaster, period and horizon |
| `trading_all.csv` | stage B: the 24 runs |
| `asset_stats.csv`, `universe_hold.csv` | section 6 |

The evaluation is deterministic given the data and the seed. The code is exercised by `tests/test_forecast.py`, `tests/test_walkforward.py`, `tests/test_intraday.py` and `tests/test_run_mc.py`.

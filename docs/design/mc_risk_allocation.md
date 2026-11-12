# Risk-based allocation for the Monte Carlo universe: design

Status: **approved by the owner on 2026-11-12** (the design, `EW`/`none` as the reference of gate R2 and the Tiingo download). Phases 1 to 3 are built (section 11). **Gate R1 is met** (`docs/design/mc_risk_calibration_results.md`); gate R2 has not been run and there is no allocation result yet. It follows `docs/design/mc_backtest_results.md` section 8 (items 1 and 2) and `docs/handoff_montecarlo_estado.es.md` section 6.

Rules this work keeps:

- `config.paper_mc.yaml` is not changed and the running `paper_mc` experiment is not touched (a parameter change restarts it). Its behaviour must stay bit-identical: new code goes in new files, and no existing function of `src/stride/forecast/` changes what it returns.
- No protected module is touched (`risk/leverage.py`, `risk/guard.py`, the kill switch, `execution/orders.py`, `execution/ibkr_broker.py`), no value of `config.yaml`, `config.paper_b.yaml` or `config.paper_dip.yaml`.
- `data/stride.db` is opened read-only.
- No number is promised. Every figure quoted below is a past measurement taken from the results document; everything else is a test still to be run.

## 1. The idea and what it cannot do

The evaluation found that the simulation cannot tell which asset will go up, and that its risk interval is calibrated. The current rule uses the unreliable number (the expected gain) both to decide and to size, and it sat in cash a large part of the time (18% to 100% of the sessions, depending on the rule) while an equal-weight buy-and-hold of the same assets made +33% and +54% in the two periods, with drawdowns of 28% and 11%.

The proposal is to stop forecasting direction: hold the universe, and use the simulation only for what it does well, which is sizing by risk and keeping the portfolio away from the 20% kill switch.

Limits that must be stated before any test:

1. **Risk allocation creates no edge.** Its return is the return of the assets it holds. What it can change is how much of that return is kept per unit of drawdown, and how often the kill switch fires.
2. **No leverage.** With `max_total` at 1.0, volatility targeting can only scale exposure down. Risk parity moves weight toward the low-volatility assets (UUP, TLT, XLU), and without leverage that lowers portfolio volatility and probably return. Leverage is not proposed: `risk/leverage.py` is protected and the instance has no margin.
3. **Risk weights ignore expected return.** Assets with a negative measured carry get weight like any other (natural gas: annualized log return of -0.42 in the 2023-2026 period, TLT lost money in both periods). A brake (section 4) is the only pre-registered answer to that.
4. **Per-asset calibration is not portfolio calibration.** The 85% to 91% coverage was measured asset by asset, on 30-minute data since 2016. The portfolio loss cap depends on the simulated correlations, which have not been tested, and neither has anything in 2008. Testing both is the first gate (section 6).
5. **The universe was chosen in 2026.** A test of that list on 2007 onward has a hindsight bias (all 20 survived and are liquid today). It cannot be removed, only stated.

## 2. Data: the daily history

`data/stride.db` (table `prices`, adjusted daily bars, Tiingo, 2007-01-03 to 2026-10-23) has 15 of the 20 assets:

| Available from | Assets |
| --- | --- |
| 2007-01 | QQQ, IWM, XLE, XLV, EFA, TLT, GLD, SLV, USO, VNQ, DBA |
| 2007-03 | UUP |
| 2011-11 | CPER |
| 2024-01, 2024-07 | IBIT, ETHA |
| missing | XLU, FXI, INDA, EWZ, UNG |

- The five missing symbols are downloaded from Tiingo (data-only key, already allowed) into a separate research database, `data/mc_daily_research.db` (git-ignored by `*.db`), together with a copy of the 15 and a top-up to the current date. `data/stride.db` is not written.
- **As-of universe.** An asset joins when it has a full history window, as in the 30-minute evaluation. In 2008 the portfolio therefore holds at most 16 assets (no CPER, INDA, IBIT, ETHA).
- **Crypto.** The main test uses IBIT and ETHA only since they exist. `BTC-USD` and `ETH-USD` (Coinbase, since 2015 and 2016) are used only in one labelled sensitivity run as proxies; a spot price is not the ETF.
- **Grid.** One step per NYSE session, close to close. The existing `simulate()` and `standardize()` already accept `n_slots=1`, so the same filtered bootstrap runs on daily returns without changing them. `run_forecast` and `ForecastParams` assume 7 slots per day and are not used by the daily research.

## 3. Allocators (pre-registered)

All take the joint simulated paths at the risk horizon (20 sessions, 2,000 paths, 252-day window, drift shrink 0: the null model, no direction).

| Id | Weights before limits |
| --- | --- |
| `EW` | equal weight: the null allocation, the reference the others must beat |
| `IV` | inverse of the simulated 20-day standard deviation of each asset |
| `ERC` | equal risk contribution, from the covariance of the joint simulated paths |

Limits applied to every allocator, taken from the current instance and not tuned: 25% per asset, the three group caps, total at most 100%, simulated 5-day loss (1% tail) at most 8% of equity, minimum order 150 USD, rebalance band 20%.

Rebalance: weights are recomputed on the first session of each month; between rebalances the only changes are reductions forced by the loss cap or by a brake. Fills at the next session open with the commission and spread of `config.yaml` (read, not changed).

Two ablations, one run each, to learn whether the simulation earns its place:

- `IV` with a plain 60-day trailing volatility instead of the simulated one.
- The selected variant without the portfolio loss cap.

## 4. Brakes (pre-registered)

| Id | Rule |
| --- | --- |
| `none` | always hold |
| `trend` | an asset is held only while its close is above its 200-day average (the `TREND_FILTER` idea of the SPEC, checked at the monthly rebalance) |
| `dd` | exposure is multiplied by `1 - drawdown / 0.15`, floored at 0: the portfolio is in cash before the 20% kill switch |

The grid is 3 allocators x 3 brakes = **9 variants**, plus the 2 ablations and 1 crypto-proxy run. No other variant is added after seeing results; if one is, the document says so and it is reported as exploratory.

## 5. The 1,000 USD problem (owner decision 1)

With 1,000 USD and a 150 USD minimum order the account holds at most 6 positions. A risk-weighted portfolio of 20 assets cannot exist at that size: the minimum order would drop the smallest weights, which under `IV` and `ERC` are the most volatile assets, leaving an arbitrary low-volatility portfolio. Two set-ups are proposed, both run:

- **`U20` at 10,000 USD**: the full universe. It measures the allocation itself.
- **`S6` at 1,000 USD**: one asset per class, fixed in advance by a rule and not by results: the asset with the lowest spread measured in `docs/design/mc_universe.md` in each of six classes (US equity, international equity, bonds and dollar, metals, energy and agriculture, real estate). It measures what the real account size allows.

The difference between the two is the price of the commission floor. Which one would go to paper, and with what simulated capital, is the owner's decision after the results.

## 6. Tests and gates

Walk-forward only, with the anti-look-ahead test of the existing suite extended to the daily panel.

- **Selection period:** 2008-01-01 to 2016-12-31 (2007 is the warm-up). It contains 2008 and 2011.
- **Holdout:** 2017-01-01 to the last bar, read once. Caveat: the equal-weight buy-and-hold result of this period is already known from the 30-minute evaluation, so `EW`/`none` is not blind; the other variants are.

**Gate R1, risk calibration on daily data (before any allocation result is looked at).**

1. Per asset: the simulated 90% interval contains the outcome 87% to 93% of the time at 5 and 20 sessions, in each period.
2. Portfolio: for the `EW` weights, the realized 5-day loss exceeds the simulated 5% quantile in at most 7.5% of the evaluations and the simulated 1% quantile in at most 2%, in each period, with block-bootstrap intervals by calendar month.
3. The same exceedance frequencies for 2008-2009 and for 2020 are reported apart. They do not gate, because the samples are small, but a failure there is stated in the verdict.

If R1 fails, the loss cap is not a trustworthy control and the work stops there with that finding.

**Gate R2, allocation.** The variant is selected on the selection period only (best return over maximum drawdown among runs whose kill switch did not fire). It passes if, on the holdout:

1. the 20% kill switch did not fire, in either period;
2. net return after costs is positive in both periods;
3. total costs are below 1% of starting capital per year;
4. return over maximum drawdown is at least that of `EW`/`none` with the same costs. This is the null allocation of the same assets, not an index;
5. robustness: at least 6 of the 9 variants avoid the kill switch in both periods, and the selected variant's neighbours (same allocator with the other brakes, same brake with the other allocators) all have a positive holdout return.

The SPEC alarm applies: an out-of-sample Sharpe above 1.5 is audited as a probable error. All 9 variants are published, not only the selected one. If R2 fails, the result is that risk allocation does not improve on holding this universe, and nothing goes to paper.

## 7. Phases, one pull request each

| Phase | Content | Ends with |
| --- | --- | --- |
| 0 | this design | owner approval |
| 1 | daily research data: `scripts/research/mc_daily_data.py`, the research database, a daily `ReturnPanel` builder, data-quality report, tests | data ready, no result |
| 2 | gate R1: `scripts/research/mc_daily_calibration.py`, results in `docs/results/mc_daily/` | R1 verdict |
| 3 | pure module `src/stride/forecast/risk_weights.py` (`EW`, `IV`, `ERC`, brakes), with hypothesis tests: weights never negative, never above the caps, sum at most 1, every step only lowers a weight | module, no result |
| 4 | gate R2: the daily walk-forward of the 9 variants, `docs/design/mc_risk_results.md` | R2 verdict |
| 5 | only if R1 and R2 pass and the owner approves: a new paper instance (section 8) | separate design |

## 8. How it could reach paper (owner decision 2, not now)

It cannot be a change to `config.paper_mc.yaml` while the forward experiment runs (evaluation at 40 sessions, about 2027-01-11). The options, to be decided after phase 4:

- a fourth, separate instance with its own config and database, added to the loop the way `paper_mc` was, with daily decisions (one evaluation after the close, fills at the next open);
- or wait for the `paper_mc` verdict and replace its rule then.

## 9. Other ways to more profit, and where they stand

In the order this design suggests testing them, each as its own design and pull request, all on the daily history:

1. **Risk allocation** (this document).
2. **Cross-sectional ranking** (results section 8, item 3): relative strength between the 20 assets as a tilt on the risk weights. It reuses phases 1 and 3; its test is a rank correlation between the score and the next-month return, with the same selection and holdout.
3. **Cost of execution** (item 4): at 1,000 USD the 0.35 USD minimum commission weighs more than any forecast measured so far. This is an owner decision on the cost model or the capital of a new instance, not a modelling task; section 5 measures its size.
4. **Sizing with calibrated quantities only** (item 5): made unnecessary by this design if R2 passes, since no expected gain is used.
5. **One-day mean reversion** (item 6): needs the 30-minute data and belongs next to the DIP work, not here.

## 10. Open points for the owner

1. Approve the design: the 9 variants, the two set-ups of section 5, the periods and the two gates.
2. Confirm that `EW`/`none` may be used as the reference of gate R2 (it is the null allocation of the same assets, in the same role as the null forecaster, not an index).
3. Confirm the download of XLU, FXI, INDA, EWZ and UNG from Tiingo into the research database.

## 11. As built

### Phase 1: daily research data (2026-11-12)

| Piece | File |
| --- | --- |
| Daily panel, as-of universe, data-quality table (pure) | `src/stride/forecast/daily.py` |
| Download and report | `scripts/research/mc_daily_data.py` |
| Tests | `tests/test_forecast_daily.py` |
| Report of this run | `docs/results/mc_daily/` |

Differences from section 2:

- **All 20 assets were downloaded in full from Tiingo, not copied from `data/stride.db` and topped up.** Adjusted prices are rescaled at every dividend, so bars appended to an older copy would not line up with it. `data/stride.db` (read-only) gives the two crypto spot series and a cross-check.
- The crypto spot proxies (`BTC-USD`, `ETH-USD`) are a copy and end on 2026-10-23; they are not topped up. They are only used in the labelled sensitivity run.

What the data looks like (`data_quality.csv`, `universe_as_of.csv`):

- 20 assets, 2007-01-03 to 2026-11-11, 4,997 sessions for the 12 that existed at the start. Later starts: DBA 2007-01-05, UUP 2007-03-01, UNG 2007-04-18, CPER 2011-11-15, INDA 2012-02-03, IBIT 2024-01-11, ETHA 2024-07-23.
- No missing session inside any asset's own span, no bar on a non-session, no non-positive price.
- The as-of universe (a full 252-session window) has 16 assets from 2008 to 2011, 17 at the end of 2012, 18 from 2013 to 2024 and 20 since 2025.
- Largest one-day moves are between 4% (UUP) and 29% (SLV, 2026-01-30); none was removed. CPER has a run of 8 sessions with an unchanged close, the longest of the 20 (the others have at most 3).

Cross-checks:

- Against `data/stride.db` (same provider, so it only proves the download is the same series): the daily returns of the 15 shared assets are identical on every common day (`cross_check.csv`).
- Against the Alpaca 30-minute bars of `data/mc_research.db`, an independent source, 2016 onward (`cross_check_intraday.csv`): the median gap between the two daily returns is 1 to 6 basis points per asset, expected because the last 30-minute bar is not the closing auction. 18 days in total differ by more than 1% (CPER 5, FXI 4, INDA 4, XLE 2, USO 2, DBA 1), the largest 3.5% (CPER, 2016-01-19). Nothing before 2016 has a second source.

The existing simulator was not changed: `tests/test_forecast_daily.py` runs `standardize` and `simulate(..., n_slots=1)` on a daily panel and checks reproducibility and that the simulated risk follows the volatility of the data.

### Phase 2: gate R1 (2026-11-12)

| Piece | File |
| --- | --- |
| Daily simulation, calibration cases, block-bootstrap frequency (pure) | `src/stride/forecast/daily_risk.py` |
| The gate | `scripts/research/mc_daily_calibration.py` |
| Tests | `tests/test_forecast_daily_risk.py` |
| Results | `docs/design/mc_risk_calibration_results.md`, `docs/results/mc_daily/r1_*` |

Gate R1 is met: pooled coverage of the 90% interval 0.894 to 0.902 at 5 and 20 sessions in both periods, and the 5-session loss of the equal-weight portfolio went beyond the simulated 5% and 1% quantiles in 4.4% to 5.1% and 1.6% of the evaluations.

Settled before the run: the coverage check of section 6 pools all assets, as the 30-minute gate did; the asset-by-asset figures are reported (four cells outside the range at 20 sessions, none at 5).

Findings that phases 3 and 4 must respect (details in the results document, section 5): the 1% tail was exceeded about 1.6 times as often as simulated; in the first days of a crash the realized loss was up to 2.9 times the simulated 1% quantile; and an equal-weight hold of the universe lost more than 20% over 20 sessions in 2008 and in 2020, so the kill switch result in those two years is the real test of the brakes.

### Phase 3: weights and brakes (2026-11-12)

| Piece | File |
| --- | --- |
| Allocators `EW`, `IV`, `ERC`, limits, brakes, loss-cap factor (pure) | `src/stride/forecast/risk_weights.py` |
| Tests, with hypothesis properties | `tests/test_risk_weights.py` |

No result: the module is not run on market data in this phase. `src/stride/forecast/weights.py` is not changed (its `portfolio_loss` is imported).

Points the design left open, settled here before any allocation result exists:

- **What a removed weight becomes.** Every step after the allocator only lowers a weight, and what is taken away stays in cash. That holds for the caps, for a target below the minimum order, and for the `trend` brake: an asset below its 200-day average leaves its share in cash and the other assets are not scaled up (the usual form of this rule, and the one that does not concentrate the portfolio in a crash).
- **`ERC` method.** Cyclical coordinate descent on the covariance of the joint simulated paths, rescaled to sum to 1. The hypothesis test checks that every risk contribution equals 1/n.
- **`trend` needs 200 closes.** An asset with fewer is not held by the `trend` variants.
- **The `dd` brake has an absorbing end.** At a drawdown of 15% its exposure is zero, and a portfolio in cash cannot recover its drawdown, so a `dd` variant that reaches 15% stays in cash for good. That is the rule as pre-registered (it is in cash before the 20% kill switch); phase 4 reports whether and when it happens instead of changing the rule.

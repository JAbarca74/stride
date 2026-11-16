# Risk allocation, three follow-ups: design

Status: **proposal, not approved** (2026-11-13). Nothing here is built or run. It follows `docs/design/mc_risk_results.md` (section 7, limits, and section 8, item 3) and `docs/design/mc_backtest_results.md` (section 8, item 3). Each part is its own phase and pull request, and each needs the owner's go-ahead.

Rules this work keeps:

- `config.paper_risk.yaml` and `config.paper_mc.yaml` are not changed while their experiments run. Nothing here is a change to `paper_risk`: a variant that looks good would need its own instance, design and approval.
- No protected module is touched, no value of `config.yaml`, `config.paper_b.yaml` or `config.paper_dip.yaml`. `data/stride.db` is opened read-only.
- The backtest and the paper job share `step_session` and `RiskTrader`. Any new behaviour is off by default, the 114 runs of `docs/results/mc_risk/risk_runs.csv` must regenerate identical, and `python -m stride.run_risk --verify` must still replay the paper account exactly.
- No number is promised. Every figure quoted is a past measurement from the results documents.

## 0. What kind of evidence this can be

**Both periods have been read.** The selection period (2008-2016) and the holdout (2017 to 2026-11) were used to select and to judge `IV_trend`, and the results of 19 runs per set-up and period are published (the 9 variants, their versions without the loss cap and one plain-volatility run). A variant designed now is designed by someone who has seen them. So:

- Every run of this document is **exploratory**. It is written to `docs/results/mc_risk_explore/`, every table carries the word, and no result may be described as validated out of sample or as passing gate R2.
- The criteria below are fixed before any run so that the reading is not adjusted afterwards. Meeting them means "worth a forward test", nothing more.
- The count of runs tried on this history goes from 19 per set-up to 19 plus the ones below (7 at most). It is reported with every result, and the deflated Sharpe ratio of `src/stride/backtest/validation.py` is reported with that count.
- **The only evidence not yet read is forward.** `data/paper_risk.db` stores the daily bars of the 20 assets from 2026-11. At the 120-session review each exploratory variant can be replayed on those same sessions next to the paper account (no download, no change to the instance). 120 sessions are too few to judge a return; they can show whether exposure, costs and drawdown behave as in the backtest.

Reference for all three parts: `U20` / `IV_trend`, 10,000 USD, as measured (`risk_runs.csv`):

| Period | Return | Max drawdown | Return / drawdown | Average exposure | Costs / year |
| --- | --- | --- | --- | --- | --- |
| selection | +35.5% | 8.8% | 4.02 | 60% | 0.31% |
| holdout | +63.9% | 9.3% | 6.84 | 65% | 0.36% |

Order proposed: part 2 first (it changes the yardstick the other two are read with), then part 1, then part 3.

## 1. Handing the weight of the braked assets to the others

**What it is.** `IV_trend` computes inverse-volatility weights over every eligible asset and then sets to zero the ones below their 200-day average. Their weight stays in cash (35% to 40% on average). The variant rescales the surviving weights before the limits are applied.

**Variants (fixed):**

| Id | Rule after the trend brake, before `apply_limits` |
| --- | --- |
| `IV_trend_r100` | surviving weights are rescaled to sum to 1 |
| `IV_trend_r50` | surviving weights are rescaled to sum to the midpoint between their own sum and 1 |

The limits are unchanged and still apply after the rescale: 25% per asset, the group caps, total at most 100%, the simulated 5-session loss cap of 8%, the 150 USD minimum order. With three assets in trend the account therefore holds at most 75%.

**What it cannot do, stated first.** It adds no information: it is more exposure to the assets the brake already picked. More exposure means a larger drawdown and return together, so a higher return alone shows nothing. The criterion is on return per unit of drawdown, and on the distance to the kill switch.

**What may differ from the reference.** The loss cap never acted for `IV_trend`. With concentrated weights it may, which would make this the first variant where the simulation has something to do. `cap_binding_share` is reported.

**Test (fixed before any run).** Both periods, `U20`, same costs, seed and fills as the reference. A variant is "worth a forward test" only if **all** hold, in **both** periods:

1. the 20% kill switch does not fire;
2. the maximum drawdown is at most 12% (the limit `paper_risk` is judged with);
3. return over maximum drawdown is at least the reference's (4.02 and 6.84);
4. costs are below 1% of starting capital per year;
5. the worst calendar year is no more than 2 points below the reference's worst year of the same period.

Reported and not gating: the result with the cash interest of part 2 on both sides (the reference holds more cash, so it gains more from it), `S6` at 1,000 USD, and the year table.

If a criterion fails, that is the result: the cash of `IV_trend` is part of how it stays away from the kill switch.

**Code.** One optional field on `RiskRule` (`redistribute: float = 0.0`), read in `RiskTrader._rebalance` between the trend brake and `apply_limits`; a pure helper in `risk_weights.py` with hypothesis tests (never negative, sum at most 1, zero stays zero, `0.0` returns the input unchanged). `scripts/research/mc_risk_explore.py`, separate from `mc_risk_evaluate.py`.

## 2. Interest on cash

**What it is.** In every run so far cash earns nothing. This part measures what that omission is worth. It is a correction of the accounting, not a strategy, and it has no pass or fail by itself.

**Two measurements (fixed):**

| Id | Rule | What it is |
| --- | --- | --- |
| `cash_bill` | positive cash accrues daily at the 3-month Treasury bill rate (FRED `DTB3`, already read by `FredSource`) minus a haircut of 0.5 points, floored at 0, on calendar days over 365 | an upper bound: what idle cash would earn if the broker paid close to the bill rate |
| `cash_etf` | the weight left in cash after the limits is held in a Treasury-bill ETF (`BIL`, listed 2007-05), bought and sold like any asset with the commission, spread and minimum order of `config.yaml`; it is outside the trend brake and the group caps, and capped at 100% minus the other targets | what can actually be done in the account |

`cash_bill` needs no trade and no new asset. `cash_etf` needs the `BIL` daily bars from Tiingo in the research database (one symbol, the owner's go-ahead for the download as with the five symbols of phase 1).

**To settle before the run, not assumed here:** whether the broker of the cost model pays interest on a cash balance of this size. If it pays nothing below a threshold, `cash_bill` is not attainable and only `cash_etf` describes the account. This is read from the broker's published terms and written in the results; it was not checked for this design.

**Known facts that bound the result.** The bill rate was close to zero from 2009 to 2015 and in 2020-2021, so most of whatever is measured comes from 2007-2008, 2017-2019 and 2022 onward. Every variant of `risk_runs.csv` holds some cash, so all of them are rerun, not only `IV_trend`: the ranking of gate R2 is recomputed with interest and reported next to the published one, without replacing it.

**Test (fixed before any run).** For each of the 9 main variants of `U20` and both periods, report return, CAGR, maximum drawdown and costs with and without interest. Then, for `cash_etf` on `IV_trend` only, it is "worth proposing for a new instance" if in both periods:

1. the gain in final equity over the plain run is larger than the extra costs it pays (it is not a wash of commissions on a small balance);
2. the maximum drawdown rises by at most 0.5 points;
3. the kill switch does not fire.

Also reported: whether the selection of gate R2 (best return over drawdown on 2008-2016 among the runs that survive) would have been different with interest. If it would, that is stated as a limit of the published selection; the paper instance is not changed.

**Code.** Research only. `Trader` is shared with `paper_mc` and is not touched: the accrual is a step of the research runner before `step_session` (`book.cash *= 1 + rate * days / 365`), tested for exactness against a hand-computed case and for "no interest on zero or negative cash". `cash_etf` is an extra column of the panel with a fixed target, in the research script. `paper_risk` keeps paying nothing on cash while it runs; changing that would change the experiment.

## 3. Ranking between assets as a tilt on the risk weights

**What it is.** The evaluation found no skill in "will this asset go up". The untested question is "which of the 20 will do better than the others". The trend brake already uses each asset's own direction; this part asks whether the order between assets adds anything on top of it.

**Two steps, the second only if the first passes.**

**Step K1: is there a signal?** No account, no costs.

- Score, fixed: the asset's total return over the 252 sessions ending 21 sessions before the rebalance (12 months skipping the last one), computed with rows up to the evaluation only.
- Reported next to it and not used to pass: the same over 126 sessions, and the 252-session score divided by the asset's 60-day volatility.
- At each monthly rebalance, among the eligible assets (at least 8), the Spearman rank correlation between the score and the return from that rebalance's fill to the next one.
- **K1 passes if**, on 2008-2016, the mean monthly rank correlation is positive and the 90% block-bootstrap interval (resampling blocks of 6 consecutive months) excludes zero, **and** on 2017-2026 the mean has the same sign. Both periods are already read for returns, so this is exploratory too; the rank correlation itself has not been computed.

If K1 fails the work stops there with that finding, and no tilt is run.

**Step K2: the tilt.** One variant, fixed:

- `IV_trend_tilt`: each inverse-volatility weight is multiplied by `1 + 0.5 x (2 x p - 1)`, where `p` is the asset's rank percentile by score among the eligible assets (0 for the lowest, 1 for the highest). Multipliers run from 0.5 to 1.5. The weights are rescaled to their sum before the tilt, then the trend brake and the limits apply as in `IV_trend`.
- Keeping the sum unchanged separates this part from part 1: the tilt moves weight between assets and does not raise the exposure by itself.

"Worth a forward test" only if **all** hold, in **both** periods, against `IV_trend`:

1. the kill switch does not fire and the maximum drawdown is at most 12%;
2. return over maximum drawdown is at least the reference's (4.02 and 6.84);
3. costs are below 1% of starting capital per year;
4. the tilt with the multiplier range halved (0.75 to 1.25) is at least as good as the reference on criterion 2 as well (a result that exists only at one strength is not kept).

**Limits to state with any result.** A 12-month return and a 200-day average measure nearly the same thing, so the tilt may add little once the brake is on; `IV_none` with the tilt is reported to show the tilt without the brake. Twenty assets give a coarse ranking. The universe was chosen in 2026.

**Code.** A pure `rank_tilt(scores, strength)` in `risk_weights.py` with hypothesis tests (multipliers inside the range, order preserved, the sum of weights unchanged, a constant score changes nothing), a look-ahead test of the score on the daily panel (changing rows after the evaluation changes no score), and the existing `Weigher` hook of `RiskTrader` to apply it. No change to `step_session`.

## 4. Phases, one pull request each

| Phase | Content | Ends with |
| --- | --- | --- |
| F0 | this design | owner approval, and the decisions of section 5 |
| F1 | part 2: `cash_bill` on the 9 variants, then `cash_etf` after the `BIL` download | the accounting measured, labelled exploratory |
| F2 | part 1: the helper, `IV_trend_r100` and `IV_trend_r50` | the five criteria, met or not |
| F3 | part 3, step K1 | signal or no signal |
| F4 | part 3, step K2, only if K1 passes | the four criteria, met or not |
| F5 | at the 120-session review of `paper_risk`: replay of whatever met its criteria on the stored forward sessions | a forward comparison, too short to conclude on return |

## 5. Decisions for the owner

1. Approve the three parts, or a subset, and the order.
2. The download of `BIL` from Tiingo into the research database (one symbol).
3. The criteria as written, in particular the 12% drawdown limit and "return over drawdown at least the reference's" as the common yardstick, or others, fixed now.
4. Confirm that nothing here may change `paper_risk`: a variant that meets its criteria is a candidate for a separate instance after the 120-session verdict.

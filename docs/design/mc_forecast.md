# Monte Carlo portfolio (`paper_mc`): forecast and decision design

Phase 2 of `docs/handoff_montecarlo.es.md`. Status: **design, waiting for the owner's approval before any code**. It builds on `docs/design/mc_universe.md` (20 assets, approved 2026-11-11 together with: bearish signal = cash only, cost model = IBKR Pro Tiered).

Nothing here changes B, DIP, `execution/orders.py`, `risk/guard.py`, `risk/leverage.py` or the kill switch code. All numbers below are **starting values or pre-registered candidates**; the final ones are chosen by the walk-forward of phase 5 and then live in `config.paper_mc.yaml`.

## 1. What the module does and what it cannot do

Every hour it simulates thousands of possible futures per asset from the asset's own recent history, and reads from them a probability of gain after costs, an expected gain and a risk. It buys when the futures cluster clearly above the cost, sells to cash when they stop doing so, and does nothing in between.

Two limits must be stated before the design, because they set the thresholds:

1. **A simulation built from past returns can only see what the past window contains.** Its direction comes from the recent drift (momentum) and from patterns that follow states similar to today's. It does not know news.
2. **High probabilities do not exist at short horizons.** For an asset with a trailing Sharpe ratio of 2 (a very strong trend), the probability of ending up after 5 trading days is about 61%, and after 20 days about 71%. An entry threshold of 80% would never fire. Realistic entry thresholds are 55% to 65%, and the edge comes from repeating a small advantage with controlled costs.

## 2. Data and time grid

- **Slots.** The regular session is cut into 7 slots: 9:30-10:00, then 10:00 to 16:00 by the hour. A return is the log change between consecutive slot closes; the first return of the day (previous 16:00 close to 10:00) contains the overnight gap. A trading day is 7 steps; early-close days have fewer.
- **Source.** Alpaca SIP bars, adjusted. Alpaca's `1Hour` bar stamped 9:00 mixes pre-market trades with the first half hour, so phase 3 stores `30Min` bars of the regular session and aggregates them to the 7 slots. History from 2016 (IBIT from 2024-01, ETHA from 2024-07).
- **Horizons.** 1 hour (1 step), 1 day (7 steps), 5 days (35 steps), as requested, plus 20 days (140 steps) as a candidate because of limit 2 above.
- **Delay.** The Basic plan delays SIP data 15 minutes. Each evaluation runs at about 20 minutes past the slot close and uses the bar that just closed. The starting price of the simulation is that close.

## 3. Simulation method

**Filtered historical simulation with a slot-aligned stationary bootstrap**, 5,000 paths per asset and evaluation.

1. *Filter.* Each past return is divided by the volatility known just before it: an EWMA volatility (same span as the system's `vol60`, expressed in steps) times a slot factor (the overnight slot and the first and last hours are more volatile than midday). The results are standardized shocks `z`.
2. *Resample.* A path is built by copying runs of consecutive shocks from the window. Runs have a random (geometric) length with a mean of 2 days, which keeps short-term autocorrelation and fat tails. A run always starts at a historical point with the same slot as the step being simulated, so an overnight shock is never placed at midday.
3. *Rescale.* Shocks are multiplied by today's volatility and the slot factor, and the EWMA is updated along the path, so volatility clusters as in the data.
4. *Same draws for all assets.* The 20 assets use the same historical dates in each path, which preserves their correlations. Decisions stay per asset; the joint paths are only used for the portfolio risk cap (section 6).
5. *Reproducible and pure.* `forecast/` receives the returns, the starting state and a seed (derived by the caller from date, slot and symbol). No network, clock or files. Reruns give identical numbers.

Why not a normal or GARCH model: hourly returns have fat tails, an overnight jump and intraday seasonality that a parametric model would have to assume; the bootstrap takes them from the data. Why not a plain bootstrap: without the volatility filter the simulation would use the average volatility of the window, too calm in a storm and too wild in a calm.

**Where direction comes from: two candidate generators, chosen by calibration.**

- **G1, drift.** The shocks keep a fraction `lambda` of their window mean (`lambda` = 0.5 or 1.0). This is trailing momentum, shrunk because a 3 to 12 month mean is a noisy estimate. `lambda` = 0 is the null model with no direction: it never trades and is the reference a real forecaster must beat.
- **G2, analogs.** As G1, but the runs are drawn preferentially from historical moments whose state resembled the current one (1-day and 5-day returns in volatility units, and current volatility against the window average; nearest neighbours in the same slot). It can express "after a drop like today's, this asset tended to rebound".

**Window.** 3, 6 or 12 months, chosen in phase 5.

## 4. What is read from the paths

For each asset and horizon `h`, with `c` the round-trip cost of the intended order as a fraction (commission both ways from `backtest/costs.py` plus the modeled spread):

| Quantity | Definition |
| --- | --- |
| `p_net` | share of paths with return above `c` |
| `p_up` | share of paths with return above 0 |
| `edge` | mean return minus `c` |
| `sigma` | standard deviation of the return |
| `es5` | mean of the worst 5% of the paths |

With 5,000 paths the sampling error of a probability is at most 0.7 points, far below the hysteresis gap.

## 5. Decision rule (per asset, long or cash)

The decision horizon `H` is 5 or 20 days (chosen in phase 5). The 1-day horizon confirms; the 1-hour horizon is recorded for calibration but does not trigger trades, because its typical move (4 to 40 bps) is below the cost.

| State | Condition | Action |
| --- | --- | --- |
| Flat | `p_net(H)` >= `p_enter` **and** `edge(H)` >= `k_cost` x `c` **and** `p_up(1d)` >= 0.50 | Enter (`MC_ENTER`) |
| Long | `p_up(H)` <= `p_exit` **or** `edge(H)` <= 0 | Exit to cash (`MC_EXIT`) |
| Any | otherwise | Nothing (`MC_HOLD`) |

- Candidates: `p_enter` in {0.55, 0.60, 0.65}; `p_exit` = `p_enter` - 0.10 (fixed gap = hysteresis); `k_cost` = 1 (expected gain at least twice the cost, since `edge` is already net).
- It may trade at any hourly evaluation, but the existing guard already forbids buying and selling the same asset on the same day and allows at most 12 orders a day. `paper_mc` keeps the guard as is, so an asset changes direction at most once a day.
- No shorts and no inverse funds.

## 6. Dynamic weights and risk limits

1. **Raw weight** for each asset with an entry or a held position: `kappa` x `edge(H)` / `sigma(H)`^2 (fractional Kelly on the simulated distribution; `kappa` in {0.25, 0.5}). Assets without a signal get 0.
2. **Caps:** 25% per asset; 30% for IBIT + ETHA; 35% for GLD + SLV + CPER; 40% for QQQ + IWM. Total at most 100% (no leverage, no margin); the rest stays in cash.
3. **Portfolio risk cap:** using the joint paths, if the 5-day loss exceeded in 1% of the paths is above 8% of equity, all weights are scaled down until it is not. The kill switch stays at 20% (`kill_switch_dd: 0.20`, existing code untouched); this cap is there so that one bad week does not get close to it.
4. **Minimum order 150 USD.** At 150 USD the IBKR minimum of 0.35 USD is 23 bps per side; at 100 USD it is 35. A target below the minimum is dropped (highest `edge`/`sigma` first when cash is short). This means **at most 6 positions at a time** with 1,000 USD, fewer than the 8 to 10 of the first estimate; it is the price of the commission floor.
5. **Resizing** an open position goes through the existing rebalance band (20% of target) and cost filter, so weights move with the simulation but do not churn.

## 7. Execution in paper (recommendation on the open decision)

**Recommended: (b) extend the shadow broker to fill on intraday bars**, not a second Alpaca account.

- The cost model chosen is IBKR. Alpaca paper fills at zero commission, so its equity would overstate the result unless the system kept a second, corrected ledger anyway.
- Shadow fills use the same rule as the backtest, so paper and backtest are directly comparable (the phase 4 exit criterion of the SPEC).
- No new account, keys or secrets.

Fill rule: an order decided at the evaluation after slot `k` closes is filled at the **open of slot `k+2`** (the first bar that starts after the decision), with the modeled spread and commission. Orders from the last slot and the evening run fill at the next session's open. The backtest uses exactly this rule. `SimBroker` is not a protected module; the change is additive and B and DIP keep their daily fills.

## 8. Calibration and go/no-go (phase 5)

Walk-forward only: at each evaluation the model sees data up to that slot and nothing later.

- **Split.** Selection period 2017-01 to 2022-12; holdout 2023-01 to 2026-10, run **once** with the configuration chosen on the selection period. Crypto ETFs join when they have a full window.
- **Stage A, forecast quality** (12 forecasters: window x generator x `lambda`). For every asset, horizon and evaluation: predicted `p_up` against what happened.
  - Reliability table in 10 bins: where the model said 60%, did it happen about 60% of the time?
  - Brier skill score against the base rate, and against the null model.
  - Coverage: the simulated 90% interval should contain the outcome 87% to 93% of the time.
  - Confidence intervals by block bootstrap, because horizons overlap.
- **Gate.** No paper trading unless, on the holdout and for the decision horizon: every bin with at least 200 cases is within 5 points of the diagonal, coverage is inside 87-93%, and the Brier skill score against the null model is above 0 with its 95% interval excluding 0. If no forecaster passes, the honest result is that this method has no edge on this universe, and we stop.
- **Stage B, trading rule** (12 rules: `H` x `p_enter` x `kappa`), only with the forecaster chosen in stage A: net return, maximum drawdown, number of trades, total costs, average exposure, time in cash. No comparison with indices. The SPEC alarm applies: an out-of-sample Sharpe above 1.5 is audited as a probable error before continuing.
- **Tests.** Anti-look-ahead (fake future bars change no past forecast); hypothesis properties on weights (never negative, never above caps, sum at most 1, no order below the minimum); seed reproducibility; a synthetic series with known drift whose probability the simulator must recover.

## 9. Module layout (phase 4)

```
src/stride/forecast/
    returns.py      # slot returns, EWMA volatility, slot factors, shocks
    simulate.py     # bootstrap generators G1 and G2 -> paths
    summary.py      # p_net, p_up, edge, sigma, es5 per horizon
    decide.py       # entry/exit state machine with hysteresis
    weights.py      # Kelly weights, caps, portfolio risk cap, minimum order
    calibration.py  # reliability, Brier, coverage
```

All pure. `run_daily.py` gets an hourly phase that only runs for a config with the forecast block; B and DIP have no such block and behave as today.

## 10. Open points for the owner

1. Approve this design, in particular: realistic thresholds (55-65%, not 80%), the 20-day horizon as a candidate, at most 6 positions of at least 150 USD, and the go/no-go gate.
2. Approve option (b), shadow fills on intraday bars.
3. The targets are handed to `execution/orders.py` in the form it already accepts. If that turns out to need any change in `orders.py` or `guard.py`, work stops and the change is proposed first.

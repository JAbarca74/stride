# Paper instance for the risk-allocation result (`paper_risk`): design

Status: **approved by the owner on 2026-11-12. Phase 5.1 is built (section 9): the instance exists and is disabled (`enabled: false`). It is not wired into the loop (phase 5.2) and not running (phase 5.3, the owner's go-ahead).** Phase 5 of `docs/design/mc_risk_allocation.md`. It follows gates R1 and R2 (`docs/design/mc_risk_calibration_results.md`, `docs/design/mc_risk_results.md`).

Rules this work keeps:

- `paper_mc`, its config and its database are not touched; its forward experiment goes on unchanged. B and DIP do not change behaviour.
- No protected module (`risk/leverage.py`, `risk/guard.py`, the kill switch, `execution/orders.py`, `execution/ibkr_broker.py`), no value of `config.yaml`.
- `mode` can only be `paper`. The account is a simulated ledger: no order reaches a broker.
- No figure is promised. The backtest numbers quoted are past measurements.

## 1. What would run, and what it can show

One variant, the only one gate R2 supports: **`IV_trend` on the 20 assets with 10,000 simulated USD** (`U20`). Inverse-volatility weights from the simulated 20-session risk, recomputed on the first session of each month; an asset is held only above its 200-day average; what is not held stays in cash.

What a forward run can and cannot show must be said first:

1. **It is slow.** The rule trades once a month. In the backtest it made 3.4% and 5.2% a year with single years between -2.7% and +13.5%. A few months of paper cannot confirm or refute that; they can only show that the instance does what the backtest does and stays inside its risk limits.
2. **It is not the real account size.** The result at 1,000 USD was poor for the pre-registered selection. A good paper run at 10,000 simulated USD says nothing about 1,000.
3. **It is not expected to beat being invested in a rising market.** In 2023-2026 it made less than a buy-and-hold of the same assets. Its measured value is in 2008, 2020 and 2022.

So the purpose of the instance is narrow: check that the live job reproduces the backtest, and observe the drawdown behaviour forward.

## 2. Parameters (fixed; the ones gate R2 was run with)

| Parameter | Value |
| --- | --- |
| Universe | the 20 assets of `docs/design/mc_universe.md` |
| Simulated capital | 10,000 USD |
| Allocator, brake | `IV`, `trend` with 200 closes |
| Simulation | no direction, 252-session window, 60-session volatility span, 2,000 joint paths, horizons 5 and 20 sessions |
| Rebalance | first session of each month (the first session the instance runs is also one) |
| Limits | 25% per asset; 40% QQQ + IWM, 35% GLD + SLV + CPER, 30% IBIT + ETHA; total 100%; simulated 5-session loss (1% tail) at most 8%; minimum order 150 USD; rebalance band 20%; 12 orders a day |
| Costs | commission and spread of `config.yaml` (IBKR Pro Tiered), read, not changed |
| Kill switch | 20% from the peak at a close: sell everything at the next open, stop for good; also a `KILL_SWITCH_RISK` file |
| Seed | fixed in the config; the seed of a session derives from it and the session date |

They live in a new `config.paper_risk.yaml`, each one commented, validated by its own model. Changing any of them restarts the experiment, as with `paper_mc`.

The gate showed that a plain 60-day volatility sizes as well as the simulated one. The instance keeps the simulated one because that is the variant that was measured; swapping it would be a new, untested variant.

## 3. How it runs

- **One evaluation per session.** When the daily bar of session `t` is in the database, the job fills the orders decided at `t-1` at the open of `t`, then decides at the close of `t`, then records equity and checks the kill switch. This is the order of the backtest (`run_risk_trading`), and the same `RiskTrader` object does it, so paper and backtest cannot drift apart.
- **Fills are known a day late.** An order decided after the close of `t` fills at the open of `t+1`, which the job only sees in the bar of `t+1`. On a simulated ledger that changes nothing but the time the fill is written.
- **Data.** Daily adjusted bars from Tiingo, the source B and DIP use, stored in the instance database (`data/paper_risk.db`). First run: about two years of history (252 sessions for the window, 200 for the trend). 20 requests on the first tick, then 20 short ones a day.
- **Missed days.** The job replays every session it has not processed, oldest first, with the seed of each session, so a job that was down for a week ends in the same account as one that ran every day. A test checks it, as `tests/test_run_mc.py` does for `paper_mc`.
- **Where.** A new job `python -m stride.run_risk --config config.paper_risk.yaml`, called from `scripts/actions_loop.sh` after the Monte Carlo line and in the same way: a failing tick prints a line and never fails the run. `scripts/save_state.sh` adds `data/paper_risk.db`. These two shared scripts are the only existing files that change.
- **Alerts.** `INFO` with the summary after each processed session; `KILL_SWITCH`, `DATA_FAILED`, `RUN_FAILED` as the other instances. `--report` prints capital, positions, weights, last trades and the simulated loss of what is held.

## 4. One point the backtest did not have to solve: dividends

The backtest uses adjusted prices throughout, which is a total-return series. A live ledger is different: when a fund pays a dividend, the provider rescales the whole past history, while the ledger holds a number of shares bought at the old scale. Left alone, the ledger would lose every dividend.

Proposed rule: when the stored history of a held asset is revised, the job multiplies the shares held by `old adjusted close / new adjusted close` on the last common day. That is the dividend reinvested at no cost, which is what the adjusted series assumes. The event is logged (`DIVIDEND_ADJUST`). A test builds a history, revises it, and checks that the ledger's equity follows the total-return series exactly.

## 5. Data safeguards

- A session is processed only when at least 80% of the as-of universe has its bar; otherwise the tick waits (`no_data`).
- A close-to-close move above 40% is treated as bad data and stops the tick with `DATA_FAILED` (the largest real one-day move in the 19-year history of these 20 assets is 28.5%).
- A bar is used only if its date is a NYSE session and not after the last completed session.

## 6. How it would be judged (fixed before any result)

Evaluated at 120 sessions, about six months. It continues only if **all** hold:

1. The 20% kill switch did not fire.
2. The drawdown from the peak never passed 12% (the backtest maximum was 9.3%; beyond 12% the instance is behaving unlike its own backtest).
3. Costs are below 0.5% of the starting capital over the 120 sessions (the backtest paid 0.3% to 0.4% a year).
4. A backtest replayed over the same sessions with the same data and seed ends in exactly the same account (the job does what was tested).

Not a criterion, and stated so: the sign of the return. Over six months it is noise for this rule. It is reported, with the equal-weight hold of the same assets next to it for context, and neither decides anything.

## 7. Phases, one pull request each

| Phase | Content | Ends with |
| --- | --- | --- |
| 5.0 | this design | owner approval and the decisions of section 8 |
| 5.1 | `src/stride/risk_paper/` (config model, daily store, job), `config.paper_risk.yaml` with `enabled: false`, tests (replay equals continuous run, dividend revision, data safeguards, no look-ahead, kill switch file) | merged, disabled: every tick is a no-op |
| 5.2 | wire into `scripts/actions_loop.sh` and `scripts/save_state.sh`; operating notes in Spanish | merged, still disabled |
| 5.3 | `enabled: true` | the owner's go-ahead, in its own pull request |

## 8. Decisions for the owner

1. **Run it at all, and when.** Now, next to `paper_mc`, or after the `paper_mc` verdict (about 2027-01-11). Running both adds one more job to each tick of the loop.
2. **10,000 simulated USD.** It is what the gate measured. It is not the size of the real account.
3. **Judge at 120 sessions with the four criteria of section 6**, or other criteria, fixed now.
4. **The dividend rule of section 4.**
5. Changing `scripts/actions_loop.sh` and `scripts/save_state.sh`, which B and DIP also go through.

## 9. As built

### Phase 5.1: the instance, disabled (2026-11-12)

| Piece | File |
| --- | --- |
| Config model | `src/stride/risk_paper/config.py` |
| Daily job (tick, report, self-check) | `src/stride/risk_paper/job.py` |
| Entry point | `src/stride/run_risk.py` (`python -m stride.run_risk`) |
| Instance config, `enabled: false` | `config.paper_risk.yaml` |
| State and one shared session step for backtest and job | `RiskTrader.to_state` / `from_state` and `step_session` in `src/stride/forecast/risk_walkforward.py` |
| Tests | `tests/test_run_risk.py` |

`run_risk_trading` now goes through `step_session`; the 114 runs of `docs/results/mc_risk/risk_runs.csv` were regenerated after the change and are identical. No other existing file changes; `scripts/actions_loop.sh` and `scripts/save_state.sh` are not touched until phase 5.2.

Where it differs from the sections above, or settles what they left open:

- **Seed.** The seed of a session derives from the config seed and the session date (`session_seed`), so it does not depend on how much history the panel holds. The research runs used the panel row instead; the two are different random draws of the same simulation, so the paper account is not a bit-for-bit continuation of `risk_runs.csv`.
- **Dividends (section 4).** The job does not watch the download for a revision. At the end of each tick it saves, per asset, the day and close of the last bar it used; at the next tick it compares that close with what the database now holds for the same day, and multiplies the shares held by `old / new` when they differ. That survives a tick that fails between the download and the save. Event `DIVIDEND_ADJUST`.
- **Criterion 4 (section 6).** `python -m stride.run_risk --verify` replays the account from its first session with the stored bars and compares the equity day by day. Without a dividend the replay is exact. After one, the stored history is rescaled, so share counts and per-share commissions differ by cents: the check passes when the largest gap is within 0.1%. A liquidation by a kill-switch file is not replayed.
- **Kill-switch file.** A `KILL_SWITCH_RISK` or `KILL_SWITCH` file is acted on after the sessions of the tick are replayed, so the sells fill at the next open, never at an open that had passed when the file was written.
- **History.** The first day of history is fixed at the first run (`history_days` before it) and nothing is deleted, so the replay can always be redone.
- **Tables** in `data/paper_risk.db`: `risk_equity` (per session), `risk_trades` (fills), `risk_log` (per session: rebalance, brake factors, simulated loss quantiles and weights of what is held), `risk_paper_state`, and the shared `prices`, `runs` (phase `risk`), `events`.

Checked on real data, outside the repository's instance: two sessions (2026-11-10 and 2026-11-11) in a temporary folder with Tiingo prices and no alerts. The account started in cash, bought the 9 assets above their 200-day average at the open of 2026-11-11 (53% invested, 6.20 USD of costs) and the replay matched exactly. That is a check of the plumbing, not a result.

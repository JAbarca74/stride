# DIP at 100 USD: the split order and the crypto that is never bought (design)

Status: **proposal, not approved** (2026-11-16). Nothing is built or changed. It follows a review of the paper databases on 2026-11-15, asked for by the owner.

Rules this work keeps:

- No value of `config.yaml`, `config.paper_b.yaml` or `config.paper_dip.yaml` changes without the owner; this document only names the values that would.
- `risk/guard.py` and `execution/orders.py` are protected. Part 2, option C, would touch `risk/guard.py` and needs its own explicit go-ahead.
- `paper_mc` and `paper_risk` are not affected by anything here.
- No number is promised. The figures below are measured on the paper database; the backtest figures of section 4 do not exist yet.

## 1. What the paper account did (measured, `data/paper_dip.db`, to 2026-11-13)

| Item | Value |
| --- | --- |
| Capital | 100 USD; equity 100.60 USD at the 2026-11-13 close (+0.60%) |
| Invested since | 2026-11-09. From 2026-10-23 to 11-05 the orders stayed `planned` (the morning phase did not run; fixed by PRs #89 and #90) |
| Held | SPY, 0.1078 shares bought at 742.32, 81% of equity |
| In cash | 19.30 USD: the 20% crypto target (ETHA 10%, IBIT 10%) was never bought |
| Gain on SPY | +1.30 USD |
| Commissions | 0.70 USD: **two** SPY orders of 40 USD at the 0.35 USD minimum of IBKR Tiered |
| Skipped buys | 22 `RESIZE_SKIPPED` events for each of ETHA and IBIT ("buy 10.00: cost"), one per session |

B pays no commission in its ledger (Alpaca paper), so the same split costs B nothing; this document is about DIP.

## 2. Cause 1: one 80 USD buy becomes two orders

`backtest/engine.py::split_orders` (used by `execution/orders.py`) cuts every order above `execution.max_order_pct_equity` x equity (0.60) into equal parts, so that a first unit larger than 60% of equity is not vetoed for ever by the guard (`risk/guard.py`, line 139). The SPY target of DIP is 80% of equity, so every build of the position is two orders, sent the same day.

- **It does not lower risk.** Both parts are sent on the same day and fill at the same open: the account ends with the same 80 USD of SPY. The only effect is a second minimum commission.
- **It costs 0.35 USD each time,** which is 0.35% of a 100 USD account. It happens again at every rebalance large enough to exceed 60 USD.

Three ways out, each of which is the owner's decision:

| Option | Change | Who decides | Effect |
| --- | --- | --- | --- |
| A | `execution.max_order_pct_equity: 1.0` in `config.paper_dip.yaml` only | owner: a config value | no split for an unlevered account; the guard check stays and is simply looser |
| B | `split_orders` sends the parts on consecutive sessions | not protected (`backtest/engine.py`) | the cost stays (two commissions); it only spreads the timing. **Not recommended** |
| C | the guard exempts from the 60% limit a buy that cannot use margin (`allow_margin: false`) and fits in cash | owner's explicit go-ahead: `risk/guard.py` is protected | same as A for DIP, without changing a value; B and the levered accounts keep the limit |

## 3. Cause 2: an entry that was missed is treated as a resize

The crypto entries were planned on 2026-10-23 as `ALLOCATE_IN` and never filled (the morning phase did not run). From the next day the strategy state already counted one unit of ETHA and one of IBIT (`signals.current_units = 1`), so the planner labelled the buy `HOLD`. `skip_costly_resizes` skips a `HOLD` buy whose cost is above `execution.max_resize_cost_pct` (0.5%): 0.35 USD on 10 USD is 3.5%. The account holds nothing of either, so the buy is not a resize, but it is skipped at every session and will be until the target reaches about 78 USD.

The same rule has a second consequence that matters at this size, worked out from the config and not yet measured:

- A HOLD top-up is skipped while `0.35 / order + 0.0005 > 0.005`, that is for every order below about **78 USD**.
- The 20 USD monthly deposit is therefore not invested while the gap to the targets stays below 78 USD. Cash can build up for about four months before one top-up goes through.

The fix is one condition: **a buy of a symbol the account does not hold is an entry, never a resize.** It goes in `skip_costly_resizes` (`backtest/engine.py`, not protected), so the backtest and the paper job change together.

- Effect on the paper account: at the next session DIP buys about 10 USD each of ETHA and IBIT. That costs 0.70 USD, and the 20% crypto allocation of the rule is then actually held.
- B is not affected: it holds every asset of its targets.
- Whether 0.70 USD for a 20 USD crypto sleeve is worth it at 100 USD is exactly what section 4 measures. It is not assumed here.

## 4. The test (fixed before any run)

Backtest of `config.paper_dip.yaml` with the engine of `--mode backtest`, 100 USD plus 20 USD a month, IBKR Tiered costs, on the stored history (`data/stride.db`, read-only), from 2017-01-01 to the last bar. The period has been used before for the strategy, so this is a measurement of an execution rule, **not** an out-of-sample result.

| Run | Change |
| --- | --- |
| `V0` | as today |
| `V1` | cause 2 fixed: an unheld symbol is never skipped as a resize |
| `V2` | option A: `max_order_pct_equity` 1.0 |
| `V3` | `V1` and `V2` together |

Reported for each: total and annual costs (USD and % of deposits), average cash share, number of skipped resizes, time-weighted return, maximum drawdown, the kill switch, and the year table.

A run is proposed for the paper account only if, against `V0`:

1. the kill switch does not fire;
2. the maximum drawdown is no more than 2 points higher;
3. the average cash share is lower;
4. the time-weighted return net of costs is at least `V0`'s;
5. costs per year rise by less than 0.5% of the capital deposited.

If no run meets all five, the result is that the current behaviour is the cheaper one at this size, and nothing changes.

**Not a criterion:** the return of one week in paper. Five invested sessions say nothing about a trend-following rule, and no weekly target is part of the SPEC (section 1: "guaranteeing profits" is out of scope).

## 5. Phases, one pull request each

| Phase | Content | Ends with |
| --- | --- | --- |
| C0 | this design | owner approval, and the choice between A and C |
| C1 | `V1`: the condition in `skip_costly_resizes`, with tests (an unheld symbol is never skipped; a held one keeps today's rule; B's backtest equity is unchanged) | merged only if section 4 supports it |
| C2 | `scripts/research/dip_costs.py`: the four runs and their table, `docs/results/dip_costs/` | the five criteria, met or not |
| C3 | option A (one config value, by the owner) or option C (guard change, with the owner's go-ahead and line-by-line review) | the paper account changes |

## 6. Decisions for the owner

1. Approve the test of section 4 and its five criteria.
2. Option A, B or C for the split.
3. Whether a fix that passes is applied to the running paper account of DIP. It changes its behaviour from that day; the account's history stays.

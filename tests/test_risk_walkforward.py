from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stride.forecast.daily import build_daily_panel
from stride.forecast.daily_risk import DailyRiskParams
from stride.forecast.params import WeightParams
from stride.forecast.returns import ReturnPanel
from stride.forecast.risk_walkforward import (
    RISK_ENTER,
    RISK_EXIT,
    RISK_REDUCE,
    RISK_RESIZE,
    RiskResult,
    RiskTrader,
    run_risk_trading,
)
from stride.forecast.risk_weights import ALLOCATORS, RiskRule
from stride.forecast.walkforward import CostModel

DAYS = pd.bdate_range("2019-01-01", periods=520)
FAST = DailyRiskParams(window_days=60, n_paths=300, horizons=(5, 20))
LIMITS = WeightParams(max_weight=0.5, risk_horizon=5, min_order_usd=50.0)
START = 220  # after the 200-day trend window and the 60-day history window


def _free(shares: float, price: float) -> float:
    return 0.0


def _flat_fee(shares: float, price: float) -> float:
    return 0.35


def _panel(
    count: int = 4, seed: int = 0, crash_from: int | None = None, falling: int | None = None
) -> ReturnPanel:
    """Random walks of 1% a day; optionally every asset falls 3% a day from a row,
    or one asset drifts down 0.4% a day from the start."""
    rng = np.random.default_rng(seed)
    bars = {}
    for index in range(count):
        ret = rng.normal(0.0003, 0.01, size=len(DAYS))
        if crash_from is not None:
            ret[crash_from:] = -0.03
        if falling == index:
            ret = ret - 0.004
        close = 100.0 * np.exp(np.cumsum(ret))
        opens = np.concatenate([close[:1], close[:-1]])
        bars[f"A{index}"] = pd.DataFrame({"open": opens, "close": close}, index=DAYS)
    return build_daily_panel(bars, DAYS)


def _run(
    panel: ReturnPanel,
    rules: list[RiskRule],
    *,
    stop: int | None = None,
    fee: bool = False,
    equity: float = 10_000.0,
) -> list[RiskResult]:
    costs = CostModel(np.full(len(panel.symbols), 5.0), _flat_fee if fee else _free)
    traders = [
        RiskTrader(rule, costs, initial_equity=equity, kill_switch_dd=0.20, size=len(panel.symbols))
        for rule in rules
    ]
    return run_risk_trading(panel, START, stop or len(panel), traders, params=FAST, base_seed=11)


def test_buys_only_at_the_monthly_rebalance_and_fills_at_the_next_open() -> None:
    panel = _panel()
    (result,) = _run(panel, [RiskRule("EW_none", "EW", "none", LIMITS)])
    trades = result.trading.trades
    assert not result.trading.killed
    first = trades[trades["reason"] == RISK_ENTER]
    assert len(first) == 4
    # Decided at the close of START, filled at the open of the next session.
    assert (pd.DatetimeIndex(first["end"]).tz_convert(None).normalize() == DAYS[START + 1]).all()
    assert first["price"].to_numpy() == pytest.approx(panel.open[START + 1] * 1.0005)
    # Every buy was decided on a rebalance day: the first evaluated session of a month.
    log = result.log.set_index("day")
    rebalance_days = set(log.index[log["rebalance"]])
    months = pd.DatetimeIndex(log.index).to_period("M")
    assert len(rebalance_days) == months.nunique()
    assert rebalance_days == set(log.groupby(months).head(1).index)
    buys = trades[trades["side"] == "buy"]
    previous = dict(zip(DAYS[1:], DAYS[:-1], strict=True))
    filled = pd.DatetimeIndex(buys["end"]).tz_convert(None).normalize()
    decided = [previous[day] for day in filled]
    assert set(decided) <= rebalance_days
    assert set(trades["reason"]) <= {RISK_ENTER, RISK_EXIT, RISK_REDUCE, RISK_RESIZE}
    # Fully invested at equal weight after the first fill, minus the spread.
    assert result.trading.equity["exposure"].iloc[2] == pytest.approx(1.0, abs=0.02)
    assert result.held.iloc[1].to_numpy() == pytest.approx(np.full(4, 0.25), abs=0.01)


def test_weights_never_exceed_the_limits() -> None:
    panel = _panel(count=3)
    limits = WeightParams(max_weight=0.25, risk_horizon=5, min_order_usd=50.0)
    rules = [RiskRule(f"{a}_none", a, "none", limits) for a in ALLOCATORS]
    for result in _run(panel, rules):
        log = result.log
        assert (log["base_total"] <= 0.75 + 1e-9).all()  # three assets capped at 25% each
        assert ((log["scale"] > 0) & (log["scale"] <= 1.0)).all()
        # Held weights drift with prices, but the account never borrows.
        assert (result.held.sum(axis=1) <= 1.0 + 1e-9).all()
        assert (result.trading.equity["exposure"] <= 1.0 + 1e-9).all()


def test_the_brakes_only_lower_exposure_between_rebalances() -> None:
    panel = _panel(seed=3, crash_from=330)
    rules = [RiskRule("EW_none", "EW", "none", LIMITS), RiskRule("EW_dd", "EW", "dd", LIMITS)]
    for result in _run(panel, rules):
        log = result.log
        month = pd.DatetimeIndex(log["day"]).to_period("M")
        for _, group in log.groupby(month):
            assert (np.diff(group["scale"].to_numpy()) <= 1e-12).all()
        assert (log["cap_scale"] <= 1.0).all()
        assert (log["scale"] <= log["cap_scale"] + 1e-12).all()


def test_drawdown_brake_reaches_cash_before_the_kill_switch() -> None:
    panel = _panel(seed=3, crash_from=330)
    loose = WeightParams(max_weight=0.5, risk_horizon=5, min_order_usd=50.0, max_portfolio_loss=5.0)
    plain, braked = _run(
        panel, [RiskRule("EW_none", "EW", "none", loose), RiskRule("EW_dd", "EW", "dd", loose)]
    )
    # Without a brake a 3% a day fall takes the account through the 20% limit.
    assert plain.trading.killed
    assert RISK_REDUCE in set(braked.trading.trades["reason"])
    assert not braked.trading.killed
    curve = braked.trading.equity["equity"]
    assert float((1.0 - curve / curve.cummax()).max()) < 0.20
    # Near the floor the targets fall below the minimum order: the variant ends in cash.
    assert braked.log["scale"].iloc[-1] < 0.05
    assert braked.trading.equity["exposure"].iloc[-1] == 0.0
    assert curve.iloc[-1] > plain.trading.equity["equity"].iloc[-1]


def test_trend_brake_leaves_a_falling_asset_in_cash() -> None:
    panel = _panel(seed=5, falling=2)
    plain, trend = _run(
        panel,
        [RiskRule("EW_none", "EW", "none", LIMITS), RiskRule("EW_trend", "EW", "trend", LIMITS)],
    )
    assert "A2" in set(plain.trading.trades["symbol"])
    assert "A2" not in set(trend.trading.trades["symbol"])
    # Its share is not handed to the other assets.
    first = trend.log[trend.log["rebalance"]].iloc[0]
    assert first["base_total"] <= 0.75 + 1e-9
    assert (trend.held["A2"] == 0.0).all()


def test_loss_cap_scales_a_wild_portfolio_down() -> None:
    panel = _panel(seed=7)
    tight = WeightParams(
        max_weight=0.5, risk_horizon=5, min_order_usd=50.0, max_portfolio_loss=0.01
    )
    capped, free = _run(
        panel,
        [RiskRule("EW_tight", "EW", "none", tight), RiskRule("EW_none", "EW", "none", LIMITS)],
    )
    assert (capped.log["cap_scale"] < 1.0).all()
    assert capped.trading.equity["exposure"].mean() < 0.6 * free.trading.equity["exposure"].mean()
    # The simulated 1% loss of what is actually held stays near the 1% cap.
    assert capped.log["held_q01"].iloc[5:].min() > -0.02


def test_no_look_ahead() -> None:
    """Rewriting every later bar changes nothing that was decided before."""
    panel = _panel(seed=9)
    cut = 400
    rules = [RiskRule("ERC_dd", "ERC", "dd", LIMITS), RiskRule("IV_trend", "IV", "trend", LIMITS)]
    full = _run(panel, rules)
    changed_close = panel.close.copy()
    changed_close[cut:] *= 0.5
    changed_open = panel.open.copy()
    changed_open[cut:] *= 0.5
    ret = np.full_like(changed_close, np.nan)
    ret[1:] = np.log(changed_close[1:] / changed_close[:-1])
    other = ReturnPanel(
        panel.symbols, panel.end, panel.day, panel.slot, changed_open, changed_close, ret
    )
    again = _run(other, rules)
    for before, after in zip(full, again, strict=True):
        a = before.log[before.log["row"] < cut].reset_index(drop=True)
        b = after.log[after.log["row"] < cut].reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b)
        old = before.trading.trades
        new = after.trading.trades
        limit = panel.end[cut]
        pd.testing.assert_frame_equal(
            old[old["end"] < limit].reset_index(drop=True),
            new[new["end"] < limit].reset_index(drop=True),
        )


def test_reproducible_and_last_row_places_no_order() -> None:
    panel = _panel(seed=2)
    rules = [RiskRule("IV_none", "IV", "none", LIMITS)]
    (one,) = _run(panel, rules, stop=300, fee=True)
    (two,) = _run(panel, rules, stop=300, fee=True)
    pd.testing.assert_frame_equal(one.trading.trades, two.trading.trades)
    pd.testing.assert_frame_equal(one.trading.equity, two.trading.equity)
    assert len(one.trading.equity) == 300 - START
    assert int(one.log["row"].max()) == 298  # nothing is decided at the last row
    assert (one.trading.trades["commission"] == 0.35).all()


def test_order_limit_spreads_a_rebalance_over_two_days() -> None:
    """The guard allows 12 orders a day; the rest of a rebalance goes the next day."""
    panel = _panel(count=14, seed=4)
    limits = WeightParams(max_weight=0.25, risk_horizon=5, min_order_usd=50.0)
    (result,) = _run(panel, [RiskRule("EW_none", "EW", "none", limits)], stop=START + 6)
    trades = result.trading.trades
    by_day = trades.groupby(pd.DatetimeIndex(trades["end"]).tz_convert(None).normalize()).size()
    assert by_day.loc[DAYS[START + 1]] == 12
    assert by_day.loc[DAYS[START + 2]] == 2
    assert set(trades["symbol"]) == set(panel.symbols)


def test_a_small_account_drops_targets_below_the_minimum_order() -> None:
    panel = _panel(count=4, seed=6)
    limits = WeightParams(max_weight=0.5, risk_horizon=5, min_order_usd=150.0)
    (result,) = _run(panel, [RiskRule("EW_none", "EW", "none", limits)], equity=500.0, fee=True)
    # 25% of 500 USD is 125 USD, below the minimum order: nothing is bought.
    assert result.trading.trades.empty
    assert (result.trading.equity["equity"] == 500.0).all()


def test_a_small_drawdown_does_not_liquidate_a_small_account() -> None:
    """The drawdown brake shrinks targets below the minimum order long before 15%;
    that alone must not sell whole positions."""
    panel = _panel(count=4, seed=6)
    limits = WeightParams(max_weight=0.5, risk_horizon=5, min_order_usd=150.0)
    (result,) = _run(
        panel, [RiskRule("EW_dd", "EW", "dd", limits)], equity=700.0, fee=True, stop=START + 40
    )
    log = result.log
    assert log["dd_scale"].min() < 1.0  # there was a drawdown
    assert log["dd_scale"].min() > 0.5  # but a small one
    assert RISK_EXIT not in set(result.trading.trades["reason"])
    assert result.trading.equity["exposure"].iloc[2:].min() > 0.9


def test_run_validates_its_range_and_horizons() -> None:
    panel = _panel()
    with pytest.raises(ValueError, match="start"):
        run_risk_trading(panel, 10, 5, [], params=FAST, base_seed=1)
    with pytest.raises(ValueError, match="horizons must contain"):
        run_risk_trading(
            panel,
            START,
            300,
            [],
            params=DailyRiskParams(window_days=60, horizons=(5,)),
            base_seed=1,
        )
    costs = CostModel(np.full(4, 5.0), _free)
    odd = RiskRule("x", "EW", "none", WeightParams(risk_horizon=35))
    trader = RiskTrader(odd, costs, initial_equity=1000.0, kill_switch_dd=0.2, size=4)
    with pytest.raises(ValueError, match="not simulated"):
        run_risk_trading(panel, START, 300, [trader], params=FAST, base_seed=1)

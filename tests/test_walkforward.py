from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stride.forecast.params import (
    SLOTS_PER_DAY,
    DecisionParams,
    ForecastParams,
    WeightParams,
)
from stride.forecast.returns import ReturnPanel
from stride.forecast.summary import Forecast, Summary
from stride.forecast.walkforward import (
    CostModel,
    Rule,
    forecast_at,
    forecast_table,
    last_slot_rows,
    realized,
    run_trading,
    seed_for,
    trading_metrics,
)

SYMBOLS = ("AAA", "BBB")


def _panel(
    days: int, seed: int = 0, drift: float = 0.0, closes: np.ndarray | None = None
) -> ReturnPanel:
    """A synthetic panel: random-walk closes, each open equal to the previous close."""
    steps = days * SLOTS_PER_DAY
    if closes is None:
        rng = np.random.default_rng(seed)
        closes = 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.002, size=(steps, 2)), axis=0))
    opens = np.vstack([closes[:1], closes[:-1]])
    ret = np.full_like(closes, np.nan)
    ret[1:] = np.log(closes[1:] / closes[:-1])
    sessions = pd.bdate_range("2024-01-02", periods=days)
    day = pd.DatetimeIndex(np.repeat(sessions.to_numpy(), SLOTS_PER_DAY))
    slot = np.tile(np.arange(SLOTS_PER_DAY, dtype=np.int64), days)
    end = pd.DatetimeIndex(day).tz_localize("UTC") + pd.to_timedelta(14 + slot, unit="h")
    return ReturnPanel(SYMBOLS, pd.DatetimeIndex(end), day, slot, opens, closes, ret)


def _free(shares: float, price: float) -> float:
    return 0.0


def _flat_fee(shares: float, price: float) -> float:
    return 0.35


NO_COSTS = CostModel(np.zeros(2), _free)
FAST = ForecastParams(window_days=20, n_paths=300, n_paths_risk=100, horizons=(1, 7, 35))


def test_seed_for_is_stable_and_row_dependent() -> None:
    assert seed_for(1, 10) == seed_for(1, 10)
    assert seed_for(1, 10) != seed_for(1, 11)
    assert seed_for(1, 10) != seed_for(2, 10)


def test_last_slot_rows_and_realized_returns() -> None:
    panel = _panel(3)
    assert last_slot_rows(panel).tolist() == [6, 13, 20]
    out = realized(panel, 6, 7)
    assert out[0] == pytest.approx(panel.close[13, 0] / panel.close[6, 0] - 1.0)
    assert np.isnan(realized(panel, 20, 1)).all()  # the future is not in the panel


def test_round_trip_cost_counts_commission_and_spread_both_ways() -> None:
    costs = CostModel(np.array([5.0, 10.0]), _flat_fee)
    out = costs.round_trip(np.array([50.0, float("nan")]), 150.0)
    assert out[0] == pytest.approx(2 * (0.35 / 150.0 + 0.0005))
    assert np.isnan(out[1])


def test_forecast_needs_a_full_window() -> None:
    panel = _panel(30)
    assert (
        forecast_at(panel, FAST.window_steps - 1, NO_COSTS, order_usd=150.0, params=FAST, seed=1)
        is None
    )
    assert (
        forecast_at(panel, FAST.window_steps, NO_COSTS, order_usd=150.0, params=FAST, seed=1)
        is not None
    )


def test_forecasts_do_not_change_when_the_future_changes() -> None:
    """Anti-look-ahead: rewriting every bar after an evaluation leaves it untouched."""
    panel = _panel(40, seed=3)
    rows = [200, 230]
    base = forecast_table(panel, rows, NO_COSTS, order_usd=150.0, params=FAST, seed=5)
    closes = panel.close.copy()
    closes[231:] *= 3.0  # absurd future prices after the last evaluation
    future = forecast_table(
        _panel(40, closes=closes), rows, NO_COSTS, order_usd=150.0, params=FAST, seed=5
    )
    said = ["row", "symbol", "horizon", "p_up", "p_net", "mean", "sigma", "q05", "q95"]
    pd.testing.assert_frame_equal(base[said], future[said])
    assert not np.allclose(base["realized"], future["realized"])


def test_forecast_table_has_one_line_per_asset_and_horizon_with_an_outcome() -> None:
    panel = _panel(30, seed=4)
    table = forecast_table(panel, [150, 205], NO_COSTS, order_usd=150.0, params=FAST, seed=1)
    # Row 205 is 4 steps from the end: only the 1-step horizon has an outcome.
    assert len(table[table["row"] == 150]) == 2 * 3
    assert table[table["row"] == 205]["horizon"].unique().tolist() == [1]
    assert (table["p_net"] <= table["p_up"]).all()
    assert forecast_table(panel, [5], NO_COSTS, order_usd=150.0, params=FAST, seed=1).empty


# --- trading ---------------------------------------------------------------------


def _summary(p: float, edge: float) -> Summary:
    two = np.full(2, 1.0)
    return Summary(
        mean=two * edge,
        sigma=two * 0.03,
        es5=two * -0.05,
        p_up=np.array([p, 0.4]),
        p_net=np.array([p, 0.4]),
        edge=np.array([edge, -0.01]),
        q05=two * -0.05,
        q95=two * 0.05,
    )


def _signal(p: float, edge: float = 0.02) -> Forecast:
    """A forecast that likes asset AAA with probability `p` and never asset BBB."""
    summary = _summary(p, edge)
    joint = np.zeros((10, 2))
    return Forecast(
        (7, 35), {7: summary, 35: summary}, np.array([True, True]), {7: joint, 35: joint}
    )


RULE = Rule(
    "test",
    DecisionParams(horizon=35, confirm_horizon=7, p_enter=0.60, p_exit=0.50),
    WeightParams(kappa=0.25, max_weight=0.25, min_order_usd=150.0),
)


def test_an_entry_fills_at_the_open_two_slots_later_with_costs() -> None:
    panel = _panel(3, seed=1)
    costs = CostModel(np.array([10.0, 10.0]), _flat_fee)
    result = run_trading(
        panel,
        [1],
        lambda row: _signal(0.7),
        [RULE],
        costs,
        initial_equity=1000.0,
        kill_switch_dd=0.20,
    )[0]
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade["symbol"] == "AAA"
    assert trade["side"] == "buy"
    assert trade["end"] == panel.end[3]  # decided after slot 1, filled at the open of slot 3
    assert trade["price"] == pytest.approx(panel.open[3, 0] * 1.001)
    assert trade["notional"] == pytest.approx(250.0)  # the 25% cap
    assert trade["commission"] == 0.35
    shares = 250.0 / trade["price"]
    expected = 1000.0 - 250.0 - 0.35 + shares * panel.close[20, 0]
    assert result.equity["equity"].iloc[-1] == pytest.approx(expected)
    assert result.equity["exposure"].iloc[-1] == pytest.approx(
        shares * panel.close[20, 0] / expected
    )


def test_an_order_after_the_close_fills_at_the_next_open() -> None:
    panel = _panel(3, seed=1)
    result = run_trading(
        panel,
        [6],
        lambda row: _signal(0.7),
        [RULE],
        NO_COSTS,
        initial_equity=1000.0,
        kill_switch_dd=0.20,
    )[0]
    assert result.trades.iloc[0]["end"] == panel.end[7]
    assert result.trades.iloc[0]["price"] == pytest.approx(panel.open[7, 0])


def test_hysteresis_and_exit() -> None:
    panel = _panel(5, seed=2)
    said = {1: 0.7, 8: 0.55, 15: 0.45}  # enter, keep (between thresholds), exit

    result = run_trading(
        panel,
        list(said),
        lambda row: _signal(said[row]),
        [RULE],
        NO_COSTS,
        initial_equity=1000.0,
        kill_switch_dd=0.20,
    )[0]
    assert result.trades["side"].tolist() == ["buy", "sell"]
    assert result.trades["reason"].tolist() == ["MC_ENTER", "MC_EXIT"]
    assert result.trades["end"].tolist() == [panel.end[3], panel.end[17]]
    assert result.equity["exposure"].iloc[-1] == 0.0


def test_the_same_asset_is_not_bought_and_sold_on_the_same_day() -> None:
    panel = _panel(3, seed=2)
    said = {0: 0.7, 3: 0.3, 8: 0.3}  # enter, then an exit signal the same day

    result = run_trading(
        panel,
        list(said),
        lambda row: _signal(said[row]),
        [RULE],
        NO_COSTS,
        initial_equity=1000.0,
        kill_switch_dd=0.20,
    )[0]
    # The same-day exit is refused (guard rule); the next day's exit goes through.
    assert result.trades["side"].tolist() == ["buy", "sell"]
    assert result.trades["end"].tolist() == [panel.end[2], panel.end[10]]


def test_kill_switch_liquidates_and_stops() -> None:
    closes = np.full((5 * SLOTS_PER_DAY, 2), 100.0)
    closes[7:, 0] = 10.0  # asset AAA collapses on day 2
    panel = _panel(5, closes=closes)
    all_in = Rule(
        "all-in",
        RULE.decision,
        WeightParams(kappa=5.0, max_weight=1.0, max_portfolio_loss=1.0, min_order_usd=0.0),
    )
    result = run_trading(
        panel,
        list(range(len(panel))),
        lambda row: _signal(0.7),
        [all_in],
        NO_COSTS,
        initial_equity=1000.0,
        kill_switch_dd=0.20,
    )[0]
    assert result.killed
    assert result.trades["reason"].tolist() == ["MC_ENTER", "KILL_SWITCH"]
    assert result.trades["end"].iloc[-1] == panel.end[14]  # the open after the breach
    assert result.equity["exposure"].iloc[-1] == 0.0
    assert result.equity["equity"].iloc[-1] == pytest.approx(100.0)


def test_rules_share_forecasts_but_keep_separate_books() -> None:
    panel = _panel(3, seed=1)
    strict = Rule("strict", DecisionParams(p_enter=0.80, p_exit=0.50), RULE.weights)
    calls: list[int] = []

    def forecasts(row: int) -> Forecast:
        calls.append(row)
        return _signal(0.7)

    loose_result, strict_result = run_trading(
        panel, [1], forecasts, [RULE, strict], NO_COSTS, initial_equity=1000.0, kill_switch_dd=0.20
    )
    assert calls == [1]
    assert len(loose_result.trades) == 1
    assert strict_result.trades.empty
    assert strict_result.equity["equity"].iloc[-1] == 1000.0


def test_trading_metrics() -> None:
    panel = _panel(3, seed=1)
    costs = CostModel(np.zeros(2), _flat_fee)
    result = run_trading(
        panel,
        [1],
        lambda row: _signal(0.7),
        [RULE],
        costs,
        initial_equity=1000.0,
        kill_switch_dd=0.20,
    )[0]
    metrics = trading_metrics(result, 1000.0)
    assert metrics["trades"] == 1.0
    assert metrics["costs_usd"] == pytest.approx(0.35)
    assert metrics["final_equity"] == pytest.approx(result.equity["equity"].iloc[-1])
    assert 0.0 <= metrics["max_drawdown"] < 0.05
    assert metrics["killed"] == 0.0
    assert metrics["days_in_cash_share"] == 0.0

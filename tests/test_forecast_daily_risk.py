from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stride.forecast.daily import build_daily_panel
from stride.forecast.daily_risk import (
    DailyRiskParams,
    calibration_cases,
    portfolio_quantiles,
    rate_interval,
    simulate_joint,
)
from stride.forecast.returns import ReturnPanel

DAYS = pd.bdate_range("2015-01-02", periods=1300)
FAST = DailyRiskParams(window_days=120, n_paths=400, horizons=(5, 20))


def _panel(seed: int = 0, late: int = 0) -> ReturnPanel:
    """Three assets with independent normal returns of 0.5%, 1% and 2% a day."""
    rng = np.random.default_rng(seed)
    bars = {}
    for symbol, vol in (("CALM", 0.005), ("MID", 0.01), ("WILD", 0.02)):
        days = DAYS[late:] if symbol == "WILD" else DAYS
        close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, vol, size=len(days))))
        bars[symbol] = pd.DataFrame({"open": close, "close": close}, index=days)
    return build_daily_panel(bars, DAYS)


def test_params_are_validated() -> None:
    with pytest.raises(ValueError, match="window_days"):
        DailyRiskParams(window_days=5)
    with pytest.raises(ValueError, match="n_paths"):
        DailyRiskParams(n_paths=0)
    with pytest.raises(ValueError, match="increasing"):
        DailyRiskParams(horizons=(20, 5))


def test_simulate_joint_is_reproducible_and_scaled_by_volatility() -> None:
    panel = _panel()
    valid, paths = simulate_joint(panel, 500, FAST, seed=3)
    assert valid.tolist() == [True, True, True]
    assert paths.shape == (2, 400, 3)
    again = simulate_joint(panel, 500, FAST, seed=3)[1]
    assert np.array_equal(paths, again)
    assert not np.array_equal(paths, simulate_joint(panel, 500, FAST, seed=4)[1])
    sigma = paths.std(axis=1)
    assert (sigma[1] > sigma[0]).all()
    assert sigma[0, 0] < sigma[0, 1] < sigma[0, 2]
    # No direction: the mean of the paths is small against their spread.
    assert np.abs(paths[1].mean(axis=0)).max() < 0.25 * sigma[1].min()


def test_simulate_joint_uses_the_as_of_universe() -> None:
    panel = _panel(late=400)
    valid, paths = simulate_joint(panel, 450, FAST, seed=1)
    assert valid.tolist() == [True, True, False]  # WILD has 50 returns, the window is 120
    assert paths.shape == (2, 400, 2)
    assert simulate_joint(panel, 520, FAST, seed=1)[0].tolist() == [True, True, True]
    none, empty = simulate_joint(panel, 50, FAST, seed=1)
    assert not none.any()
    assert empty.shape == (2, 400, 0)


def test_simulate_joint_does_not_look_ahead() -> None:
    panel = _panel()
    cut = panel.upto(501)
    assert np.array_equal(
        simulate_joint(panel, 500, FAST, seed=9)[1], simulate_joint(cut, 500, FAST, seed=9)[1]
    )


def test_portfolio_quantiles_are_ordered_and_follow_the_weights() -> None:
    rng = np.random.default_rng(0)
    joint = rng.normal(0.0, [0.01, 0.05], size=(5000, 2))
    q05, q01 = portfolio_quantiles(joint, np.array([0.5, 0.5]), (0.05, 0.01))
    assert q01 < q05 < 0
    calm = portfolio_quantiles(joint, np.array([1.0, 0.0]), (0.05,))[0]
    wild = portfolio_quantiles(joint, np.array([0.0, 1.0]), (0.05,))[0]
    assert wild < q05 < calm
    assert calm == pytest.approx(-1.645 * 0.01, rel=0.1)


def test_calibration_cases_on_data_the_model_fits() -> None:
    """Independent normal returns: coverage near 90%, loss exceedance near nominal."""
    panel = _panel(seed=5)
    rows = list(range(130, len(panel), 6))
    assets, portfolio = calibration_cases(panel, rows, FAST, base_seed=7)
    assert set(assets["symbol"]) == {"CALM", "MID", "WILD"}
    assert set(assets["horizon"]) == {5, 20}
    for horizon in (5, 20):
        cases = assets[assets["horizon"] == horizon]
        inside = (cases["realized"] >= cases["q05"]) & (cases["realized"] <= cases["q95"])
        assert 0.85 < inside.mean() < 0.95
    five = portfolio[portfolio["horizon"] == 5]
    assert (five["assets"] == 3).all()
    assert (five["q01"] < five["q05"]).all()
    assert 0.02 < (five["realized"] < five["q05"]).mean() < 0.09
    assert (five["realized"] < five["q01"]).mean() < 0.04
    # Rows whose outcome is not in the panel yet are left out.
    last_five = int(five["row"].max())
    assert last_five + 5 < len(panel)
    assert int(portfolio[portfolio["horizon"] == 20]["row"].max()) + 20 < len(panel)


def test_calibration_cases_realized_is_the_equal_weight_hold() -> None:
    panel = _panel(seed=2)
    assets, portfolio = calibration_cases(panel, [300], FAST, base_seed=1)
    for horizon in (5, 20):
        single = assets[assets["horizon"] == horizon]["realized"].to_numpy()
        expected = panel.close[300 + horizon] / panel.close[300] - 1.0
        assert single == pytest.approx(expected)
        held = portfolio[portfolio["horizon"] == horizon]["realized"].to_numpy()
        assert held[0] == pytest.approx(expected.mean())


def test_calibration_cases_do_not_look_ahead() -> None:
    panel = _panel(seed=4)
    full_assets, full_portfolio = calibration_cases(panel, [300, 400], FAST, base_seed=1)
    cut_assets, cut_portfolio = calibration_cases(panel.upto(411), [300, 400], FAST, base_seed=1)
    # The simulated quantiles of row 400 are the same whether or not later bars exist.
    key = (full_portfolio["row"] == 400) & (full_portfolio["horizon"] == 5)
    cut_key = (cut_portfolio["row"] == 400) & (cut_portfolio["horizon"] == 5)
    assert full_portfolio[key]["q01"].to_numpy() == pytest.approx(
        cut_portfolio[cut_key]["q01"].to_numpy()
    )
    assert len(cut_assets) < len(full_assets)  # the 20-day outcome of row 400 is missing


def test_rate_interval_brackets_the_rate_and_resamples_groups() -> None:
    rng = np.random.default_rng(0)
    hits = (rng.random(2000) < 0.05).astype(float)
    groups = np.repeat(np.arange(100), 20)
    out = rate_interval(hits, groups, np.random.default_rng(1))
    assert out.cases == 2000
    assert out.rate == pytest.approx(hits.mean())
    assert out.low < out.rate < out.high
    # Hits that come in clusters give a wider interval than the same hits spread out.
    clustered = np.zeros(2000)
    clustered[:100] = 1.0
    spread = np.zeros(2000)
    spread[::20] = 1.0
    wide = rate_interval(clustered, groups, np.random.default_rng(1))
    narrow = rate_interval(spread, groups, np.random.default_rng(1))
    assert wide.rate == narrow.rate == pytest.approx(0.05)
    assert (wide.high - wide.low) > (narrow.high - narrow.low)


def test_rate_interval_of_nothing() -> None:
    out = rate_interval(np.array([]), np.array([]), np.random.default_rng(0))
    assert out.cases == 0
    assert np.isnan(out.rate)

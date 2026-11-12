from __future__ import annotations

from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stride.forecast.calibration import (
    brier_score,
    brier_skill,
    brier_skill_interval,
    coverage,
    reliability_table,
)
from stride.forecast.decide import MC_ENTER, MC_EXIT, MC_HOLD, decide, long_after
from stride.forecast.params import (
    SLOTS_PER_DAY,
    DecisionParams,
    ForecastParams,
    WeightParams,
)
from stride.forecast.returns import (
    Filtered,
    build_panel,
    ewma_variance,
    slot_factors,
    standardize,
)
from stride.forecast.simulate import analog_weights, simulate
from stride.forecast.summary import Forecast, Summary, run_forecast, summarize
from stride.forecast.weights import portfolio_loss, target_weights

DAYS = 252


def _slots(days: int) -> np.ndarray:
    return np.tile(np.arange(SLOTS_PER_DAY, dtype=np.int64), days)


def _normal_returns(days: int, assets: int, mean: float, sd: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(mean, sd, size=(days * SLOTS_PER_DAY, assets))


# --- returns ---------------------------------------------------------------------


def _grid(days: list[str]) -> pd.DataFrame:
    rows = [
        (pd.Timestamp(day), slot, pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=14 + slot))
        for day in days
        for slot in range(SLOTS_PER_DAY)
    ]
    return pd.DataFrame(rows, columns=["day", "slot", "end"])


def test_build_panel_aligns_fills_gaps_and_marks_unlisted_history() -> None:
    grid = _grid(["2026-10-15", "2026-10-16"])
    full = grid[["day", "slot"]].assign(open=100.0, close=np.linspace(100.0, 113.0, 14))
    gappy = full.drop(index=[3]).iloc[2:]  # listed from slot 2; no trades in slot 3
    panel = build_panel({"AAA": full, "BBB": gappy}, grid)
    assert panel.symbols == ("AAA", "BBB")
    assert len(panel) == 14
    assert np.isnan(panel.ret[0]).all()  # no previous close
    assert panel.ret[1, 0] == pytest.approx(np.log(101.0 / 100.0))
    assert np.isnan(panel.close[:2, 1]).all()
    assert np.isnan(panel.ret[:3, 1]).all()
    assert panel.ret[3, 1] == 0.0  # repeated close
    assert np.isnan(panel.open[3, 1])
    assert panel.ret[4, 1] == pytest.approx(np.log(104.0 / 102.0))
    assert len(panel.upto(5)) == 5
    middle = panel.window(3, 9)
    assert len(middle) == 6
    assert middle.end[0] == panel.end[3]
    assert middle.close[0, 0] == panel.close[3, 0]
    assert panel.upto(5).ret.shape == (5, 2)


def test_slot_factors_measure_the_size_of_each_slot() -> None:
    slot = _slots(200)
    ret = _normal_returns(200, 1, 0.0, 0.001, seed=1)
    ret[slot == 0] *= 3.0  # a large overnight slot
    factors = slot_factors(ret, slot)
    assert factors[0, 0] > 2.0 * factors[3, 0]
    assert np.mean(factors[slot, 0] ** 2) == pytest.approx(1.0, rel=0.05)


def test_ewma_variance_row_t_ignores_return_t_and_later() -> None:
    scaled = _normal_returns(30, 1, 0.0, 0.01, seed=2)
    base = ewma_variance(scaled, span=20)
    changed = scaled.copy()
    changed[100:] *= 5.0
    after = ewma_variance(changed, span=20)
    # The seed (row 0) is the window mean; rows 1..100 then only differ through it.
    drift = (after[:101] - base[:101]) / (after[0] - base[0])
    assert (np.diff(drift[:, 0]) <= 1e-12).all()  # the seed's influence only decays
    assert after[101, 0] > base[101, 0]
    with pytest.raises(ValueError, match="span"):
        ewma_variance(scaled, span=0)


def test_standardize_gives_unit_scale_shocks_and_rejects_gaps() -> None:
    slot = _slots(DAYS)
    ret = _normal_returns(DAYS, 2, 0.0, 0.002, seed=3)
    filtered = standardize(ret, slot, span=140)
    assert filtered.shocks.shape == ret.shape
    assert filtered.shocks.std(axis=0) == pytest.approx([1.0, 1.0], rel=0.1)
    assert filtered.next_variance.shape == (2,)
    ret[5, 0] = np.nan
    with pytest.raises(ValueError, match="missing returns"):
        standardize(ret, slot, span=140)


def test_standardize_flat_series_gives_zero_shocks() -> None:
    filtered = standardize(np.zeros((70, 1)), _slots(10), span=14)
    assert (filtered.shocks == 0.0).all()


# --- simulate --------------------------------------------------------------------


def _flat_filtered(shocks: np.ndarray) -> Filtered:
    steps, assets = shocks.shape
    return Filtered(
        shocks=shocks,
        variance=np.ones((steps + 1, assets)),
        factors=np.ones((SLOTS_PER_DAY, assets)),
        scaled=shocks,
    )


def test_simulate_never_puts_a_shock_in_the_wrong_slot() -> None:
    slot = _slots(50)
    shocks = (slot + 1.0).reshape(-1, 1)  # a shock's value names its slot
    paths = simulate(
        _flat_filtered(shocks),
        slot,
        start_slot=3,
        horizons=(1, 7, 14),
        n_paths=300,
        mean_block=2.0,
        drift_shrink=1.0,
        vol_span=10**9,  # volatility stays at 1
        rng=np.random.default_rng(0),
    )
    assert paths.shape == (3, 300, 1)
    assert paths[0, :, 0] == pytest.approx(4.0)  # slot 3
    assert paths[1, :, 0] == pytest.approx(28.0, rel=1e-6)  # one of each slot
    assert paths[2, :, 0] == pytest.approx(56.0, rel=1e-6)


def test_simulate_is_reproducible_and_seed_dependent() -> None:
    slot = _slots(DAYS)
    filtered = standardize(_normal_returns(DAYS, 3, 0.0, 0.002, seed=4), slot, span=140)

    def run(seed: int) -> np.ndarray:
        return simulate(
            filtered,
            slot,
            start_slot=0,
            horizons=(7, 35),
            n_paths=200,
            mean_block=14.0,
            drift_shrink=0.5,
            vol_span=140,
            rng=np.random.default_rng(seed),
        )

    assert np.array_equal(run(7), run(7))
    assert not np.array_equal(run(7), run(8))


def test_simulate_recovers_a_known_drift() -> None:
    mean, sd, horizon = 0.0004, 0.002, 35
    slot = _slots(DAYS)
    ret = _normal_returns(DAYS, 1, mean, sd, seed=5)
    filtered = standardize(ret, slot, span=420)
    paths = simulate(
        filtered,
        slot,
        start_slot=0,
        horizons=(horizon,),
        n_paths=20_000,
        mean_block=14.0,
        drift_shrink=1.0,
        vol_span=420,
        rng=np.random.default_rng(1),
    )
    sample_mean, sample_sd = ret.mean(), ret.std()
    expected = NormalDist().cdf(sample_mean * horizon / (sample_sd * horizon**0.5))
    assert (paths[0, :, 0] > 0).mean() == pytest.approx(expected, abs=0.03)
    assert expected > 0.8  # the test would be empty with a weak drift


def test_simulate_without_drift_has_no_direction() -> None:
    slot = _slots(DAYS)
    filtered = standardize(_normal_returns(DAYS, 1, 0.0004, 0.002, seed=5), slot, span=420)
    paths = simulate(
        filtered,
        slot,
        start_slot=0,
        horizons=(35,),
        n_paths=20_000,
        mean_block=14.0,
        drift_shrink=0.0,
        vol_span=420,
        rng=np.random.default_rng(1),
    )
    assert (paths[0, :, 0] > 0).mean() == pytest.approx(0.5, abs=0.03)


def test_simulate_keeps_cross_asset_correlation() -> None:
    slot = _slots(DAYS)
    common = _normal_returns(DAYS, 1, 0.0, 0.002, seed=6)
    ret = np.hstack([common, common + _normal_returns(DAYS, 1, 0.0, 0.0005, seed=7)])
    filtered = standardize(ret, slot, span=420)
    paths = simulate(
        filtered,
        slot,
        start_slot=0,
        horizons=(35,),
        n_paths=5000,
        mean_block=14.0,
        drift_shrink=0.0,
        vol_span=420,
        rng=np.random.default_rng(2),
    )
    assert np.corrcoef(paths[0, :, 0], paths[0, :, 1])[0, 1] > 0.9


def test_simulate_rejects_bad_inputs() -> None:
    filtered = _flat_filtered(np.zeros((3, 1)))
    with pytest.raises(ValueError, match="shorter than one session"):
        simulate(
            filtered,
            np.arange(3, dtype=np.int64),
            start_slot=0,
            horizons=(1,),
            n_paths=1,
            mean_block=2.0,
            drift_shrink=0.0,
            vol_span=10,
            rng=np.random.default_rng(0),
        )


def test_analog_weights_point_at_successors_of_same_slot_moments() -> None:
    slot = _slots(DAYS)
    filtered = standardize(_normal_returns(DAYS, 1, 0.0, 0.002, seed=8), slot, span=140)
    weights = analog_weights(filtered, slot, 0, share=0.1, minimum=30)
    assert weights is not None
    assert weights.sum() == pytest.approx(1.0)
    chosen = np.flatnonzero(weights)
    assert len(chosen) >= 30
    assert (slot[chosen - 1] == slot[-1]).all()
    assert (slot[chosen] == (slot[-1] + 1) % SLOTS_PER_DAY).all()


def test_analog_weights_prefer_similar_states() -> None:
    slot = _slots(DAYS)
    ret = _normal_returns(DAYS, 1, 0.0, 0.002, seed=9)
    ret[-7:] = 0.01  # the last day was a very strong rally
    filtered = standardize(ret, slot, span=140)
    weights = analog_weights(filtered, slot, 0, share=0.1, minimum=30)
    assert weights is not None
    day_return = np.convolve(filtered.scaled[:, 0], np.ones(7), mode="full")[: len(slot)]
    chosen = np.flatnonzero(weights) - 1
    eligible = np.flatnonzero(slot[:-1] == slot[-1])[5:]
    assert day_return[chosen].mean() > day_return[eligible].mean()


def test_analog_weights_need_enough_history() -> None:
    slot = _slots(4)
    filtered = standardize(_normal_returns(4, 1, 0.0, 0.002, seed=8), slot, span=14)
    assert analog_weights(filtered, slot, 0, share=0.1, minimum=30) is None


# --- summary ---------------------------------------------------------------------


def test_summarize_reads_probabilities_and_tail() -> None:
    paths = np.log1p(np.array([[-0.10], [-0.02], [0.01], [0.03]]))
    summary = summarize(paths, np.array([0.02]))
    assert summary.p_up[0] == 0.5
    assert summary.p_net[0] == 0.25
    assert summary.mean[0] == pytest.approx(-0.02)
    assert summary.edge[0] == pytest.approx(-0.04)
    assert summary.es5[0] == pytest.approx(-0.10)


FAST = ForecastParams(window_days=63, n_paths=400, n_paths_risk=200, horizons=(1, 7, 35))


def test_run_forecast_is_deterministic_and_skips_unlisted_assets() -> None:
    slot = _slots(100)
    ret = _normal_returns(100, 3, 0.0002, 0.002, seed=10)
    ret[:500, 2] = np.nan  # listed too recently for a 63-day window
    cost = np.array([0.003, 0.003, 0.003])
    first = run_forecast(ret, slot, cost, FAST, seed=11)
    again = run_forecast(ret, slot, cost, FAST, seed=11)
    other = run_forecast(ret, slot, cost, FAST, seed=12)
    assert first.valid.tolist() == [True, True, False]
    assert np.isnan(first.by_horizon[35].p_up[2])
    assert (first.joint[35][:, 2] == 0.0).all()
    assert np.array_equal(first.by_horizon[35].p_up[:2], again.by_horizon[35].p_up[:2])
    assert not np.array_equal(first.by_horizon[35].mean[:2], other.by_horizon[35].mean[:2])
    main = first.by_horizon[35]
    assert main.edge[0] == pytest.approx(main.mean[0] - 0.003)
    assert main.p_net[0] <= main.p_up[0]
    assert main.q05[0] < main.q95[0]


def test_run_forecast_uses_only_the_window() -> None:
    slot = _slots(100)
    ret = _normal_returns(100, 1, 0.0002, 0.002, seed=13)
    older = ret.copy()
    older[: len(ret) - FAST.window_steps] = 0.5  # garbage before the window
    cost = np.array([0.0])
    a = run_forecast(ret, slot, cost, FAST, seed=1).by_horizon[7]
    b = run_forecast(older, slot, cost, FAST, seed=1).by_horizon[7]
    assert a.p_up[0] == b.p_up[0]


def test_run_forecast_analog_generator_runs_and_keeps_joint_paths_driftless() -> None:
    slot = _slots(100)
    ret = _normal_returns(100, 2, 0.0005, 0.002, seed=14)
    params = ForecastParams(
        window_days=63, generator="analog", n_paths=400, n_paths_risk=200, horizons=(7, 35)
    )
    forecast = run_forecast(ret, slot, np.zeros(2), params, seed=3)
    assert forecast.joint[35].shape == (200, 2)
    assert np.isfinite(forecast.by_horizon[35].p_up).all()
    assert abs(float(forecast.joint[35].mean())) < 0.02


def test_run_forecast_needs_a_full_window_and_handles_no_valid_asset() -> None:
    slot = _slots(100)
    with pytest.raises(ValueError, match="not enough history"):
        run_forecast(np.zeros((10, 1)), slot[:10], np.zeros(1), FAST, seed=0)
    empty = run_forecast(np.full((700, 1), np.nan), slot, np.zeros(1), FAST, seed=0)
    assert not empty.valid.any()
    assert np.isnan(empty.by_horizon[35].p_up[0])


def test_params_reject_bad_values() -> None:
    with pytest.raises(ValueError, match="hysteresis"):
        DecisionParams(p_enter=0.5, p_exit=0.6)
    with pytest.raises(ValueError, match="drift_shrink"):
        ForecastParams(drift_shrink=1.5)
    with pytest.raises(ValueError, match="increasing"):
        ForecastParams(horizons=(7, 1))
    with pytest.raises(ValueError, match="group caps without a group"):
        WeightParams(group_caps={"crypto": 0.3})
    with pytest.raises(ValueError, match="kappa"):
        WeightParams(kappa=0.0)


# --- decide ----------------------------------------------------------------------


def _summary(
    p_net: list[float],
    p_up: list[float],
    edge: list[float],
    cost: float = 0.004,
    sigma: float = 0.03,
) -> Summary:
    size = len(p_net)
    edges = np.array(edge, dtype=float)
    return Summary(
        mean=edges + cost,
        sigma=np.full(size, sigma),
        es5=np.full(size, -0.06),
        p_up=np.array(p_up, dtype=float),
        p_net=np.array(p_net, dtype=float),
        edge=edges,
        q05=np.full(size, -0.05),
        q95=np.full(size, 0.05),
    )


def _forecast(
    main: Summary, confirm: Summary | None = None, joint: np.ndarray | None = None
) -> Forecast:
    size = len(main.mean)
    paths = np.zeros((10, size)) if joint is None else joint
    return Forecast(
        horizons=(7, 35),
        by_horizon={7: confirm if confirm is not None else main, 35: main},
        valid=np.isfinite(main.p_net),
        joint={7: paths, 35: paths},
    )


RULE = DecisionParams(horizon=35, confirm_horizon=7, p_enter=0.60, p_exit=0.50, k_cost=1.0)


def test_decide_enters_only_when_every_condition_holds() -> None:
    main = _summary(
        p_net=[0.62, 0.59, 0.62, 0.62, float("nan")],
        p_up=[0.66, 0.66, 0.66, 0.66, float("nan")],
        edge=[0.006, 0.006, 0.003, 0.006, float("nan")],
    )
    confirm = _summary(p_net=[0.5] * 5, p_up=[0.55, 0.55, 0.55, 0.45, 0.55], edge=[0.0] * 5)
    held = np.zeros(5, dtype=bool)
    actions = decide(_forecast(main, confirm), held, RULE)
    # 1: probability too low; 2: edge below the cost; 3: 1-day points down; 4: no forecast.
    assert actions == [MC_ENTER, MC_HOLD, MC_HOLD, MC_HOLD, MC_HOLD]
    assert long_after(actions, held).tolist() == [True, False, False, False, False]


def test_decide_exits_on_low_probability_lost_edge_or_missing_forecast() -> None:
    main = _summary(
        p_net=[0.50, 0.40, 0.55, float("nan")],
        p_up=[0.55, 0.50, 0.58, float("nan")],
        edge=[0.001, 0.001, -0.001, float("nan")],
    )
    held = np.ones(4, dtype=bool)
    actions = decide(_forecast(main), held, RULE)
    assert actions == [MC_HOLD, MC_EXIT, MC_EXIT, MC_EXIT]
    assert long_after(actions, held).tolist() == [True, False, False, False]


def test_decide_hysteresis_keeps_the_current_state_between_thresholds() -> None:
    main = _summary(p_net=[0.55], p_up=[0.56], edge=[0.006])
    forecast = _forecast(main)
    assert decide(forecast, np.array([False]), RULE) == [MC_HOLD]  # not enough to enter
    assert decide(forecast, np.array([True]), RULE) == [MC_HOLD]  # not enough to exit


# --- weights ---------------------------------------------------------------------

SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
SIZING = WeightParams(
    kappa=0.25,
    max_weight=0.25,
    groups={"pair": ("AAA", "BBB")},
    group_caps={"pair": 0.30},
    risk_horizon=35,
    max_portfolio_loss=0.08,
    min_order_usd=150.0,
)


def test_weights_cap_per_asset_group_and_total() -> None:
    main = _summary(p_net=[0.7] * 6, p_up=[0.7] * 6, edge=[0.02] * 6)
    weights = target_weights(
        _forecast(main), np.ones(6, dtype=bool), SYMBOLS, equity=1000.0, horizon=35, params=SIZING
    )
    assert weights.sum() <= 1.0 + 1e-12
    assert weights.max() <= 0.25 + 1e-12
    assert weights[:2].sum() <= 0.30 + 1e-12
    assert ((weights == 0) | (weights * 1000.0 >= 150.0)).all()


def test_weights_follow_the_simulation_and_skip_assets_without_a_signal() -> None:
    main = _summary(p_net=[0.7] * 3, p_up=[0.7] * 3, edge=[0.0006, 0.0003, 0.02])
    long = np.array([True, True, False])
    weights = target_weights(
        _forecast(main), long, SYMBOLS[:3], equity=10_000.0, horizon=35, params=SIZING
    )
    assert weights[2] == 0.0  # not long
    assert weights[0] == pytest.approx(2 * weights[1])  # twice the edge, same risk


def test_weights_drop_the_weakest_when_orders_would_be_too_small() -> None:
    edge = [0.02, 0.019, 0.018, 0.017, 0.016, 0.015]
    main = _summary(p_net=[0.7] * 6, p_up=[0.7] * 6, edge=edge)
    params = WeightParams(max_weight=0.25, min_order_usd=150.0)
    weights = target_weights(
        _forecast(main), np.ones(6, dtype=bool), SYMBOLS, equity=600.0, horizon=35, params=params
    )
    # 600 USD holds at most four 150 USD orders: the four strongest signals.
    assert (weights > 0).tolist() == [True, True, True, True, False, False]
    assert weights[:4] == pytest.approx([0.25] * 4)


def test_weights_respect_the_portfolio_loss_cap() -> None:
    main = _summary(p_net=[0.7] * 2, p_up=[0.7] * 2, edge=[0.02, 0.02])
    rng = np.random.default_rng(0)
    crash = rng.normal(0.0, 0.2, size=(4000, 1)) * np.ones((1, 2))  # two assets, one risk
    forecast = _forecast(main, joint=crash)
    params = WeightParams(max_weight=0.5, max_portfolio_loss=0.08)
    weights = target_weights(
        forecast, np.ones(2, dtype=bool), SYMBOLS[:2], equity=10_000.0, horizon=35, params=params
    )
    assert 0 < weights.sum() < 1.0
    assert portfolio_loss(weights, crash, 0.01) == pytest.approx(0.08, rel=1e-6)


def test_weights_are_zero_without_equity() -> None:
    main = _summary(p_net=[0.7], p_up=[0.7], edge=[0.02])
    weights = target_weights(
        _forecast(main), np.array([True]), SYMBOLS[:1], equity=0.0, horizon=35, params=SIZING
    )
    assert weights.tolist() == [0.0]


@settings(max_examples=200, deadline=None)
@given(
    edge=st.lists(st.floats(-0.02, 0.05), min_size=6, max_size=6),
    sigma=st.floats(0.005, 0.15),
    long=st.lists(st.booleans(), min_size=6, max_size=6),
    equity=st.floats(50.0, 50_000.0),
    seed=st.integers(0, 10_000),
)
def test_weights_invariants(
    edge: list[float], sigma: float, long: list[bool], equity: float, seed: int
) -> None:
    """Whatever the simulation says, the targets can never break a limit."""
    main = _summary(p_net=[0.6] * 6, p_up=[0.6] * 6, edge=edge, sigma=sigma)
    joint = np.random.default_rng(seed).normal(0.0, sigma, size=(500, 6))
    held = np.array(long)
    weights = target_weights(
        _forecast(main, joint=joint), held, SYMBOLS, equity=equity, horizon=35, params=SIZING
    )
    assert (weights >= 0).all()
    assert (weights[~held] == 0).all()
    assert (weights[np.array(edge) <= 0] == 0).all()
    assert weights.max() <= SIZING.max_weight + 1e-12
    assert weights.sum() <= SIZING.max_total + 1e-12
    assert weights[:2].sum() <= SIZING.group_caps["pair"] + 1e-12
    assert ((weights == 0) | (weights * equity >= SIZING.min_order_usd - 1e-9)).all()
    assert portfolio_loss(weights, joint, SIZING.risk_quantile) <= SIZING.max_portfolio_loss + 1e-9


# --- calibration -----------------------------------------------------------------


def test_reliability_table_of_a_calibrated_forecast_is_on_the_diagonal() -> None:
    rng = np.random.default_rng(0)
    predicted = rng.uniform(0.0, 1.0, size=50_000)
    happened = (rng.uniform(size=50_000) < predicted).astype(float)
    table = reliability_table(predicted, happened)
    assert len(table) == 10
    assert table["count"].sum() == 50_000
    assert table["gap"].abs().max() < 0.03


def test_reliability_table_shows_overconfidence_and_skips_empty_bins() -> None:
    predicted = np.array([0.85] * 100 + [1.0] * 10)
    happened = np.array([1.0] * 50 + [0.0] * 50 + [1.0] * 10)
    table = reliability_table(predicted, happened)
    assert table["low"].round(1).tolist() == [0.8, 0.9]
    assert table["gap"].iloc[0] == pytest.approx(-0.35)
    assert table["count"].tolist() == [100, 10]


def test_brier_scores_and_skill() -> None:
    happened = np.array([1.0, 0.0, 1.0, 0.0])
    sharp = np.array([0.9, 0.1, 0.8, 0.2])
    base = np.full(4, 0.5)
    assert brier_score(base, happened) == pytest.approx(0.25)
    assert brier_score(sharp, happened) == pytest.approx(0.025)
    assert brier_skill(sharp, happened, base) == pytest.approx(0.9)
    assert brier_skill(base, happened, base) == 0.0
    assert brier_skill(sharp, happened, happened) == 0.0  # a perfect reference


def test_coverage_counts_outcomes_inside_the_interval() -> None:
    low, high = np.full(4, -0.05), np.full(4, 0.05)
    assert coverage(low, high, np.array([0.0, 0.05, 0.06, -0.2])) == 0.5


def test_brier_skill_interval_separates_skill_from_noise() -> None:
    rng = np.random.default_rng(1)
    truth = rng.uniform(0.3, 0.7, size=6000)
    happened = (rng.uniform(size=6000) < truth).astype(float)
    base = np.full(6000, 0.5)
    months = np.repeat(np.arange(60), 100)
    good = brier_skill_interval(truth, happened, base, months, np.random.default_rng(2))
    assert good.low > 0.0
    assert good.low <= good.skill <= good.high
    noise = brier_skill_interval(base, happened, base, months, np.random.default_rng(2))
    assert noise.low <= 0.0 <= noise.high

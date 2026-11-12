from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stride.forecast.params import WeightParams
from stride.forecast.risk_weights import (
    ALLOCATORS,
    RiskRule,
    allocate,
    apply_limits,
    drawdown_scale,
    equal_weights,
    erc_weights,
    inverse_vol_weights,
    loss_scale,
    risk_contributions,
    trend_ok,
)
from stride.forecast.weights import portfolio_loss

SYMBOLS = ("QQQ", "IWM", "GLD", "SLV", "TLT", "IBIT")
LIMITS = WeightParams(
    max_weight=0.25,
    max_total=1.0,
    groups={"us_equity": ("QQQ", "IWM"), "metals": ("GLD", "SLV"), "crypto": ("IBIT",)},
    group_caps={"us_equity": 0.40, "metals": 0.35, "crypto": 0.30},
    risk_horizon=5,
    risk_quantile=0.01,
    max_portfolio_loss=0.08,
    min_order_usd=150.0,
)


def _joint(seed: int, sigmas: tuple[float, ...], rho: float = 0.3, paths: int = 4000) -> np.ndarray:
    """Correlated normal simple returns with the given volatilities."""
    rng = np.random.default_rng(seed)
    size = len(sigmas)
    corr = np.full((size, size), rho) + (1.0 - rho) * np.eye(size)
    cov = corr * np.outer(sigmas, sigmas)
    return rng.multivariate_normal(np.zeros(size), cov, size=paths)


def test_rule_is_validated() -> None:
    with pytest.raises(ValueError, match="allocator"):
        RiskRule("x", "KELLY", "none")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="brake"):
        RiskRule("x", "EW", "stop")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="dd_floor"):
        RiskRule("x", "EW", "dd", dd_floor=0.0)
    with pytest.raises(ValueError, match="trend_days"):
        RiskRule("x", "EW", "trend", trend_days=1)


def test_equal_and_inverse_vol_weights() -> None:
    assert equal_weights(4).tolist() == [0.25] * 4
    assert len(equal_weights(0)) == 0
    weights = inverse_vol_weights(np.array([0.01, 0.02, 0.04]))
    assert weights.sum() == pytest.approx(1.0)
    assert weights == pytest.approx(np.array([4.0, 2.0, 1.0]) / 7.0)
    with pytest.raises(ValueError, match="sigma"):
        inverse_vol_weights(np.array([0.01, np.nan]))
    # A zero volatility is floored, not divided by.
    assert np.isfinite(inverse_vol_weights(np.array([0.0, 0.02]))).all()


def test_erc_equalizes_risk_contributions() -> None:
    sigmas = np.array([0.05, 0.10, 0.20, 0.40])
    corr = np.array(
        [[1.0, 0.8, 0.2, 0.0], [0.8, 1.0, 0.3, 0.1], [0.2, 0.3, 1.0, -0.2], [0.0, 0.1, -0.2, 1.0]]
    )
    cov = corr * np.outer(sigmas, sigmas)
    weights = erc_weights(cov)
    assert weights.sum() == pytest.approx(1.0)
    assert (weights > 0).all()
    assert risk_contributions(weights, cov) == pytest.approx(np.full(4, 0.25), abs=1e-6)
    # Without correlation ERC is inverse volatility.
    diagonal = np.diag(sigmas**2)
    assert erc_weights(diagonal) == pytest.approx(inverse_vol_weights(sigmas), abs=1e-8)
    # Two highly correlated assets share one risk budget more than an independent one.
    twins = np.array([[1.0, 0.95, 0.0], [0.95, 1.0, 0.0], [0.0, 0.0, 1.0]]) * 0.01
    spread = erc_weights(twins)
    assert spread[2] > spread[0] == pytest.approx(spread[1])
    with pytest.raises(ValueError, match="square"):
        erc_weights(np.zeros((2, 3)))


@settings(max_examples=60, deadline=None)
@given(
    sigmas=st.lists(st.floats(0.005, 0.6), min_size=2, max_size=8),
    rho=st.floats(-0.1, 0.9),
)
def test_erc_property_equal_contributions(sigmas: list[float], rho: float) -> None:
    size = len(sigmas)
    corr = np.full((size, size), rho) + (1.0 - rho) * np.eye(size)
    cov = corr * np.outer(sigmas, sigmas)
    weights = erc_weights(cov)
    assert weights.sum() == pytest.approx(1.0)
    assert (weights > 0).all()
    assert risk_contributions(weights, cov) == pytest.approx(np.full(size, 1.0 / size), abs=1e-5)


def test_allocate_sums_to_one_and_ranks_by_risk() -> None:
    joint = _joint(1, (0.02, 0.04, 0.08))
    for allocator in ALLOCATORS:
        weights = allocate(allocator, joint)
        assert weights.sum() == pytest.approx(1.0)
        assert (weights > 0).all()
    assert allocate("EW", joint) == pytest.approx(np.full(3, 1 / 3))
    for allocator in ("IV", "ERC"):
        weights = allocate(allocator, joint)
        assert weights[0] > weights[1] > weights[2]  # the calmer asset weighs more
    assert allocate("ERC", joint[:, :1]).tolist() == [1.0]
    with pytest.raises(ValueError, match="unknown"):
        allocate("KELLY", joint)  # type: ignore[arg-type]


def test_trend_ok_needs_history_and_a_close_above_the_average() -> None:
    rising = np.linspace(100.0, 150.0, 250)
    falling = np.linspace(150.0, 100.0, 250)
    young = np.concatenate([np.full(100, np.nan), np.linspace(100.0, 150.0, 150)])
    closes = np.column_stack([rising, falling, young])
    assert trend_ok(closes, 200).tolist() == [True, False, False]
    assert trend_ok(closes, 100).tolist() == [True, False, True]
    assert trend_ok(closes[:50], 200).tolist() == [False, False, False]
    # A close exactly at the average is not above it.
    flat = np.full((200, 1), 10.0)
    assert trend_ok(flat, 200).tolist() == [False]


def test_trend_ok_reads_only_the_window() -> None:
    closes = np.column_stack([np.linspace(100.0, 150.0, 300)])
    changed = closes.copy()
    changed[:100] = 1e6  # older than the window
    assert trend_ok(closes, 200).tolist() == trend_ok(changed, 200).tolist()


def test_drawdown_scale() -> None:
    assert drawdown_scale(0.0, 0.15) == 1.0
    assert drawdown_scale(-0.02, 0.15) == 1.0  # a new high is not a drawdown
    assert drawdown_scale(0.075, 0.15) == pytest.approx(0.5)
    assert drawdown_scale(0.15, 0.15) == 0.0
    assert drawdown_scale(0.30, 0.15) == 0.0
    with pytest.raises(ValueError, match="floor"):
        drawdown_scale(0.1, 0.0)


def test_loss_scale_brings_the_simulated_loss_to_the_cap() -> None:
    joint = _joint(2, (0.05, 0.06, 0.07))
    weights = np.full(3, 1 / 3)
    loss = portfolio_loss(weights, joint, 0.01)
    assert loss > 0.08
    scale = loss_scale(weights, joint, 0.01, 0.08)
    assert 0.0 < scale < 1.0
    assert portfolio_loss(weights * scale, joint, 0.01) == pytest.approx(0.08)
    assert loss_scale(weights * 0.1, joint, 0.01, 0.08) == 1.0


def test_apply_limits_caps_and_drops_small_orders() -> None:
    weights = np.array([0.30, 0.30, 0.20, 0.10, 0.09, 0.01])
    out = apply_limits(weights, SYMBOLS, equity=10_000.0, params=LIMITS)
    # QQQ and IWM are capped at 25% each and then at 40% together.
    assert out[0] == pytest.approx(0.20)
    assert out[1] == pytest.approx(0.20)
    assert out[2:5] == pytest.approx([0.20, 0.10, 0.09])
    assert out[5] == 0.0  # 1% of 10,000 is below the 150 USD minimum order
    assert apply_limits(weights, SYMBOLS, equity=0.0, params=LIMITS).tolist() == [0.0] * 6
    with pytest.raises(ValueError, match="one weight"):
        apply_limits(weights[:3], SYMBOLS, equity=1.0, params=LIMITS)
    with pytest.raises(ValueError, match="not negative"):
        apply_limits(-weights, SYMBOLS, equity=1.0, params=LIMITS)


@settings(max_examples=300, deadline=None)
@given(
    raw=st.lists(st.floats(0.0, 1.5), min_size=6, max_size=6),
    equity=st.floats(100.0, 1_000_000.0),
    max_weight=st.floats(0.05, 1.0),
    max_total=st.floats(0.2, 1.0),
    min_order=st.floats(0.0, 500.0),
)
def test_apply_limits_properties(
    raw: list[float], equity: float, max_weight: float, max_total: float, min_order: float
) -> None:
    """Never negative, never above a cap, sum at most the total, and never raised."""
    params = WeightParams(
        max_weight=max_weight,
        max_total=max_total,
        groups=LIMITS.groups,
        group_caps=LIMITS.group_caps,
        min_order_usd=min_order,
    )
    weights = np.array(raw)
    out = apply_limits(weights, SYMBOLS, equity=equity, params=params)
    tolerance = 1e-12
    assert (out >= 0).all()
    assert (out <= weights + tolerance).all()  # every step only lowers a weight
    assert (out <= max_weight + tolerance).all()
    assert out.sum() <= max_total + tolerance
    assert out[:2].sum() <= 0.40 + tolerance
    assert out[2:4].sum() <= 0.35 + tolerance
    assert out[5] <= 0.30 + tolerance
    held = out > 0
    assert (out[held] * equity >= min_order - 1e-9).all()  # no order below the minimum


@settings(max_examples=100, deadline=None)
@given(
    seed=st.integers(0, 10_000),
    scale=st.floats(0.2, 3.0),
    drawdown=st.floats(0.0, 0.4),
)
def test_brakes_and_loss_cap_only_lower(seed: int, scale: float, drawdown: float) -> None:
    joint = _joint(seed, (0.02, 0.03, 0.05, 0.06, 0.02, 0.10), paths=500) * scale
    for allocator in ALLOCATORS:
        base = apply_limits(allocate(allocator, joint), SYMBOLS, equity=10_000.0, params=LIMITS)
        factor = min(
            drawdown_scale(drawdown, 0.15),
            loss_scale(base, joint, LIMITS.risk_quantile, LIMITS.max_portfolio_loss),
        )
        final = base * factor
        assert 0.0 <= factor <= 1.0
        assert (final <= base + 1e-12).all()
        assert final.sum() <= 1.0 + 1e-12
        assert portfolio_loss(final, joint, LIMITS.risk_quantile) <= 0.08 + 1e-9

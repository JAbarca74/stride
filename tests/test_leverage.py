from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stride.config import load_config
from stride.indicators import ewma_volatility
from stride.risk.leverage import (
    BROKER,
    CAPITAL,
    KELLY,
    MAX_LEVERAGE,
    VOL_TARGET,
    HeldInstrument,
    KellyEstimate,
    choose_instruments,
    effective_leverage,
    ewma_covariance,
    forward_volatility,
    kelly_estimate,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
RISK = CONFIG.risk  # kelly 1.0, vol 0.60, max 4.0, etf 2.5, dd 0.20, margin $2,000
SWITCH = CONFIG.leverage_switch  # enter 1.25, exit 0.95, hold 20
ELIGIBLE = {"SPY": {2: "SSO", 3: "UPRO"}, "TLT": {3: "TMF"}, "GLD": {2: "UGL"}}
NO_ESTIMATE = KellyEstimate(None, math.nan, math.nan, 0)
DAYS = pd.bdate_range("2024-01-02", periods=300)


def _kelly(l_star: float) -> KellyEstimate:
    return KellyEstimate(l_star, 0.1, 0.2, 756)


def _lev(kelly: KellyEstimate = NO_ESTIMATE, **kw: float | bool) -> tuple[float, str, bool]:
    args: dict[str, float | bool] = {
        "sigma": 0.0,
        "broker_leverage": 2.0,
        "equity": 500.0,
        "drawdown": 0.0,
        "delever_active": False,
    } | kw
    decision = effective_leverage(kelly, risk=RISK, **args)  # type: ignore[arg-type]
    return decision.leverage, decision.binding_limit, decision.delever_active


# Step 1: Kelly.


def test_kelly_by_hand() -> None:
    returns = pd.Series([0.002, -0.001] * 200)  # 400 days
    mu = returns.mean() * 252
    sigma = returns.std(ddof=1) * math.sqrt(252)
    estimate = kelly_estimate(returns, risk_free=0.04, min_days=250)
    assert estimate.l_star == pytest.approx((mu - 0.04) / sigma**2)
    assert estimate.days == 400


def test_kelly_uses_the_last_three_years() -> None:
    returns = pd.Series([0.5] * 100 + [0.002, -0.001] * 400)  # old outliers dropped
    assert kelly_estimate(returns, 0.0, 250).days == 756
    assert kelly_estimate(returns, 0.0, 250).mu == pytest.approx(0.0005 * 252)


@pytest.mark.parametrize(
    "returns",
    [
        pd.Series([0.002, -0.001] * 100),  # fewer than 250 days
        pd.Series([-0.002, 0.001] * 200),  # negative L*
        pd.Series([0.001] * 400),  # zero volatility
        pd.Series([0.01]),  # one day
    ],
)
def test_kelly_without_estimate(returns: pd.Series) -> None:
    assert kelly_estimate(returns, 0.0, 250).l_star is None


# Forward-looking volatility (SPEC 1.3).


def _returns(seed: int, columns: list[str], corr: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = rng.normal(0, 0.01, len(DAYS))
    data = {
        c: corr * base + math.sqrt(1 - corr**2) * rng.normal(0, 0.01, len(DAYS)) for c in columns
    }
    return pd.DataFrame(data, index=DAYS)


def test_single_asset_matches_vol60() -> None:
    close = pd.Series(100 * np.cumprod(1 + _returns(1, ["SPY"])["SPY"]), index=DAYS)
    returns = close.pct_change().iloc[1:].to_frame("SPY")
    sigma = forward_volatility({"SPY": 1.0}, returns)
    assert sigma == pytest.approx(float(ewma_volatility(close, 60).iloc[-1]))


def test_correlated_assets_add_more_risk_than_independent_ones() -> None:
    together = forward_volatility({"A": 0.5, "B": 0.5}, _returns(2, ["A", "B"], corr=0.95))
    apart = forward_volatility({"A": 0.5, "B": 0.5}, _returns(2, ["A", "B"], corr=0.0))
    assert together > apart > 0


def test_new_position_counts_the_same_day() -> None:
    returns = _returns(3, ["SPY", "IBIT"])
    returns["IBIT"] *= 4  # bitcoin-like volatility
    without = forward_volatility({"SPY": 0.5}, returns)
    with_btc = forward_volatility({"SPY": 0.5, "IBIT": 0.3}, returns)
    assert with_btc > 2 * without


def test_no_positions_means_zero_volatility() -> None:
    assert forward_volatility({}, _returns(4, ["A"])) == 0.0
    assert forward_volatility({"A": 0.0}, _returns(4, ["A"])) == 0.0


def test_volatility_input_errors() -> None:
    with pytest.raises(ValueError, match="no returns"):
        forward_volatility({"X": 1.0}, _returns(5, ["A"]))
    with pytest.raises(ValueError, match="at least 60"):
        ewma_covariance(_returns(5, ["A"]).iloc[:10])


# Step 2: effective leverage, each limit binding in turn.


def test_kelly_binds() -> None:
    assert _lev(_kelly(1.5), sigma=0.1)[:2] == (1.5, KELLY)


def test_vol_target_binds() -> None:
    leverage, binding, _ = _lev(_kelly(3.0), sigma=0.4)
    assert leverage == pytest.approx(1.5)
    assert binding == VOL_TARGET


def test_max_leverage_binds() -> None:
    assert _lev(_kelly(9.0), equity=5_000, broker_leverage=6.0)[:2] == (4.0, MAX_LEVERAGE)


def test_broker_binds_with_a_margin_account() -> None:
    assert _lev(_kelly(3.0), equity=5_000, broker_leverage=2.0)[:2] == (2.0, BROKER)


def test_capital_binds_below_2000() -> None:
    # No margin: the cash account's broker limit (1.0) is not applied, the ETF cap is.
    assert _lev(_kelly(3.0), equity=500, broker_leverage=1.0)[:2] == (2.5, CAPITAL)


def test_fallback_replaces_only_the_kelly_term() -> None:
    assert _lev(NO_ESTIMATE)[:2] == (1.0, KELLY)
    leverage, binding, _ = _lev(NO_ESTIMATE, sigma=1.2)  # vol target 0.5 still applies
    assert leverage == pytest.approx(0.5)
    assert binding == VOL_TARGET


def test_zero_sigma_drops_the_vol_target() -> None:
    assert (
        VOL_TARGET
        not in effective_leverage(
            _kelly(2.0),
            sigma=0.0,
            broker_leverage=2.0,
            equity=500,
            drawdown=0.0,
            delever_active=False,
            risk=RISK,
        ).limits
    )


def test_deleveraging_starts_stays_and_resets() -> None:
    assert _lev(_kelly(2.0), drawdown=0.2) == (2.0, KELLY, False)  # not beyond 20%
    assert _lev(_kelly(2.0), drawdown=0.21) == (1.0, KELLY, True)  # halved after the min
    assert _lev(_kelly(2.0), drawdown=0.05, delever_active=True) == (1.0, KELLY, True)
    assert _lev(_kelly(2.0), drawdown=0.0, delever_active=True) == (2.0, KELLY, False)


@pytest.mark.parametrize(
    "kw",
    [
        {"sigma": math.nan},
        {"equity": -1.0},
        {"drawdown": 1.5},
        {"broker_leverage": math.inf},
        {"equity": 5_000, "broker_leverage": 0.0},
    ],
)
def test_invalid_inputs_are_rejected(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        _lev(_kelly(2.0), **kw)


# Step 2b: instruments.


def _choose(
    exposures: dict[str, float],
    equity: float = 1_000.0,
    current: dict[str, HeldInstrument] | None = None,
) -> dict[str, tuple[str, float]]:
    result = choose_instruments(
        exposures,
        equity,
        eligible=ELIGIBLE,
        current=current or {},
        switch=SWITCH,
        max_etf_portfolio_leverage=2.5,
    )
    return {a: (c.symbol, round(c.cash_usd, 6)) for a, c in result.items()}


def test_k_below_one_uses_normal_etfs() -> None:
    assert _choose({"SPY": 400, "TLT": 300}) == {"SPY": ("SPY", 400), "TLT": ("TLT", 300)}


def test_smallest_multiple_that_fits() -> None:
    # k = 1.6: at 2x SPY 600 + GLD 200 + EFA 200 (no version) = 1,000 fits.
    result = _choose({"SPY": 1_200, "GLD": 400, "EFA": 200})
    assert result == {"SPY": ("SSO", 600), "GLD": ("UGL", 200), "EFA": ("EFA", 200)}


def test_tlt_goes_from_1x_straight_to_3x() -> None:
    at_2x = _choose({"SPY": 1_000, "TLT": 300})  # 2x fits: 500 + 300 (TLT stays 1x)
    assert at_2x["TLT"] == ("TLT", 300)
    at_3x = _choose({"SPY": 1_200, "TLT": 900})  # 2x: 600 + 900 > 1,000 -> 3x
    assert at_3x == {"SPY": ("UPRO", 400), "TLT": ("TMF", 300)}


def test_portfolio_capped_at_2_5x() -> None:
    result = choose_instruments(
        {"SPY": 5_000},
        1_000.0,
        eligible=ELIGIBLE,
        current={},
        switch=SWITCH,
        max_etf_portfolio_leverage=2.5,
    )
    assert result["SPY"].exposure_usd == pytest.approx(2_500)  # capped from 5,000
    assert result["SPY"].symbol == "UPRO"  # 2x would need 1,250 of cash; 3x needs 833


def test_scaled_down_when_nothing_fits() -> None:
    result = choose_instruments(
        {"EFA": 1_100, "SPY": 1_100},
        1_000.0,
        eligible=ELIGIBLE,
        current={},
        switch=SWITCH,
        max_etf_portfolio_leverage=2.5,
    )
    assert sum(c.cash_usd for c in result.values()) == pytest.approx(1_000)
    assert result["EFA"].reason.endswith("+SCALED")


def test_hysteresis_band_keeps_the_current_instrument() -> None:
    held = {"SPY": HeldInstrument(multiple=2, days_held=30)}
    assert _choose({"SPY": 1_100}, current=held)["SPY"] == ("SSO", 550)  # k = 1.1
    assert _choose({"SPY": 1_100})["SPY"][0] == "SPY"  # nothing held: normal, scaled to fit
    assert _choose({"SPY": 940}, current=held)["SPY"] == ("SPY", 940)  # k <= 0.95: back


def test_enter_threshold_is_inclusive() -> None:
    assert _choose({"SPY": 1_249.0})["SPY"][0] == "SPY"
    assert _choose({"SPY": 1_250.0})["SPY"][0] == "SSO"


def test_minimum_hold_days() -> None:
    young = {"SPY": HeldInstrument(multiple=2, days_held=5)}
    assert _choose({"SPY": 900}, current=young)["SPY"] == ("SSO", 450)  # kept 20 days
    old = {"SPY": HeldInstrument(multiple=2, days_held=20)}
    assert _choose({"SPY": 900}, current=old)["SPY"] == ("SPY", 900)


def test_closed_position_is_dropped_and_frees_the_hold() -> None:
    young = {"SPY": HeldInstrument(multiple=2, days_held=1)}
    assert _choose({"SPY": 0.0, "TLT": 100}, current=young) == {"TLT": ("TLT", 100)}


def test_instrument_input_errors() -> None:
    with pytest.raises(ValueError, match="equity"):
        _choose({"SPY": 100}, equity=0)
    with pytest.raises(ValueError, match="long only"):
        _choose({"SPY": -100})


# Invariants (hypothesis): thousands of random scenarios.

finite = st.floats(min_value=0.0, max_value=10.0, allow_nan=False)


@settings(max_examples=500, deadline=None)
@given(
    l_star=st.one_of(st.none(), st.floats(min_value=0.01, max_value=50)),
    sigma=finite,
    broker=st.floats(min_value=0.1, max_value=10),
    equity=st.floats(min_value=0, max_value=1e7),
    drawdown=st.floats(min_value=0, max_value=1),
    active=st.booleans(),
)
def test_leverage_never_exceeds_any_limit(  # noqa: PLR0917
    l_star: float | None,
    sigma: float,
    broker: float,
    equity: float,
    drawdown: float,
    active: bool,
) -> None:
    kelly = KellyEstimate(l_star, 0.1, 0.2, 756)
    decision = effective_leverage(
        kelly,
        sigma=sigma,
        broker_leverage=broker,
        equity=equity,
        drawdown=drawdown,
        delever_active=active,
        risk=RISK,
    )
    assert 0 < decision.leverage <= RISK.max_leverage
    assert all(decision.leverage <= limit + 1e-12 for limit in decision.limits.values())
    expected = min(decision.limits.values()) * (0.5 if decision.delever_active else 1.0)
    assert decision.leverage == pytest.approx(expected)
    if equity < RISK.margin_min_equity:
        assert decision.leverage <= RISK.max_etf_portfolio_leverage
    else:
        assert decision.leverage <= broker


assets = st.sampled_from(["SPY", "TLT", "GLD", "EFA", "DBC"])


@settings(max_examples=500, deadline=None)
@given(
    exposures=st.dictionaries(assets, st.floats(min_value=0, max_value=5_000), max_size=5),
    equity=st.floats(min_value=1, max_value=2_000),
    held=st.dictionaries(
        assets,
        st.builds(
            HeldInstrument, multiple=st.sampled_from([1, 2, 3]), days_held=st.integers(0, 40)
        ),
        max_size=5,
    ),
)
def test_instruments_always_fit_in_the_equity(
    exposures: dict[str, float], equity: float, held: dict[str, HeldInstrument]
) -> None:
    result = choose_instruments(
        exposures,
        equity,
        eligible=ELIGIBLE,
        current=held,
        switch=SWITCH,
        max_etf_portfolio_leverage=2.5,
    )
    assert sum(c.cash_usd for c in result.values()) <= equity * (1 + 1e-9)
    assert sum(c.exposure_usd for c in result.values()) <= max(sum(exposures.values()), 0) * (
        1 + 1e-9
    )
    for asset, choice in result.items():
        assert choice.multiple in {1, *ELIGIBLE.get(asset, {})}
        assert choice.symbol == (
            asset if choice.multiple == 1 else ELIGIBLE[asset][choice.multiple]
        )
        assert choice.exposure_usd > 0
        assert choice.exposure_usd <= exposures[asset] * (1 + 1e-9)
        assert choice.cash_usd == pytest.approx(choice.exposure_usd / choice.multiple)


@settings(max_examples=200, deadline=None)
@given(
    w=st.lists(st.floats(min_value=-1, max_value=1), min_size=3, max_size=3),
    seed=st.integers(0, 1_000),
)
def test_portfolio_volatility_is_bounded_by_its_parts(w: list[float], seed: int) -> None:
    returns = _returns(seed, ["A", "B", "C"], corr=0.3)
    weights = dict(zip(["A", "B", "C"], w, strict=True))
    sigma = forward_volatility(weights, returns)
    parts = sum(abs(x) * forward_volatility({s: 1.0}, returns) for s, x in weights.items())
    assert 0 <= sigma <= parts + 1e-9

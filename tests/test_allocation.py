"""Pre-registered monthly strategies (docs/design/strategy_search.md)."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from stride.backtest.engine import BacktestSettings, MarketData, run_backtest
from stride.config import ConfigError, StrideConfig, load_config
from stride.data.calendar import trading_days
from stride.strategy.allocation import (
    breakout_on,
    dip_on,
    dual_momentum,
    hold,
    hurdle,
    kelly_share,
    lookback_return,
    momentum_vol,
    range_on,
    realized_vol,
    relative_strength,
    risk_parity,
    sleeve,
    target_weights,
    trend_on,
    vol_managed,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
DAYS = pd.bdate_range("2020-01-01", periods=300)


def _closes(**growth: float) -> pd.DataFrame:
    return pd.DataFrame(
        {s: 100 * (1 + g) ** (np.arange(len(DAYS)) / 252) for s, g in growth.items()},
        index=DAYS,
    )


def test_lookback_return_and_hurdle() -> None:
    close = pd.Series(np.arange(1.0, 301.0))
    assert lookback_return(close, 252) == pytest.approx(300 / 48 - 1)
    assert lookback_return(close, 252, 21) == pytest.approx(279 / 48 - 1)
    assert math.isnan(lookback_return(close.iloc[:100], 252))
    assert hurdle(0.04, 252) == pytest.approx(0.04)
    assert hurdle(0.04, 252, 21) == pytest.approx(0.04 * 231 / 252)


def test_dual_momentum_picks_the_best_asset_or_the_refuge() -> None:
    kw: dict[str, Any] = {"assets": ["SPY", "EFA"], "refuge": "IEF", "lookback": 252}
    assert dual_momentum(_closes(SPY=0.15, EFA=0.05, IEF=0.0), 0.02, **kw) == {"SPY": 1.0}
    assert dual_momentum(_closes(SPY=0.05, EFA=0.12, IEF=0.0), 0.02, **kw) == {"EFA": 1.0}
    # Both below the T-bill rate: the refuge.
    assert dual_momentum(_closes(SPY=0.01, EFA=-0.1, IEF=0.0), 0.04, **kw) == {"IEF": 1.0}
    cash: dict[str, Any] = kw | {"refuge": "cash"}
    assert dual_momentum(_closes(SPY=-0.1, EFA=-0.1), 0.04, **cash) == {}
    assert dual_momentum(_closes(SPY=0.1).iloc[:100], 0.0, **kw) == {}  # too short


def test_relative_strength_holds_the_top_assets_above_t_bills() -> None:
    closes = _closes(A=0.30, B=0.20, C=0.10, D=0.01, IEF=0.0)
    kw: dict[str, Any] = {
        "assets": ["A", "B", "C", "D"],
        "refuge": "IEF",
        "lookback": 252,
        "skip": 21,
    }
    weights = relative_strength(closes, 0.0, top=3, **kw)
    assert weights == pytest.approx({"A": 1 / 3, "B": 1 / 3, "C": 1 / 3})
    # With a 25% T-bill rate (22.9% over the 231 days) only A (27.2%) beats it:
    # two slots go to the refuge.
    weights = relative_strength(closes, 0.25, top=3, **kw)
    assert weights == pytest.approx({"A": 1 / 3, "IEF": 2 / 3})
    assert sum(weights.values()) == pytest.approx(1.0)


def test_vol_managed_scales_inversely_with_volatility_and_caps() -> None:
    rng = np.random.default_rng(1)
    calm = pd.DataFrame({"SPY": 100 * np.cumprod(1 + rng.normal(0, 0.005, 300))}, index=DAYS)
    wild = pd.DataFrame({"SPY": 100 * np.cumprod(1 + rng.normal(0, 0.03, 300))}, index=DAYS)
    kw: dict[str, Any] = {
        "assets": ["SPY"],
        "target_vol": 0.20,
        "max_weight": 3.0,
        "vol_window": 21,
    }
    assert vol_managed(calm, 0.0, **kw)["SPY"] == pytest.approx(3.0)  # capped
    assert vol_managed(wild, 0.0, **kw)["SPY"] < 0.5
    assert vol_managed(calm.iloc[:10], 0.0, **kw) == {}


def _noisy(**vols: float) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    return pd.DataFrame(
        {
            s: 100 * np.cumprod(1 + 0.0005 + rng.normal(0, v / math.sqrt(252), len(DAYS)))
            for s, v in vols.items()
        },
        index=DAYS,
    )


def test_realized_vol_is_annualized_and_nan_when_short() -> None:
    closes = _noisy(A=0.20)
    assert 0.15 < realized_vol(closes["A"], 252) < 0.25
    assert math.isnan(realized_vol(closes["A"].iloc[:10], 21))


def test_momentum_vol_sizes_the_winner_and_keeps_the_refuge_at_one() -> None:
    closes = _closes(SPY=0.15, QQQ=0.25, IEF=0.0)
    rng = np.random.default_rng(2)
    closes["QQQ"] *= np.cumprod(1 + rng.normal(0, 0.02, len(DAYS)))  # about 32% a year
    kw: dict[str, Any] = {
        "assets": ["SPY", "QQQ"],
        "refuge": "IEF",
        "lookback": 252,
        "skip": 0,
        "target_vol": 0.20,
        "max_weight": 2.0,
        "vol_window": 63,
    }
    weights = momentum_vol(closes, 0.0, **kw)
    momentum = {a: lookback_return(closes[a], 252) for a in ["SPY", "QQQ"]}
    best = max(momentum, key=lambda a: momentum[a])
    assert list(weights) == [best]
    expected = min(2.0, 0.20 / realized_vol(closes[best], 63))
    assert weights[best] == pytest.approx(expected)
    # Nothing beats a 50% T-bill rate: the refuge at full weight.
    assert momentum_vol(closes, 0.5, **kw) == {"IEF": 1.0}


def test_risk_parity_weights_inverse_to_volatility_and_hits_the_target() -> None:
    closes = _noisy(SPY=0.20, TLT=0.10)
    kw: dict[str, Any] = {
        "assets": ["SPY", "TLT", "MISSING"],
        "target_vol": 0.10,
        "max_weight": 4.0,
        "vol_window": 63,
    }
    weights = risk_parity(closes, 0.0, **kw)
    assert set(weights) == {"SPY", "TLT"}
    ratio = realized_vol(closes["TLT"], 63) / realized_vol(closes["SPY"], 63)
    assert weights["SPY"] / weights["TLT"] == pytest.approx(ratio)
    returns = closes.pct_change().iloc[-63:]
    portfolio = returns["SPY"] * weights["SPY"] + returns["TLT"] * weights["TLT"]
    assert float(portfolio.std(ddof=1)) * math.sqrt(252) == pytest.approx(0.10)
    capped = risk_parity(closes, 0.0, **(kw | {"target_vol": 0.80, "max_weight": 1.5}))
    assert sum(capped.values()) == pytest.approx(1.5)
    assert risk_parity(closes.iloc[:10], 0.0, **kw) == {}


def test_hold_is_always_fully_invested_in_equal_weights() -> None:
    closes = _closes(SPY=-0.5, EFA=0.1)
    assert hold(closes, 0.5, assets=["SPY"]) == {"SPY": 1.0}  # even in a crash
    assert hold(closes, 0.0, assets=["SPY", "EFA", "MISSING"]) == {"SPY": 0.5, "EFA": 0.5}
    assert hold(closes, 0.0, assets=["MISSING"]) == {}


def test_trend_breakout_and_range_states_from_closes() -> None:
    up = pd.Series(np.linspace(100, 200, 300))
    down = pd.Series(np.linspace(200, 100, 300))
    assert trend_on(up, 200) and not trend_on(down, 200)
    assert not trend_on(up.iloc[:50], 200)  # too short
    assert breakout_on(up, 55, 20) and not breakout_on(down, 55, 20)
    # Range trading buys falling prices at the lows and sells rising ones.
    assert range_on(down, 20, 20) and not range_on(up, 20, 20)
    # Up, then a fall below the 20-day low: the breakout is out.
    turn = pd.concat([up, pd.Series(np.linspace(199, 150, 30))], ignore_index=True)
    assert not breakout_on(turn, 55, 20)


def test_dip_buys_deep_falls_and_rides_the_rise_past_the_target() -> None:
    kw: dict[str, Any] = {"drop": 0.25, "window": 90, "take_profit": 0.30, "trail": 3}

    def path(*tail: float) -> pd.Series:
        return pd.Series([*np.full(100, 100.0), *tail])

    assert not dip_on(path(), **kw)
    assert dip_on(path(90, 80, 74), **kw)  # 26% below the 90-day high: bought at 74
    assert dip_on(path(90, 80, 74, 60, 70, 90), **kw)  # 90 < 74 * 1.3 = 96.2: holds
    # Target reached at 97 and the rise goes on: it holds.
    assert dip_on(path(90, 80, 74, 85, 97, 105, 112), **kw)
    # Then a close below the prior 3-day low (97, 105, 112 -> 97): sold.
    assert not dip_on(path(90, 80, 74, 85, 97, 105, 112, 96), **kw)
    # Before the target, a fall below the 3-day low does not sell (no stop).
    assert dip_on(path(90, 80, 74, 85, 90, 70), **kw)


def test_kelly_share_is_a_clipped_fraction_of_excess_return_over_variance() -> None:
    rng = np.random.default_rng(4)
    returns = rng.normal(0.002, 0.03, 600)
    close = pd.Series(100 * np.cumprod(1 + returns))
    observed = pd.Series(returns[-504:])
    full = (observed.mean() * 252 - 0.02) / (observed.var(ddof=1) * 252)
    expected = min(max(0.5 * full, 0.0), 1.0)
    assert kelly_share(close, 0.02, fraction=0.5, window=504) == pytest.approx(expected)
    assert kelly_share(close, 0.02, fraction=0.0, window=504) == 1.0  # no Kelly
    assert kelly_share(close.iloc[:100], 0.02, fraction=0.5, window=504) == 0.0  # short
    falling = pd.Series(100 * np.cumprod(1 + rng.normal(-0.002, 0.03, 600)))
    assert kelly_share(falling, 0.02, fraction=0.5, window=504) == 0.0


def test_sleeve_keeps_the_core_and_sizes_crypto_only_while_on() -> None:
    days = pd.bdate_range("2018-01-01", periods=800)
    closes = pd.DataFrame(
        {
            "SPY": np.linspace(100, 150, 800),
            "IBIT": np.linspace(100, 400, 800),  # rising: trend on
            "ETHA": np.linspace(400, 100, 800),  # falling: trend off
        },
        index=days,
    )
    kw: dict[str, Any] = {
        "core": ["SPY"],
        "assets": ["IBIT", "ETHA"],
        "share": 0.2,
        "signal": "trend",
        "trend_sma": 200,
        "entry": 55,
        "exit": 20,
        "kelly_fraction": 0.0,
        "kelly_window": 504,
    }
    assert sleeve(closes, 0.0, **kw) == pytest.approx({"SPY": 0.8, "IBIT": 0.1})
    held = sleeve(closes, 0.0, **(kw | {"signal": "hold"}))
    assert held == pytest.approx({"SPY": 0.8, "IBIT": 0.1, "ETHA": 0.1})
    halved = sleeve(closes, 0.0, **(kw | {"kelly_fraction": 0.5}))
    assert halved["SPY"] == pytest.approx(0.8)
    assert 0 < halved["IBIT"] <= 0.1
    assert sum(sleeve(closes, 0.0, **kw).values()) <= 1.0


def test_target_weights_dispatch_on_the_config_mode() -> None:
    closes = _closes(SPY=0.15, EFA=0.05, IEF=0.0)
    settings = CONFIG.experiments.allocation
    assert target_weights(settings, closes, 0.02) == {"SPY": 1.0}
    noisy = _noisy(SPY=0.20, EFA=0.10, IEF=0.05)
    for mode in ("momentum_vol", "risk_parity", "vol_managed", "relative_strength", "hold"):
        weights = target_weights(settings.model_copy(update={"mode": mode}), noisy, 0.0)
        assert weights, mode
        assert set(weights) <= {"SPY", "EFA", "IEF"}


def _allocation_config(**values: object) -> StrideConfig:
    data = CONFIG.model_dump()
    data["experiments"]["allocation"].update(values)
    data["experiments"]["enabled"] = ["allocation"]
    return type(CONFIG).model_validate(data)


def test_allocation_rebalances_once_a_month() -> None:
    days = trading_days(date(2018, 1, 2), date(2020, 12, 31))
    rng = np.random.default_rng(3)

    def bars(drift: float) -> pd.DataFrame:
        close = 100 * np.cumprod(1 + rng.normal(drift, 0.01, len(days)))
        return pd.DataFrame(
            {"open": close, "high": close * 1.003, "low": close * 0.997, "close": close},
            index=days,
        )

    data = MarketData(
        {"SPY": bars(0.0008), "EFA": bars(0.0002), "IEF": bars(0.0001)},
        pd.Series(0.01, index=pd.date_range("2017-12-01", "2021-01-31")),
    )
    result = run_backtest(
        data,
        _allocation_config(),
        BacktestSettings(date(2019, 1, 2), date(2020, 12, 31), 10_000.0),
    )
    orders = result.orders
    assert not orders.empty
    order_days = pd.DatetimeIndex(orders["date"]).unique()
    months = {(d.year, d.month) for d in order_days}
    assert len(order_days) == len(months)  # at most one order day per month
    assert set(orders["symbol"]) <= {"SPY", "EFA", "IEF", "SSO", "UPRO"}
    assert result.risk_decisions["binding_limit"].isin(["ALLOCATION", "KILL_SWITCH"]).all()


def test_daily_sleeve_trades_crypto_and_keeps_the_core() -> None:
    days = trading_days(date(2016, 1, 4), date(2020, 12, 31))
    rng = np.random.default_rng(8)

    def bars(drift: float, vol: float) -> pd.DataFrame:
        close = 100 * np.cumprod(1 + rng.normal(drift, vol, len(days)))
        return pd.DataFrame(
            {"open": close, "high": close * 1.003, "low": close * 0.997, "close": close},
            index=days,
        )

    data = MarketData(
        {"SPY": bars(0.0005, 0.01), "IBIT": bars(0.002, 0.04), "ETHA": bars(0.002, 0.05)},
        pd.Series(0.01, index=pd.date_range("2015-12-01", "2021-01-31")),
    )
    config = _allocation_config(
        mode="sleeve", assets=["IBIT", "ETHA"], refuge="cash", signal="breakout",
        rebalance="daily", kelly_fraction=0.5,
    )  # fmt: skip
    result = run_backtest(
        data, config, BacktestSettings(date(2018, 6, 1), date(2020, 12, 31), 10_000.0)
    )
    symbols = set(result.orders["symbol"])
    assert "SPY" in symbols
    assert symbols <= {"SPY", "IBIT", "ETHA", "SSO", "UPRO", "BITX", "ETHU"}


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"assets": ["QQQ"]}, "assets must be in the universe"),
        ({"refuge": "BIL"}, "refuge must be in the universe or cash"),
        ({"skip": 300}, "skip must be shorter"),
        ({"core": ["QQQ"]}, "assets must be in the universe"),
    ],
)
def test_allocation_config_is_validated(values: dict[str, object], match: str) -> None:
    with pytest.raises((ConfigError, ValueError), match=match):
        _allocation_config(**values)

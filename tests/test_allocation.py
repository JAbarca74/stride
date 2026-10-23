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
    dual_momentum,
    hurdle,
    lookback_return,
    momentum_vol,
    realized_vol,
    relative_strength,
    risk_parity,
    target_weights,
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


def test_target_weights_dispatch_on_the_config_mode() -> None:
    closes = _closes(SPY=0.15, EFA=0.05, IEF=0.0)
    settings = CONFIG.experiments.allocation
    assert target_weights(settings, closes, 0.02) == {"SPY": 1.0}
    noisy = _noisy(SPY=0.20, EFA=0.10, IEF=0.05)
    for mode in ("momentum_vol", "risk_parity", "vol_managed", "relative_strength"):
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


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"assets": ["QQQ"]}, "assets must be in the universe"),
        ({"refuge": "BIL"}, "refuge must be in the universe or cash"),
        ({"skip": 300}, "skip must be shorter"),
    ],
)
def test_allocation_config_is_validated(values: dict[str, object], match: str) -> None:
    with pytest.raises((ConfigError, ValueError), match=match):
        _allocation_config(**values)

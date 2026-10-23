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
    relative_strength,
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
    # With a 15% T-bill rate only A beats it: two slots go to the refuge.
    weights = relative_strength(closes, 0.15, top=3, **kw)
    assert weights == pytest.approx({"A": 1 / 3, "IEF": 2 / 3})
    assert sum(weights.values()) == pytest.approx(1.0)


def test_vol_managed_scales_inversely_with_volatility_and_caps() -> None:
    rng = np.random.default_rng(1)
    calm = pd.DataFrame({"SPY": 100 * np.cumprod(1 + rng.normal(0, 0.005, 300))}, index=DAYS)
    wild = pd.DataFrame({"SPY": 100 * np.cumprod(1 + rng.normal(0, 0.03, 300))}, index=DAYS)
    kw: dict[str, Any] = {"assets": ["SPY"], "target_vol": 0.20, "max_weight": 3.0}
    assert vol_managed(calm, 0.0, **kw)["SPY"] == pytest.approx(3.0)  # capped
    assert vol_managed(wild, 0.0, **kw)["SPY"] < 0.5
    assert vol_managed(calm.iloc[:10], 0.0, **kw) == {}


def test_target_weights_dispatch_on_the_config_mode() -> None:
    closes = _closes(SPY=0.15, EFA=0.05, IEF=0.0)
    assert target_weights(CONFIG.experiments.allocation, closes, 0.02) == {"SPY": 1.0}


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

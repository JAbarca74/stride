"""A short allocation backtest on fixed random data, shared by the golden test
(tests/test_targets.py): the dip sleeve with a 1.5x core, leveraged versions of
the core, the drawdown scaling over a 252-day window and daily rebalancing."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from stride.backtest.engine import BacktestResult, BacktestSettings, MarketData, run_backtest
from stride.config import StrideConfig, load_config
from stride.data.calendar import trading_days

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "data" / "allocation_equity_golden.json"


def golden_config() -> StrideConfig:
    data = load_config(ROOT / "config.yaml", env={}).model_dump()
    data["experiments"]["allocation"].update(
        mode="sleeve", core=["SPY"], core_leverage=1.5, assets=["IBIT", "ETHA"],
        refuge="cash", signal="dip", rebalance="daily", kelly_fraction=0.0,
    )  # fmt: skip
    data["experiments"]["enabled"] = ["allocation", "drawdown_scaling"]
    data["experiments"]["dd_scale_window"] = 252
    return StrideConfig.model_validate(data)


def golden_data() -> MarketData:
    days = trading_days(date(2016, 1, 4), date(2020, 12, 31))
    rng = np.random.default_rng(20261024)

    def bars(drift: float, vol: float) -> pd.DataFrame:
        close = 100 * np.cumprod(1 + rng.normal(drift, vol, len(days)))
        opens = close * (1 + rng.normal(0, 0.002, len(days)))
        return pd.DataFrame(
            {"open": opens, "high": np.maximum(opens, close) * 1.003,
             "low": np.minimum(opens, close) * 0.997, "close": close},
            index=days,
        )  # fmt: skip

    spy = bars(0.0005, 0.012)
    sso = spy * 2 - 100  # a crude 2x path, enough to exercise the instrument switch
    sso = sso.clip(lower=1.0)
    sso["high"] = sso[["open", "close", "high"]].max(axis=1)
    sso["low"] = sso[["open", "close", "low"]].min(axis=1)
    return MarketData(
        {"SPY": spy, "SSO": sso, "IBIT": bars(0.002, 0.045), "ETHA": bars(0.002, 0.055)},
        pd.Series(0.01, index=pd.date_range("2015-12-01", "2021-01-31")),
        eligible={"SPY": {2: "SSO"}},
    )


def golden_run() -> BacktestResult:
    return run_backtest(
        golden_data(),
        golden_config(),
        BacktestSettings(date(2017, 1, 3), date(2020, 12, 31), 10_000.0),
    )

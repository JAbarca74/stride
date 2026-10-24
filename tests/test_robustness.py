"""Robustness check of crypto_dip (docs/design/dip_robustness.md)."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from stride.backtest import robustness as r
from stride.backtest.engine import BacktestSettings, MarketData
from stride.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
DAYS = pd.bdate_range("2010-01-04", "2017-06-30")
RATE = pd.Series(0.01, index=pd.date_range("2009-12-01", "2017-07-31"))


def _market() -> MarketData:
    bars = {}
    for i, (symbol, vol) in enumerate({"SPY": 0.18, "IBIT": 0.7, "ETHA": 0.9}.items()):
        rng = np.random.default_rng(i)
        close = 100 * np.cumprod(1 + rng.normal(0.0005, vol / math.sqrt(252), len(DAYS)))
        bars[symbol] = pd.DataFrame(
            {"open": close, "high": close * 1.004, "low": close * 0.996, "close": close},
            index=DAYS,
        )
    return MarketData(bars, RATE)


def test_parameter_variants_move_each_dip_parameter_by_twenty_percent() -> None:
    variants = r.parameter_variants()
    assert variants["as registered"] == {}
    moved = [v for k, v in variants.items() if k != "as registered"]
    assert {"dip_drop": 0.2} in moved and {"dip_drop": 0.3} in moved
    assert {"dip_window": 72} in moved and {"dip_window": 108} in moved
    assert {"take_profit": 0.24} in moved and {"take_profit": 0.36} in moved
    assert len(moved) == 6


def test_exclusions_remove_good_and_bad_extremes_alike() -> None:
    sets = r.exclusion_sets({2016: 0.10, 2017: 1.90, 2018: -0.20, 2019: 0.15})
    assert sets["all years"] == ()
    assert sets["without the best (2017) and the worst (2018) year"] == (2017, 2018)
    assert set(sets["without all of them"]) == {2017, 2018, 2020, 2021, 2022}
    assert "all years" in r.exclusion_sets({})


def test_compare_leaves_the_same_years_out_of_both_series() -> None:
    data = _market()
    days = pd.DatetimeIndex(DAYS[DAYS.year >= 2015])
    returns = pd.Series(0.001, index=days)
    row = r.compare(returns, data, "x", years=(2016,))
    assert row["run"] == "x"
    assert float(row["cagr"]) > 0  # type: ignore[arg-type]
    assert isinstance(row["beats_spy"], bool)
    full = r.compare(returns, data, "x")
    assert full["spy_cagr"] != row["spy_cagr"]  # 2016 removed from SPY too


def test_check_reports_the_three_tests_and_a_verdict() -> None:
    settings = BacktestSettings(date(2010, 1, 4), date(2017, 6, 30), 10_000.0)
    result = r.check(
        _market(), CONFIG, settings, variants={"as registered": {}}, start_years=(2016,)
    )
    assert list(result.parameters["run"]) == ["as registered"]
    assert "all years" in set(result.exclusions["run"])
    assert list(result.starts["run"]) == ["continuous from 2016"]
    assert isinstance(result.robust, bool)
    text = r.to_markdown(result)
    assert "Verdict:" in text and "## 3." in text

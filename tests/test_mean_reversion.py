"""Round 7: the owner's 25-day mean-reversion sleeve signal and its candidates."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from stride.backtest.validation import (
    ROUND_7,
    candidate_config,
    candidate_levels,
    strategy_candidates,
)
from stride.config import load_config
from stride.strategy.allocation import mean_reversion_on, sleeve

ROOT = Path(__file__).resolve().parents[1]
BASE = load_config(ROOT / "config.yaml", env={})


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range("2024-01-01", periods=len(values)))


FLAT = [100.0 + (i % 2) for i in range(25)]  # mean 100.48, std about 0.51


def test_enters_after_a_two_sigma_drop() -> None:
    assert not mean_reversion_on(_series(FLAT), window=25, entry_z=2.0)
    assert mean_reversion_on(_series([*FLAT, 98.0]), window=25, entry_z=2.0)


def test_a_small_drop_does_not_enter() -> None:
    assert not mean_reversion_on(_series([*FLAT, 100.0]), window=25, entry_z=2.0)


def test_holds_below_the_mean_and_exits_at_it() -> None:
    assert mean_reversion_on(_series([*FLAT, 98.0, 99.0]), window=25, entry_z=2.0)
    assert not mean_reversion_on(_series([*FLAT, 98.0, 101.0]), window=25, entry_z=2.0)


def test_needs_a_full_window() -> None:
    assert not mean_reversion_on(_series([100.0, 101.0, 50.0]), window=25, entry_z=2.0)


def test_reads_no_future_closes() -> None:
    rng = np.random.default_rng(3)
    values = list(100 * np.cumprod(1 + rng.normal(0, 0.03, 300)))
    for cut in range(30, 300, 17):
        past = mean_reversion_on(_series(values[:cut]), window=25, entry_z=2.0)
        changed = [*values[:cut], *(v * 5 for v in values[cut:])]
        assert mean_reversion_on(_series(changed[:cut]), window=25, entry_z=2.0) == past


def test_sleeve_uses_the_signal() -> None:
    closes = pd.DataFrame({"SPY": [*FLAT, 100.0], "IBIT": [*FLAT, 98.0]},
                          index=pd.bdate_range("2024-01-01", periods=26))  # fmt: skip
    weights = sleeve(
        closes, 0.0, core=["SPY"], assets=["IBIT"], share=0.2, signal="mean_reversion",
        trend_sma=200, entry=55, exit=20, kelly_fraction=0.0, kelly_window=504,
    )  # fmt: skip
    assert weights == {"SPY": 0.8, "IBIT": 0.2}


def test_round_7_candidates_change_only_the_signal() -> None:
    candidates = strategy_candidates(BASE)
    pairs = {"crypto_mr": "crypto_dip", "qqq_core_mr": "qqq_core_dip"}
    assert set(ROUND_7) == set(pairs)
    for name, earlier in pairs.items():
        mr = candidate_config(BASE, name).experiments
        dip = candidate_config(BASE, earlier).experiments
        assert mr.allocation.signal == "mean_reversion"
        assert (mr.allocation.mr_window, mr.allocation.mr_entry_z) == (25, 2.0)
        assert mr.dd_scale_window == dip.dd_scale_window
        for key in ("mode", "core", "core_leverage", "assets", "sleeve", "rebalance",
                    "kelly_fraction"):  # fmt: skip
            assert getattr(mr.allocation, key) == getattr(dip.allocation, key), key
        assert candidate_levels(name, (1.0, 1.5, 2.0)) == (1.0,)

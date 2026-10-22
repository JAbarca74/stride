from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stride.config import StrategyConfig, load_config
from stride.strategy.rules import (
    ENTRY_HH55,
    EXIT_LL20,
    HOLD,
    PYRAMID,
    STOP_2N,
    TREND_FILTER,
    Indicators,
    PositionState,
    decide,
)
from stride.strategy.signals import SIGNAL_COLUMNS, generate_signals

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = load_config(ROOT / "config.yaml", env={}).strategy  # stop 2N, step 0.5N, max 4
NAN = math.nan
# SMA 100, HH 110, LL 95, N 4
BASE = Indicators(sma200=100.0, hh55=110.0, ll20=95.0, n=4.0)
LONG = PositionState(units=1, last_entry=108.0)


# One test per rule (SPEC section 6), each checking the rule name.


def test_trend_filter_closes_everything() -> None:
    decision = decide(99.0, BASE, PositionState(units=3, last_entry=120.0), STRATEGY)
    assert (decision.rule, decision.target_units, decision.last_entry) == (TREND_FILTER, 0, None)


def test_trend_filter_blocks_entry_even_above_hh55() -> None:
    ind = Indicators(sma200=200.0, hh55=110.0, ll20=95.0, n=4.0)
    assert decide(150.0, ind, PositionState(), STRATEGY).rule == TREND_FILTER


def test_stop_2n_closes_the_position() -> None:
    # last entry 108, N 4: stop at 100. Close 99.9 is above SMA 99 but below the stop.
    ind = Indicators(sma200=99.0, hh55=110.0, ll20=90.0, n=4.0)
    decision = decide(99.9, ind, LONG, STRATEGY)
    assert (decision.rule, decision.target_units) == (STOP_2N, 0)


def test_exit_ll20_closes_the_position() -> None:
    # Above the 2N stop (100) but below LL20 (102).
    ind = Indicators(sma200=90.0, hh55=110.0, ll20=102.0, n=4.0)
    decision = decide(101.0, ind, LONG, STRATEGY)
    assert (decision.rule, decision.target_units) == (EXIT_LL20, 0)


def test_entry_hh55_opens_one_unit() -> None:
    decision = decide(111.0, BASE, PositionState(), STRATEGY)
    assert (decision.rule, decision.target_units, decision.last_entry) == (ENTRY_HH55, 1, 111.0)


def test_pyramid_adds_one_unit_at_half_n() -> None:
    # last entry 108 + 0.5 * 4 = 110: reached exactly.
    decision = decide(110.0, BASE, LONG, STRATEGY)
    assert (decision.rule, decision.target_units, decision.last_entry) == (PYRAMID, 2, 110.0)


def test_hold_keeps_units() -> None:
    decision = decide(109.0, BASE, LONG, STRATEGY)
    assert (decision.rule, decision.target_units, decision.last_entry) == (HOLD, 1, 108.0)


def test_hold_without_position_below_breakout() -> None:
    decision = decide(105.0, BASE, PositionState(), STRATEGY)
    assert (decision.rule, decision.target_units) == (HOLD, 0)


# Priority and limits.


def test_entry_needs_close_strictly_above_hh55() -> None:
    assert decide(110.0, BASE, PositionState(), STRATEGY).rule == HOLD


def test_pyramid_stops_at_max_units() -> None:
    full = PositionState(units=STRATEGY.max_units_per_asset, last_entry=100.0)
    decision = decide(130.0, BASE, full, STRATEGY)
    assert (decision.rule, decision.target_units) == (HOLD, 4)


def test_pyramid_adds_only_one_unit_per_day() -> None:
    decision = decide(150.0, BASE, LONG, STRATEGY)  # far above several 0.5N steps
    assert (decision.rule, decision.target_units) == (PYRAMID, 2)


def test_stop_has_priority_over_channel_exit() -> None:
    ind = Indicators(sma200=90.0, hh55=110.0, ll20=105.0, n=4.0)
    assert decide(99.0, ind, LONG, STRATEGY).rule == STOP_2N


def test_exits_do_not_fire_without_a_position() -> None:
    ind = Indicators(sma200=90.0, hh55=110.0, ll20=105.0, n=4.0)
    assert decide(99.0, ind, PositionState(), STRATEGY).rule == HOLD


@pytest.mark.parametrize(
    "ind",
    [
        Indicators(NAN, 110.0, 95.0, 4.0),  # no SMA200 yet
    ],
)
def test_missing_sma_counts_as_failed_filter(ind: Indicators) -> None:
    assert decide(150.0, ind, LONG, STRATEGY).rule == TREND_FILTER


@pytest.mark.parametrize(
    "ind", [Indicators(100.0, NAN, 95.0, 4.0), Indicators(100.0, 110.0, 95.0, NAN)]
)
def test_missing_channel_or_n_never_opens_a_position(ind: Indicators) -> None:
    decision = decide(150.0, ind, PositionState(), STRATEGY)
    assert (decision.rule, decision.target_units) == (HOLD, 0)


def test_missing_n_never_adds_or_stops() -> None:
    ind = Indicators(100.0, 110.0, 95.0, NAN)
    decision = decide(150.0, ind, LONG, STRATEGY)
    assert (decision.rule, decision.target_units) == (HOLD, 1)


# generate_signals over a series.

SMALL = StrategyConfig(
    sma_filter=5,
    entry_breakout=4,
    exit_breakout=2,
    atr_period=3,
    stop_n=2.0,
    pyramid_step_n=0.5,
    max_units_per_asset=4,
)


def _prices(closes: Sequence[float]) -> pd.DataFrame:
    close = pd.Series(closes, index=pd.bdate_range("2026-01-05", periods=len(closes)))
    return pd.DataFrame({"high": close + 0.5, "low": close - 0.5, "close": close})


def test_signal_rows_carry_rule_and_units() -> None:
    closes = [10, 10, 10, 10, 10, 10, 12, 13, 14, 15, 16, 15, 14, 9, 9]
    signals = generate_signals(_prices(closes), SMALL)
    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    rules = signals["rule"].tolist()
    assert rules[6] == ENTRY_HH55  # first breakout once every window is full
    assert PYRAMID in rules[7:12]
    assert rules[13] in {TREND_FILTER, STOP_2N, EXIT_LL20}
    assert signals["target_units"].iloc[13] == 0
    # current units of each day are the previous day's target
    assert (signals["current_units"].iloc[1:].to_numpy() == signals["target_units"].iloc[:-1]
            .to_numpy()).all()  # fmt: skip
    assert signals["target_units"].max() <= SMALL.max_units_per_asset


def test_warmup_days_never_hold_units() -> None:
    signals = generate_signals(_prices([10.0 + i for i in range(4)]), SMALL)  # SMA5 never full
    assert (signals["target_units"] == 0).all()
    assert (signals["rule"] == TREND_FILTER).all()


def test_initial_position_is_respected() -> None:
    closes = [10.0 + 0.1 * i for i in range(12)]
    start = PositionState(units=2, last_entry=10.5)
    signals = generate_signals(_prices(closes), SMALL, initial=start)
    assert signals["current_units"].iloc[0] == 2


def test_future_prices_do_not_change_past_signals() -> None:
    rng = np.random.default_rng(11)
    closes = list(100 * np.cumprod(1 + rng.normal(0.002, 0.02, 120)))
    prices = _prices(closes)
    future = prices.copy()
    future.iloc[80:] *= 0.5  # inject fake future prices
    past = slice(None, prices.index[79])
    pd.testing.assert_frame_equal(
        generate_signals(prices, SMALL).loc[past], generate_signals(future, SMALL).loc[past]
    )


def test_signals_never_short() -> None:
    rng = np.random.default_rng(5)
    closes = list(100 * np.cumprod(1 + rng.normal(0.0, 0.03, 300)))
    signals = generate_signals(_prices(closes), SMALL)
    assert (signals["target_units"] >= 0).all()

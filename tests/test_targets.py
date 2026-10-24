from __future__ import annotations

import json
from collections import deque
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from golden_backtest import GOLDEN, golden_config, golden_data, golden_run
from stride.backtest import engine
from stride.backtest.targets import (
    ALLOCATE_IN,
    ALLOCATE_OUT,
    allocation_period,
    allocation_rules,
    allocation_targets,
    drawdown_scale,
    held_from_history,
    rebuild_state,
    scaling_drawdown,
    twr_history,
    update_instruments,
)
from stride.config import load_config
from stride.data import store
from stride.risk.kill_switch import TwrState
from stride.risk.leverage import HeldInstrument, InstrumentChoice
from stride.strategy.allocation import target_weights

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})


def test_backtest_equity_curve_is_unchanged_by_the_extraction() -> None:
    """The golden curve was recorded with the engine before `allocation_targets`
    was extracted; the engine must reproduce it exactly, float for float."""
    gold = json.loads(GOLDEN.read_text(encoding="utf-8"))
    result = golden_run()
    equity = result.equity["equity"]
    assert [d.date().isoformat() for d in equity.index] == gold["dates"]
    assert [float(x) for x in equity] == gold["equity"]
    assert len(result.orders) == gold["orders"]


def test_engine_re_exports_the_moved_names() -> None:
    assert engine.drawdown_scale is drawdown_scale
    assert engine.ALLOCATE_IN == ALLOCATE_IN


def test_targets_are_the_weights_times_leverage_and_scaling() -> None:
    config = golden_config()
    data = golden_data()
    closes = pd.DataFrame({s: data.bars[s]["close"] for s in ("SPY", "IBIT", "ETHA")})
    history = closes.loc[: pd.Timestamp("2019-06-28")]
    weights = target_weights(config.experiments.allocation, history, 0.01)
    out = allocation_targets(
        config, history, 0.01, 10_000.0, scaling_drawdown=0.0, held={},
        eligible={}, previous_weights={},
    )  # fmt: skip
    assert out.weights == weights
    assert out.scale == 1.0
    # 1.4x of exposure without leveraged versions: scaled down to the cash (SPEC 7, 2b).
    total = sum(w for w in weights.values() if w > 0)
    assert total > 1
    assert out.targets == pytest.approx(
        {a: w / total * 10_000.0 for a, w in weights.items() if w > 0}
    )
    scaled = allocation_targets(
        config, history, 0.01, 10_000.0, scaling_drawdown=0.30, held={},
        eligible={}, previous_weights={},
    )  # fmt: skip
    assert scaled.scale == config.experiments.dd_scale_floor
    assert sum(scaled.targets.values()) == pytest.approx(
        total * 10_000.0 * config.experiments.dd_scale_floor
    )


def test_targets_never_read_after_the_last_close() -> None:
    config = golden_config()
    data = golden_data()
    closes = pd.DataFrame({s: data.bars[s]["close"] for s in ("SPY", "IBIT", "ETHA")})
    day = pd.Timestamp("2018-03-01")
    kw = {"scaling_drawdown": 0.0, "held": {}, "eligible": {}, "previous_weights": {}}
    before = allocation_targets(config, closes.loc[:day], 0.01, 1_000.0, **kw)  # type: ignore[arg-type]
    shocked = closes.copy()
    shocked.loc[shocked.index > day] *= 0.1  # a fake crash after t
    after = allocation_targets(config, shocked.loc[:day], 0.01, 1_000.0, **kw)  # type: ignore[arg-type]
    assert before == after


def test_rules_mark_entries_and_exits() -> None:
    rules = allocation_rules({"SPY": 0.8, "IBIT": 0.1}, {"SPY": 0.8, "ETHA": 0.1})
    assert rules == {"SPY": "HOLD", "IBIT": ALLOCATE_IN, "ETHA": ALLOCATE_OUT}


def test_update_instruments_counts_hold_days_and_switches() -> None:
    spy1 = InstrumentChoice("SPY", "SPY", 1, 100.0, 100.0, "")
    sso = InstrumentChoice("SPY", "SSO", 2, 200.0, 100.0, "")
    held, chosen, switches = update_instruments({}, {}, {"SPY": spy1})
    assert (held, chosen, switches) == ({"SPY": HeldInstrument(1, 0)}, {"SPY": "SPY"}, [])
    held, chosen, switches = update_instruments(held, chosen, {"SPY": spy1})
    assert held == {"SPY": HeldInstrument(1, 1)}
    held, chosen, switches = update_instruments(held, chosen, {"SPY": sso})
    assert (held, switches) == ({"SPY": HeldInstrument(2, 0)}, [("SPY", "SPY", "SSO")])
    held, chosen, _ = update_instruments(held, chosen, {})
    assert (held, chosen) == ({}, {})  # a closed position forgets its instrument


def test_held_from_history_replays_the_hold_days() -> None:
    days = pd.bdate_range("2026-10-01", periods=4)
    rows = [(days[0], "SPY", "SPY", 1), (days[1], "SPY", "SPY", 1),
            (days[2], "SPY", "SSO", 2), (days[3], "SPY", "SSO", 2),
            (days[3], "IBIT", "IBIT", 1)]  # fmt: skip
    held, chosen = held_from_history(rows)
    assert held == {"SPY": HeldInstrument(2, 1), "IBIT": HeldInstrument(1, 0)}
    assert chosen == {"SPY": "SSO", "IBIT": "IBIT"}


def test_twr_ignores_deposits() -> None:
    days = pd.bdate_range("2026-10-01", periods=4)
    equity = pd.Series([100.0, 120.0, 140.0, 160.0], index=days)
    flows = pd.Series([20.0, 20.0, 20.0], index=days[1:])
    state, history = twr_history(equity, flows)
    assert history == (1.0, 1.0, 1.0, 1.0)
    assert state == TwrState(1.0, 1.0)


def test_scaling_drawdown_window_and_all_time() -> None:
    history = (1.0, 1.5, 1.2, 1.1)
    state = TwrState(1.1, 1.5)
    assert scaling_drawdown(state, history, 0) == pytest.approx(1 - 1.1 / 1.5)
    assert scaling_drawdown(state, history, 2) == pytest.approx(1 - 1.1 / 1.2)
    assert scaling_drawdown(state, (), 252) == pytest.approx(1 - 1.1 / 1.5)


@settings(max_examples=200, deadline=None)
@given(
    returns=st.lists(st.floats(min_value=-0.2, max_value=0.2), min_size=1, max_size=60),
    window=st.integers(min_value=0, max_value=30),
)
def test_scaling_drawdown_matches_the_engine(returns: list[float], window: int) -> None:
    """The engine keeps a deque of the last `window` index values; same answer."""
    state = TwrState()
    recent: deque[float] = deque(maxlen=window) if window else deque(maxlen=1)
    history = []
    for r in returns:
        index = state.index * (1 + r)
        state = TwrState(index, max(state.peak, index))
        recent.append(state.index)
        history.append(state.index)
    if not window:
        expected = min(max(state.drawdown, 0.0), 1.0)
    else:
        peak = max(recent)
        expected = min(max(1.0 - state.index / peak, 0.0), 1.0)
    assert scaling_drawdown(state, history, window) == expected


def test_allocation_period() -> None:
    day = pd.Timestamp("2026-10-23")
    assert allocation_period(day, "monthly") == (2026, 10)
    assert allocation_period(day, "daily") == (2026, 10, 23)


def test_state_rebuilt_from_the_tables_matches_the_backtest() -> None:
    """Store a backtest's equity, deposits, choices and last allocation, rebuild
    the paper state from the tables, and compare with the engine's own index."""
    result = golden_run()
    conn = store.connect(":memory:")
    store.upsert_equity(conn, result.equity)
    for day, amount in zip(
        pd.DatetimeIndex(result.cash_flows.index), result.cash_flows, strict=True
    ):
        store.record_cash_flow(conn, day.date(), float(amount), "deposit", "test")
    last = result.equity.index[-1].date()
    store.save_instruments(conn, last, {"SPY": ("SSO", 2)})
    store.save_allocation(conn, last, {"SPY": 0.8, "IBIT": 0.2})
    equity = store.read_equity(conn)["equity"]
    state = rebuild_state(
        equity, store.net_cash_flows(conn), store.read_instruments(conn),
        store.last_allocation(conn), "daily",
    )  # fmt: skip
    assert state.twr.index == pytest.approx(float(result.equity["twr_index"].iloc[-1]), rel=1e-12)
    assert len(state.twr_history) == len(equity)
    assert state.held == {"SPY": HeldInstrument(2, 0)}
    assert state.last_period == (last.year, last.month, last.day)
    assert state.weights == {"SPY": 0.8, "IBIT": 0.2}


def test_store_allocations_and_instruments() -> None:
    conn = store.connect(":memory:")
    assert store.last_allocation(conn) is None
    store.save_allocation(conn, date(2026, 10, 1), {})
    store.save_allocation(conn, date(2026, 10, 2), {"SPY": 1.0})
    assert store.last_allocation(conn) == (pd.Timestamp("2026-10-02"), {"SPY": 1.0})
    assert store.last_allocation(conn, before=date(2026, 10, 2)) == (pd.Timestamp("2026-10-01"), {})
    store.save_instruments(conn, date(2026, 10, 2), {"SPY": ("SPY", 1)})
    store.save_instruments(conn, date(2026, 10, 2), {"SPY": ("SSO", 2)})  # replaces the day
    assert store.read_instruments(conn) == [(pd.Timestamp("2026-10-02"), "SPY", "SSO", 2)]


def test_rebuild_with_no_history() -> None:
    state = rebuild_state(pd.Series(dtype=float), pd.Series(dtype=float), [], None, "monthly")
    assert (state.twr, state.twr_history, state.held, state.last_period) == (
        TwrState(), (), {}, None)  # fmt: skip
    assert np.isclose(drawdown_scale(0.0, CONFIG), 1.0)

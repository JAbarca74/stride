from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stride.config import load_config
from stride.data.store import connect, load_twr, read_events, save_twr
from stride.execution.kill_switch_file import (
    KillSwitchError,
    fire,
    is_active,
    main,
    manual_reset,
)
from stride.risk.kill_switch import (
    DRAWDOWN,
    RECONCILIATION,
    KillDecision,
    TwrState,
    advance,
    equity_from_positions,
    kill_switch_decision,
    reconciliation_breaks,
)

ROOT = Path(__file__).resolve().parents[1]
RISK = load_config(ROOT / "config.yaml", env={}).risk  # kill 0.40, 1%, $1 floor
TOL, FLOOR = RISK.reconciliation_tolerance, RISK.reconciliation_floor_usd


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


# Time-weighted index (SPEC 1.3).


def test_deposits_do_not_move_the_index() -> None:
    state = advance(TwrState(), previous_equity=100.0, equity=120.0, net_flow=20.0)
    assert state == TwrState(1.0, 1.0)


def test_index_tracks_returns_and_the_peak_never_falls() -> None:
    state = advance(TwrState(), 100.0, 110.0, 0.0)  # +10%
    state = advance(state, 110.0, 99.0, 0.0)  # -10%
    assert state.index == pytest.approx(0.99)
    assert state.peak == pytest.approx(1.10)
    assert state.drawdown == pytest.approx(1 - 0.99 / 1.10)


def test_deposit_cannot_hide_a_drawdown() -> None:
    # Market -50% on the day a $20 deposit arrives: balance 70, but the index halves.
    state = advance(TwrState(), 100.0, 70.0, 20.0)
    assert state.drawdown == pytest.approx(0.5)


def test_invalid_equity_is_rejected() -> None:
    with pytest.raises(ValueError, match="previous_equity"):
        advance(TwrState(), 0.0, 10.0, 0.0)
    with pytest.raises(ValueError, match="finite"):
        advance(TwrState(), 100.0, math.nan, 0.0)


# Decision.


def test_fires_at_minus_40_not_before() -> None:
    assert not kill_switch_decision(TwrState(0.601, 1.0), 0.40, [], []).fire  # -39.9%
    decision = kill_switch_decision(TwrState(0.60, 1.0), 0.40, [], [])  # exactly -40%
    assert decision.fire and decision.reasons == (DRAWDOWN,)


def test_reconciliation_needs_two_consecutive_days() -> None:
    assert not kill_switch_decision(TwrState(), 0.40, ["SPY"], []).fire
    assert not kill_switch_decision(TwrState(), 0.40, [], ["SPY"]).fire
    decision = kill_switch_decision(TwrState(), 0.40, ["SPY"], ["GLD"])
    assert decision.fire and decision.reasons == (RECONCILIATION,)


def test_both_reasons_together() -> None:
    decision = kill_switch_decision(TwrState(0.5, 1.0), 0.40, ["SPY"], ["SPY"])
    assert decision.reasons == (DRAWDOWN, RECONCILIATION)


def test_non_finite_drawdown_fires() -> None:
    assert kill_switch_decision(TwrState(math.nan, 1.0), 0.40, [], []).fire


# Reconciliation per symbol with a $1 floor.


def test_small_rounding_is_not_a_break() -> None:
    # $30 position: 1% is $0.30, so the $1 floor applies.
    assert reconciliation_breaks({"SPY": 30.0}, {"SPY": 30.9}, TOL, FLOOR) == []
    assert reconciliation_breaks({"SPY": 30.0}, {"SPY": 31.01}, TOL, FLOOR) == ["SPY"]


def test_large_positions_use_one_percent() -> None:
    assert reconciliation_breaks({"SPY": 1_000.0}, {"SPY": 1_009.0}, TOL, FLOOR) == []
    assert reconciliation_breaks({"SPY": 1_000.0}, {"SPY": 1_011.0}, TOL, FLOOR) == ["SPY"]


def test_one_sided_symbols_are_breaks() -> None:
    assert reconciliation_breaks({"SPY": 50.0}, {}, TOL, FLOOR) == ["SPY"]
    assert reconciliation_breaks({}, {"GLD": 0.5}, TOL, FLOOR) == ["GLD"]


def test_equity_from_positions_as_in_the_backtest() -> None:
    assert equity_from_positions({"SPY": 2.0, "GLD": 0.0}, {"SPY": 500.0}, 50.0) == 1_050.0
    with pytest.raises(ValueError, match="no close"):
        equity_from_positions({"TLT": 1.0}, {}, 0.0)


# Stored peak, events and the KILL_SWITCH file.


def test_peak_survives_restarts_and_never_falls(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert load_twr(conn) == (1.0, 1.0)
    save_twr(conn, 1.2, 1.2)
    save_twr(conn, 0.9, 0.9)  # a lower "peak" is ignored
    assert load_twr(conn) == (0.9, 1.2)
    db = tmp_path / "stride.db"
    first = connect(db)
    save_twr(first, 1.1, 1.3)
    first.close()
    reopened = connect(db)
    assert load_twr(reopened) == (1.1, 1.3)
    reopened.close()


def test_fire_writes_the_file_once(conn: sqlite3.Connection, tmp_path: Path) -> None:
    switch = tmp_path / "KILL_SWITCH"
    decision = KillDecision(True, (DRAWDOWN,), 0.41)
    assert fire(conn, decision, switch)
    assert is_active(switch)
    first = switch.read_text(encoding="utf-8")
    assert "DRAWDOWN; drawdown 41.00%" in first
    assert not fire(conn, KillDecision(True, (RECONCILIATION,), 0.1), switch)
    assert switch.read_text(encoding="utf-8") == first  # first reason kept
    assert [e[1] for e in read_events(conn)] == ["KILL_SWITCH", "KILL_SWITCH_STILL_ACTIVE"]


def test_no_fire_no_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    switch = tmp_path / "KILL_SWITCH"
    assert not fire(conn, KillDecision(False, (), 0.1), switch)
    assert not is_active(switch)


def test_reset_needs_the_file_deleted_and_a_name(conn: sqlite3.Connection, tmp_path: Path) -> None:
    switch = tmp_path / "KILL_SWITCH"
    save_twr(conn, 0.55, 1.0)
    fire(conn, KillDecision(True, (DRAWDOWN,), 0.45), switch)
    with pytest.raises(KillSwitchError, match="delete the KILL_SWITCH file"):
        manual_reset(conn, "Owner", "reviewed", switch)
    switch.unlink()  # the human deletes it by hand
    with pytest.raises(ValueError, match="name"):
        manual_reset(conn, " ", "reviewed", switch)
    manual_reset(conn, "Owner", "account reviewed", switch)
    assert load_twr(conn) == (0.55, 0.55)  # peak reset to the current index
    [(_, _, detail)] = read_events(conn, "PEAK_RESET")
    assert "by Owner: account reviewed" in detail


def test_cli_status_and_reset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db, switch = str(tmp_path / "s.db"), str(tmp_path / "KILL_SWITCH")
    assert main(["status", "--file", switch]) == 0
    assert "inactive" in capsys.readouterr().out
    Path(switch).write_text("x", encoding="utf-8")
    assert main(["status", "--file", switch]) == 0
    assert "ACTIVE" in capsys.readouterr().out
    assert main(["reset", "--db", db, "--file", switch]) == 2  # missing --by/--reason
    args = ["reset", "--by", "Owner", "--reason", "ok", "--db", db, "--file", switch]
    assert main(args) == 1  # file still there
    Path(switch).unlink()
    assert main(args) == 0
    assert "reset recorded" in capsys.readouterr().out


# Invariants (hypothesis).


@settings(max_examples=1_000, deadline=None)
@given(
    index=st.floats(min_value=0, max_value=10),
    peak=st.floats(min_value=0.01, max_value=10),
    today=st.lists(st.sampled_from(["SPY", "GLD"]), max_size=2),
    yesterday=st.lists(st.sampled_from(["SPY", "GLD"]), max_size=2),
)
def test_kill_switch_always_fires_at_or_beyond_40_percent(
    index: float, peak: float, today: list[str], yesterday: list[str]
) -> None:
    state = TwrState(min(index, peak), peak)
    decision = kill_switch_decision(state, RISK.kill_switch_dd, today, yesterday)
    if state.drawdown >= RISK.kill_switch_dd:
        assert decision.fire and DRAWDOWN in decision.reasons
    elif not (today and yesterday):
        assert not decision.fire
    assert decision.fire == bool(decision.reasons)


@settings(max_examples=500, deadline=None)
@given(
    steps=st.lists(
        st.tuples(
            st.floats(min_value=-0.5, max_value=0.5),  # market return of the day
            st.floats(min_value=-50, max_value=200),  # external flow
        ),
        min_size=1,
        max_size=30,
    )
)
def test_index_ignores_flows_and_peak_is_monotone(steps: list[tuple[float, float]]) -> None:
    state, equity, product, peak = TwrState(), 100.0, 1.0, 1.0
    for market, raw_flow in steps:
        flow = max(raw_flow, -equity * (1 + market) * 0.9)  # never withdraw everything
        new_equity = equity * (1 + market) + flow
        state = advance(state, equity, new_equity, flow)
        product *= 1 + market
        assert state.peak >= peak
        peak = state.peak
        equity = new_equity
    assert state.index == pytest.approx(product, rel=1e-9)  # flows never count
    assert 0 <= state.drawdown <= 1

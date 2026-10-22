from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date

import pandas as pd
import pytest

from stride.backtest.metrics import cagr, deposits_vs_balance, time_weighted_returns
from stride.data.store import connect, net_cash_flows, record_cash_flow

DAYS = pd.bdate_range("2026-01-02", periods=60)
STAMP = pd.Timestamp("2026-10-21 18:00", tz="UTC")


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


def _first_days_of_month(days: pd.DatetimeIndex) -> list[pd.Timestamp]:
    return [group.iloc[0] for _, group in days.to_series().groupby([days.year, days.month])]


def test_twr_is_zero_with_20_dollar_deposits_and_no_return() -> None:
    # $100 start, $20 on the first trading day of each month, 0% market return.
    deposit_days = _first_days_of_month(DAYS)[1:]  # Feb and Mar 2026
    flows = pd.Series(20.0, index=pd.DatetimeIndex(deposit_days))
    equity = pd.Series(100.0, index=DAYS)
    for day in deposit_days:
        equity[equity.index >= day] += 20.0
    returns = time_weighted_returns(equity, flows)
    assert (returns.abs() < 1e-12).all()
    assert cagr(returns) == pytest.approx(0.0)
    assert equity.iloc[-1] == 140.0  # the balance grew only by deposits


def test_twr_ignores_deposits_but_keeps_market_moves() -> None:
    days = DAYS[:3]
    equity = pd.Series([100.0, 130.0, 143.0], index=days)  # +10% then +10%, $20 on day 2
    flows = pd.Series([20.0], index=[days[1]])
    returns = time_weighted_returns(equity, flows)
    assert returns.tolist() == pytest.approx([0.10, 0.10])


def test_withdrawals_are_negative_flows() -> None:
    days = DAYS[:2]
    equity = pd.Series([100.0, 80.0], index=days)
    returns = time_weighted_returns(equity, pd.Series([-20.0], index=[days[1]]))
    assert returns.iloc[0] == pytest.approx(0.0)


def test_without_flows_twr_is_the_plain_return() -> None:
    equity = pd.Series([100.0, 110.0, 99.0], index=DAYS[:3])
    assert time_weighted_returns(equity).tolist() == pytest.approx([0.10, -0.10])


def test_deposits_vs_balance() -> None:
    days = DAYS[:3]
    equity = pd.Series([100.0, 125.0, 120.0], index=days)
    flows = pd.Series([100.0, 20.0], index=[days[0], days[1]])
    table = deposits_vs_balance(equity, flows)
    assert table["deposited"].tolist() == [100.0, 120.0, 120.0]
    assert table["balance"].tolist() == [100.0, 125.0, 120.0]


def test_cash_flows_table_round_trip(conn: sqlite3.Connection) -> None:
    record_cash_flow(conn, date(2026, 2, 2), 20.0, "deposit", "cash increase without a sale",
                     recorded_at=STAMP)  # fmt: skip
    record_cash_flow(conn, date(2026, 2, 2), 5.0, "withdrawal", "manual", recorded_at=STAMP)
    record_cash_flow(conn, date(2026, 3, 2), 20.0, "deposit", "cash increase without a sale")
    net = net_cash_flows(conn)
    assert list(net.index) == [pd.Timestamp("2026-02-02"), pd.Timestamp("2026-03-02")]
    assert net.tolist() == [15.0, 20.0]
    row = conn.execute("SELECT type, detected_by, recorded_at FROM cash_flows LIMIT 1").fetchone()
    assert row == ("deposit", "cash increase without a sale", STAMP.isoformat())


def test_cash_flow_validation(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="type must be"):
        record_cash_flow(conn, date(2026, 2, 2), 20.0, "dividend", "x")
    with pytest.raises(sqlite3.IntegrityError):
        record_cash_flow(conn, date(2026, 2, 2), -20.0, "deposit", "x")


def test_empty_cash_flows(conn: sqlite3.Connection) -> None:
    assert net_cash_flows(conn).empty

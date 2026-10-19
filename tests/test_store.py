from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from stride.data.store import PRICE_COLUMNS, connect, read_prices, upsert_prices

FETCHED = pd.Timestamp("2026-10-19 20:30", tz="UTC")


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": "SPY",
        "date": pd.Timestamp("2026-10-16"),
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 1_000,
        "source": "tiingo",
        "fetched_at": FETCHED,
    }
    row.update(overrides)
    return row


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


def _count(conn: sqlite3.Connection) -> int:
    count: int = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    return count


def test_inserting_the_same_row_twice_does_not_duplicate_it(conn: sqlite3.Connection) -> None:
    frame = pd.DataFrame([_row()])
    upsert_prices(conn, frame)
    upsert_prices(conn, frame)
    assert _count(conn) == 1


def test_upsert_updates_existing_row(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, pd.DataFrame([_row()]))
    upsert_prices(conn, pd.DataFrame([_row(close=101.5, source="alpaca")]))
    stored = read_prices(conn, "SPY")
    assert len(stored) == 1
    assert stored["close"].iloc[0] == 101.5
    assert stored["source"].iloc[0] == "alpaca"


def test_same_date_different_symbols_are_separate_rows(conn: sqlite3.Connection) -> None:
    written = upsert_prices(conn, pd.DataFrame([_row(), _row(symbol="EFA")]))
    assert written == 2
    assert _count(conn) == 2


def test_date_may_be_the_index(conn: sqlite3.Connection) -> None:
    frame = pd.DataFrame([_row()]).set_index("date")
    upsert_prices(conn, frame)
    assert _count(conn) == 1


def test_read_prices_round_trip_and_range(conn: sqlite3.Connection) -> None:
    rows = [_row(date=pd.Timestamp(day)) for day in ("2026-10-14", "2026-10-15", "2026-10-16")]
    upsert_prices(conn, pd.DataFrame(rows[::-1]))
    stored = read_prices(conn, "SPY", start=date(2026, 10, 15), end=date(2026, 10, 16))
    assert list(stored.index) == [pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-16")]
    assert stored.index.name == "date"
    assert pd.DatetimeIndex(stored.index).tz is None
    assert list(stored.columns) == [c for c in PRICE_COLUMNS if c != "date"]
    assert stored["fetched_at"].iloc[0] == FETCHED
    assert stored["volume"].iloc[0] == 1_000


def test_read_prices_unknown_symbol_is_empty(conn: sqlite3.Connection) -> None:
    assert read_prices(conn, "GLD").empty


def test_missing_column_is_rejected(conn: sqlite3.Connection) -> None:
    frame = pd.DataFrame([_row()]).drop(columns=["source"])
    with pytest.raises(ValueError, match="source"):
        upsert_prices(conn, frame)


@pytest.mark.parametrize(
    "overrides",
    [
        {"low": 100.5},  # low above open
        {"high": 100.5},  # high below close
        {"volume": -1},
    ],
)
def test_contract_violations_are_rejected(
    conn: sqlite3.Connection, overrides: dict[str, Any]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        upsert_prices(conn, pd.DataFrame([_row(**overrides)]))
    assert _count(conn) == 0


def test_connect_creates_parent_folder(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "stride.db"
    connection = connect(db_path)
    upsert_prices(connection, pd.DataFrame([_row()]))
    connection.close()
    reopened = connect(db_path)
    assert _count(reopened) == 1
    reopened.close()

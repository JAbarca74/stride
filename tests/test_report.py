from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stride.config import load_config
from stride.data.sources import to_contract
from stride.data.store import (
    connect,
    net_cash_flows,
    read_equity,
    record_cash_flow,
    upsert_equity,
    upsert_prices,
)
from stride.reporting.report import build_report, write_report

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
DAYS = pd.bdate_range("2025-01-02", periods=300)
START, END = DAYS[0].date(), DAYS[-1].date()
NOW = pd.Timestamp("2026-10-21", tz="UTC").to_pydatetime()


def _close(seed: int, base: np.ndarray | None = None) -> np.ndarray:
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0004, 0.01, len(DAYS))
    if base is not None:
        returns = 0.9 * base + 0.1 * returns
    return 100 * np.cumprod(1 + returns)


def _store_prices(conn: sqlite3.Connection, symbol: str, close: np.ndarray) -> None:
    frame = pd.DataFrame({"date": DAYS, "open": close, "high": close * 1.01,
                          "low": close * 0.99, "close": close, "volume": 1})  # fmt: skip
    upsert_prices(conn, to_contract(frame, symbol, "test", NOW))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """A test database with prices, a daily equity series and monthly deposits."""
    connection = connect(":memory:")
    rng = np.random.default_rng(1)
    spy_returns = rng.normal(0.0004, 0.01, len(DAYS))
    _store_prices(connection, "SPY", 100 * np.cumprod(1 + spy_returns))
    _store_prices(connection, "EFA", _close(2, base=spy_returns))  # correlated with SPY
    _store_prices(connection, "IEF", _close(3))
    _store_prices(connection, "GLD", _close(4))
    value = 100 * np.cumprod(1 + rng.normal(0.0003, 0.008, len(DAYS)))
    deposit_days = [d for d in DAYS[1:] if d.month != (d - pd.offsets.BDay(1)).month]
    for day in deposit_days:
        record_cash_flow(connection, day.date(), 20.0, "deposit", "test")
    deposits = pd.Series(np.where(DAYS.isin(deposit_days), 20.0, 0.0), index=DAYS)
    equity = value + deposits.cumsum().to_numpy()
    upsert_equity(
        connection,
        pd.DataFrame(
            {"equity": equity, "cash": equity * 0.3, "gross_exposure": equity * 1.2,
             "leverage": 1.2},
            index=DAYS,
        ),
    )  # fmt: skip
    yield connection
    connection.close()


def test_report_is_generated_from_a_test_database(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = write_report(conn, CONFIG, START, END, tmp_path / "reports" / "2026-09.html")
    html = path.read_text(encoding="utf-8")
    assert path.exists()
    assert html.startswith("<!doctype html>")
    assert "data:image/png;base64," in html  # equity curve
    for header in ("CAGR", "Volatility", "Sharpe", "Sortino", "Max drawdown", "Calmar"):
        assert f"<th>{header}</th>" in html
    for series in ("<td>STRIDE</td>", "<td>SPY</td>", "<td>60/40</td>"):
        assert series in html


def test_report_shows_deposits_exposure_and_correlations(conn: sqlite3.Connection) -> None:
    html = build_report(conn, CONFIG, START, END)
    assert "Total deposited:" in html
    assert "Average exposure (leverage used): 1.20x" in html
    assert "Pairs above 0.7:" in html
    assert "SPY/EFA" in html
    assert "IBIT" not in html  # symbols without prices are left out of the matrix


def test_deposits_do_not_count_as_gains(conn: sqlite3.Connection) -> None:
    equity = read_equity(conn)
    deposits = float(net_cash_flows(conn).sum())  # $20 per month
    assert deposits == 20.0 * 13  # first trading days of Feb 2025 to Feb 2026
    deposited_total = float(equity["equity"].iloc[0]) + deposits
    html = build_report(conn, CONFIG, START, END)
    assert f"Total deposited: ${deposited_total:,.2f}" in html


def test_report_needs_equity(tmp_path: Path) -> None:
    empty = connect(":memory:")
    with pytest.raises(ValueError, match="at least two days"):
        build_report(empty, CONFIG, START, END)
    empty.close()


def test_report_without_benchmark_prices(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM prices")
    html = build_report(conn, CONFIG, START, END)
    assert "<td>STRIDE</td>" in html
    assert "<td>SPY</td>" not in html
    assert "Not enough price history." in html


def test_equity_table_round_trip(conn: sqlite3.Connection) -> None:
    equity = read_equity(conn, DAYS[10].date(), DAYS[12].date())
    assert list(equity.index) == list(DAYS[10:13])
    assert list(equity.columns) == ["equity", "cash", "gross_exposure", "leverage"]
    with pytest.raises(ValueError, match="missing equity columns"):
        upsert_equity(conn, equity.drop(columns=["cash"]))


def test_html_escapes_text(conn: sqlite3.Connection) -> None:
    assert "<script" not in build_report(conn, CONFIG, START, END).lower()


def test_report_period_dates(conn: sqlite3.Connection) -> None:
    html = build_report(conn, CONFIG, date(2025, 3, 3), date(2025, 6, 30))
    assert "Period 2025-03-03 to 2025-06-30" in html

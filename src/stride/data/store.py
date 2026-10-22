"""SQLite storage for daily prices (SPEC section 5, `prices` table)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

PRICE_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "fetched_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol     TEXT    NOT NULL,
    date       TEXT    NOT NULL,
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     INTEGER NOT NULL CHECK (volume >= 0),
    source     TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL,
    PRIMARY KEY (symbol, date),
    CHECK (low <= open AND open <= high),
    CHECK (low <= close AND close <= high)
)
"""

_UPSERT = f"""
INSERT INTO prices ({", ".join(PRICE_COLUMNS)})
VALUES ({", ".join("?" for _ in PRICE_COLUMNS)})
ON CONFLICT (symbol, date) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume,
    source = excluded.source,
    fetched_at = excluded.fetched_at
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database at `db_path` (creating parent folders) and ensure the schema."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_schema(conn)
    return conn


_CASH_FLOWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    amount      REAL    NOT NULL CHECK (amount > 0),
    type        TEXT    NOT NULL CHECK (type IN ('deposit', 'withdrawal')),
    detected_by TEXT    NOT NULL,
    recorded_at TEXT    NOT NULL
)
"""

CASH_FLOW_TYPES = ("deposit", "withdrawal")

_EQUITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS equity (
    date           TEXT PRIMARY KEY,
    equity         REAL NOT NULL,
    cash           REAL NOT NULL,
    gross_exposure REAL NOT NULL CHECK (gross_exposure >= 0),
    leverage       REAL NOT NULL CHECK (leverage >= 0)
)
"""
EQUITY_COLUMNS = ("equity", "cash", "gross_exposure", "leverage")


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the `prices`, `cash_flows` and `equity` tables if they do not exist."""
    with conn:
        conn.execute(_SCHEMA)
        conn.execute(_CASH_FLOWS_SCHEMA)
        conn.execute(_EQUITY_SCHEMA)


def upsert_prices(conn: sqlite3.Connection, prices: pd.DataFrame) -> int:
    """Insert or update rows keyed by (symbol, date); returns the number of rows written.

    `prices` must have the section 5 contract columns. `date` may be a column or the
    index; `fetched_at` must be a UTC timestamp.
    """
    frame = prices.reset_index() if "date" not in prices.columns else prices
    missing = [c for c in PRICE_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"missing price columns: {missing}")
    # Reject bad downloads instead of coercing them into plausible rows.
    empty = [c for c in PRICE_COLUMNS if frame[c].isna().any()]
    if empty:
        raise ValueError(f"empty values in price columns: {empty}")
    volume = pd.to_numeric(frame["volume"])
    if not (volume == volume.round()).all():
        raise ValueError("volume must be a whole number")
    rows = list(
        zip(
            frame["symbol"].astype(str).tolist(),
            pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d").tolist(),
            *(frame[c].astype(float).tolist() for c in ("open", "high", "low", "close")),
            volume.astype("int64").tolist(),
            frame["source"].astype(str).tolist(),
            [ts.isoformat() for ts in pd.to_datetime(frame["fetched_at"], utc=True)],
            strict=True,
        )
    )
    with conn:
        conn.executemany(_UPSERT, rows)
    return len(rows)


def read_prices(
    conn: sqlite3.Connection,
    symbol: str,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Prices for `symbol` indexed by `date` (timezone-naive), oldest first."""
    query = "SELECT * FROM prices WHERE symbol = ?"
    params: list[str] = [symbol]
    # Bind plain ISO dates: a Timestamp's isoformat() adds "T00:00:00", which sorts
    # after the stored "YYYY-MM-DD" text and would drop the start day.
    if start is not None:
        query += " AND date >= ?"
        params.append(pd.Timestamp(start).date().isoformat())
    if end is not None:
        query += " AND date <= ?"
        params.append(pd.Timestamp(end).date().isoformat())
    query += " ORDER BY date"
    frame = pd.read_sql_query(query, conn, params=params)
    frame["date"] = pd.to_datetime(frame["date"])
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], utc=True)
    return frame.set_index("date")


def last_price_date(conn: sqlite3.Connection, symbol: str) -> date | None:
    """Date of the newest stored bar for `symbol`, or None if there is none."""
    row = conn.execute("SELECT MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()
    return None if row[0] is None else date.fromisoformat(row[0])


def record_cash_flow(
    conn: sqlite3.Connection,
    day: date,
    amount: float,
    kind: str,
    detected_by: str,
    *,
    recorded_at: pd.Timestamp | None = None,
) -> None:
    """Store one deposit or withdrawal (SPEC section 10). `amount` is positive;
    `detected_by` says how it was found (e.g. "cash increase without a sale")."""
    if kind not in CASH_FLOW_TYPES:
        raise ValueError(f"type must be one of {CASH_FLOW_TYPES}")
    stamp = recorded_at if recorded_at is not None else pd.Timestamp.now(tz="UTC")
    with conn:
        conn.execute(
            "INSERT INTO cash_flows (date, amount, type, detected_by, recorded_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (day.isoformat(), float(amount), kind, detected_by, stamp.isoformat()),
        )


def net_cash_flows(conn: sqlite3.Connection) -> pd.Series:
    """Net external flow per day: deposits positive, withdrawals negative."""
    frame = pd.read_sql_query(
        "SELECT date, SUM(CASE type WHEN 'deposit' THEN amount ELSE -amount END) AS net"
        " FROM cash_flows GROUP BY date ORDER BY date",
        conn,
    )
    return pd.Series(frame["net"].to_numpy(dtype=float), index=pd.to_datetime(frame["date"]))


def upsert_equity(conn: sqlite3.Connection, equity: pd.DataFrame) -> int:
    """Store one row per day (SPEC section 10): equity, cash, gross exposure, leverage.

    `equity` is indexed by date; a day already stored is replaced.
    """
    missing = [c for c in EQUITY_COLUMNS if c not in equity.columns]
    if missing:
        raise ValueError(f"missing equity columns: {missing}")
    rows = list(
        zip(
            pd.DatetimeIndex(equity.index).strftime("%Y-%m-%d").tolist(),
            *(equity[c].astype(float).tolist() for c in EQUITY_COLUMNS),
            strict=True,
        )
    )
    with conn:
        conn.executemany(
            "INSERT INTO equity (date, equity, cash, gross_exposure, leverage)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (date) DO UPDATE SET"
            " equity = excluded.equity, cash = excluded.cash,"
            " gross_exposure = excluded.gross_exposure, leverage = excluded.leverage",
            rows,
        )
    return len(rows)


def read_equity(
    conn: sqlite3.Connection, start: date | None = None, end: date | None = None
) -> pd.DataFrame:
    """Daily equity rows indexed by date, oldest first."""
    query = "SELECT * FROM equity WHERE 1 = 1"
    params: list[str] = []
    if start is not None:
        query += " AND date >= ?"
        params.append(pd.Timestamp(start).date().isoformat())
    if end is not None:
        query += " AND date <= ?"
        params.append(pd.Timestamp(end).date().isoformat())
    frame = pd.read_sql_query(query + " ORDER BY date", conn, params=params)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date")

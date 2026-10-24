"""SQLite storage for daily prices (SPEC section 5, `prices` table)."""

from __future__ import annotations

import math
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

_RISK_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_state (
    key        TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    type   TEXT NOT NULL,
    detail TEXT NOT NULL
)
"""


_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    date          TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    close         REAL    NOT NULL,
    sma200        REAL,
    hh55          REAL,
    ll20          REAL,
    n             REAL,
    rule          TEXT    NOT NULL,
    current_units INTEGER NOT NULL CHECK (current_units >= 0),
    target_units  INTEGER NOT NULL CHECK (target_units >= 0),
    last_entry    REAL,
    capped        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, symbol)
)
"""
SIGNAL_TABLE_COLUMNS = (
    "close",
    "sma200",
    "hh55",
    "ll20",
    "n",
    "rule",
    "current_units",
    "target_units",
    "last_entry",
    "capped",
)

_RISK_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_decisions (
    date           TEXT PRIMARY KEY,
    l_star         REAL,
    kelly_days     INTEGER NOT NULL,
    sigma_60       REAL    NOT NULL,
    leverage       REAL    NOT NULL CHECK (leverage >= 0),
    binding_limit  TEXT    NOT NULL,
    drawdown       REAL    NOT NULL,
    delever_active INTEGER NOT NULL,
    k              REAL    NOT NULL,
    vetoes         TEXT    NOT NULL
)
"""
RISK_DECISION_COLUMNS = (
    "l_star",
    "kelly_days",
    "sigma_60",
    "leverage",
    "binding_limit",
    "drawdown",
    "delever_active",
    "k",
    "vetoes",
)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create every table (prices, cash_flows, equity, risk_state, events, signals,
    risk_decisions) if missing."""
    with conn:
        conn.execute(_SCHEMA)
        conn.execute(_CASH_FLOWS_SCHEMA)
        conn.execute(_EQUITY_SCHEMA)
        conn.execute(_RISK_STATE_SCHEMA)
        conn.execute(_EVENTS_SCHEMA)
        conn.execute(_SIGNALS_SCHEMA)
        conn.execute(_RISK_DECISIONS_SCHEMA)


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


def _now() -> str:
    return pd.Timestamp.now(tz="UTC").isoformat()


def record_event(conn: sqlite3.Connection, kind: str, detail: str, at: str | None = None) -> None:
    """Append one row to `events` (SPEC section 10): alerts, errors, kill switch, resets."""
    with conn:
        conn.execute(
            "INSERT INTO events (at, type, detail) VALUES (?, ?, ?)", (at or _now(), kind, detail)
        )


def read_events(conn: sqlite3.Connection, kind: str | None = None) -> list[tuple[str, str, str]]:
    query = "SELECT at, type, detail FROM events"
    params: tuple[str, ...] = ()
    if kind is not None:
        query += " WHERE type = ?"
        params = (kind,)
    return [tuple(row) for row in conn.execute(query + " ORDER BY id", params).fetchall()]


TWR_INDEX = "twr_index"
TWR_PEAK = "twr_peak"


def load_twr(conn: sqlite3.Connection) -> tuple[float, float]:
    """Stored time-weighted index and its peak; (1.0, 1.0) before the first run."""
    rows = dict(conn.execute("SELECT key, value FROM risk_state").fetchall())
    return float(rows.get(TWR_INDEX, 1.0)), float(rows.get(TWR_PEAK, 1.0))


def save_twr(conn: sqlite3.Connection, index: float, peak: float) -> None:
    """Store the index and peak. The peak never goes down here; only
    `reset_twr_peak` can lower it. A non-finite or negative value is refused, so a
    bad index can never be stored as the peak."""
    if not (math.isfinite(index) and math.isfinite(peak)) or index < 0 or peak <= 0:
        raise ValueError("the index must be finite and non-negative, the peak finite and positive")
    _, stored_peak = load_twr(conn)
    stamp = _now()
    with conn:
        conn.executemany(
            "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            [(TWR_INDEX, index, stamp), (TWR_PEAK, max(peak, stored_peak), stamp)],
        )


def reset_twr_peak(conn: sqlite3.Connection, by: str, reason: str) -> None:
    """Explicit human reset of the peak to the current index (SPEC 1.3), recorded
    in `events`. Only called by the manual kill-switch reset."""
    if not by.strip():
        raise ValueError("a reset needs the name of the person doing it")
    index, peak = load_twr(conn)
    stamp = _now()
    with conn:
        conn.execute(
            "INSERT INTO risk_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (TWR_PEAK, index, stamp),
        )
    record_event(conn, "PEAK_RESET", f"by {by}: {reason} (peak {peak:.6f} -> {index:.6f})", stamp)


def _none_if_nan(value: object) -> object:
    return None if isinstance(value, float) and math.isnan(value) else value


def _upsert_frame(
    conn: sqlite3.Connection, table: str, keys: tuple[str, ...], frame: pd.DataFrame,
    columns: tuple[str, ...],
) -> int:  # fmt: skip
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"missing {table} columns: {missing}")
    flat = frame.reset_index()
    flat["date"] = pd.to_datetime(flat["date"]).dt.strftime("%Y-%m-%d")
    names = (*keys, *columns)
    rows = [
        tuple(_none_if_nan(v.item() if hasattr(v, "item") else v) for v in row)
        for row in flat[list(names)].itertuples(index=False, name=None)
    ]
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns)
    with conn:
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})"
            f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}",
            rows,
        )
    return len(rows)


def upsert_signals(conn: sqlite3.Connection, signals: pd.DataFrame) -> int:
    """One row per asset and day (SPEC section 10): indicators, rule fired, current
    and target units. `signals` has `date` and `symbol` as columns or index levels."""
    return _upsert_frame(conn, "signals", ("date", "symbol"), signals, SIGNAL_TABLE_COLUMNS)


def upsert_risk_decisions(conn: sqlite3.Connection, decisions: pd.DataFrame) -> int:
    """One row per day (SPEC section 10): L*, effective L, binding limit, drawdown and
    the vetoes applied (a JSON list). Indexed by date."""
    return _upsert_frame(conn, "risk_decisions", ("date",), decisions, RISK_DECISION_COLUMNS)


def read_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    """Every row of `signals` or `risk_decisions`, oldest first."""
    if table not in {"signals", "risk_decisions"}:
        raise ValueError(f"unknown table {table!r}")
    frame = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY date", conn)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame

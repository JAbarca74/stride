"""Build the daily research database of the Monte Carlo universe (design phase 1).

`docs/design/mc_risk_allocation.md`, section 2. The 20 assets are downloaded from
Tiingo (data-only key from `.env`, GET requests only) into `data/mc_daily_research.db`
(git-ignored). `data/stride.db` is opened read-only: it gives the crypto spot series
used as proxies in one sensitivity run, and a cross-check of the daily returns of
the symbols both databases have. If the 30-minute research database of
`mc_data.py` exists, the daily returns are also compared with the ones its Alpaca
bars give, which is an independent source. Reruns are incremental. Usage:

    python scripts/research/mc_daily_data.py [--db PATH] [--source-db PATH]
        [--intraday-db PATH] [--start 2007-01-01] [--end YYYY-MM-DD] [--out DIR]
        [--no-download]

The whole history is downloaded, not copied and topped up: adjusted prices are
rescaled at every dividend, so bars appended to an older copy would not line up.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from stride.data.calendar import trading_days
from stride.data.sources import TiingoSource
from stride.data.store import connect, read_prices, upsert_prices
from stride.data.update import PlanItem, last_completed_session, update_symbol
from stride.forecast.daily import build_daily_panel, eligible, quality_table
from stride.settings import Secrets

REPO_ROOT = Path(__file__).resolve().parents[2]

# docs/design/mc_universe.md, approved 2026-11-11.
UNIVERSE = (
    "QQQ",
    "IWM",
    "XLE",
    "XLV",
    "XLU",
    "EFA",
    "FXI",
    "INDA",
    "EWZ",
    "TLT",
    "GLD",
    "SLV",
    "CPER",
    "USO",
    "UNG",
    "DBA",
    "VNQ",
    "UUP",
    "IBIT",
    "ETHA",
)
# Spot proxies for the crypto ETFs, only for the labelled sensitivity run.
CRYPTO_SPOT = ("BTC-USD", "ETH-USD")
WINDOW_DAYS = 252  # the history window of the design; an asset joins after it
RETURN_TOLERANCE = 1e-3  # a daily return differing by more than this is reported
INTRADAY_TOLERANCE = 1e-2  # the same, against the last 30-minute bar of the day
NEW_YORK = "America/New_York"


def read_only(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without the right to write to it."""
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def download(conn: sqlite3.Connection, start: date, end: date) -> None:
    """Download or top up the 20 assets from Tiingo."""
    key = Secrets(_env_file=REPO_ROOT / ".env").tiingo_key()  # type: ignore[call-arg]
    source = TiingoSource(api_key=key)
    for symbol in UNIVERSE:
        result = update_symbol(conn, source, PlanItem(symbol, "tiingo", start), end)
        note = " (full history)" if result.full_refresh and result.rows else ""
        print(f"{symbol}: +{result.rows} rows{note}")


def copy_spot(conn: sqlite3.Connection, source_db: Path) -> None:
    """Copy the crypto spot series from the main database (read-only)."""
    source = read_only(source_db)
    try:
        for symbol in CRYPTO_SPOT:
            frame = read_prices(source, symbol)
            if frame.empty:
                print(f"{symbol}: not in {source_db.name}")
                continue
            print(f"{symbol}: copied {upsert_prices(conn, frame)} rows from {source_db.name}")
    finally:
        source.close()


def cross_check(conn: sqlite3.Connection, source_db: Path) -> pd.DataFrame:
    """Compare daily log returns with the main database on the dates both have."""
    source = read_only(source_db)
    rows: list[dict[str, object]] = []
    try:
        for symbol in UNIVERSE:
            ours = read_prices(conn, symbol)["close"].astype(float)
            theirs = read_prices(source, symbol)["close"].astype(float)
            common = ours.index.intersection(theirs.index)
            if len(common) < 2:  # noqa: PLR2004 - a return needs two closes
                continue
            gap = (
                (np.log(ours.loc[common]).diff() - np.log(theirs.loc[common]).diff()).abs().dropna()
            )
            rows.append(
                {
                    "symbol": symbol,
                    "common_days": len(common),
                    "max_abs_return_gap": float(gap.max()),
                    "days_above_tolerance": int((gap > RETURN_TOLERANCE).sum()),
                    "worst_day": pd.Timestamp(gap.idxmax()).date().isoformat(),
                }
            )
    finally:
        source.close()
    return pd.DataFrame(rows)


def cross_check_intraday(conn: sqlite3.Connection, intraday_db: Path) -> pd.DataFrame:
    """Compare daily log returns with the ones the Alpaca 30-minute bars give.

    The last regular-session bar of a day is not the closing auction, so a gap of a
    few basis points is expected; a day above `INTRADAY_TOLERANCE` is counted.
    """
    source = read_only(intraday_db)
    rows: list[dict[str, object]] = []
    try:
        for symbol in UNIVERSE:
            bars = pd.read_sql_query(
                "SELECT ts, close FROM intraday_bars WHERE symbol = ? ORDER BY ts",
                source,
                params=[symbol],
            )
            if bars.empty:
                continue
            local = pd.to_datetime(bars["ts"], utc=True).dt.tz_convert(NEW_YORK)
            theirs = bars["close"].astype(float).groupby(local.dt.date).last()
            theirs.index = pd.to_datetime(theirs.index)
            ours = read_prices(conn, symbol)["close"].astype(float)
            common = ours.index.intersection(theirs.index)
            if len(common) < 2:  # noqa: PLR2004 - a return needs two closes
                continue
            gap = (
                (np.log(ours.loc[common]).diff() - np.log(theirs.loc[common]).diff()).abs().dropna()
            )
            rows.append(
                {
                    "symbol": symbol,
                    "common_days": len(common),
                    "median_gap_bps": round(float(gap.median()) * 1e4, 2),
                    "p99_gap_bps": round(float(gap.quantile(0.99)) * 1e4, 1),
                    "days_above_tolerance": int((gap > INTRADAY_TOLERANCE).sum()),
                    "max_abs_return_gap": round(float(gap.max()), 5),
                    "worst_day": pd.Timestamp(gap.idxmax()).date().isoformat(),
                }
            )
    finally:
        source.close()
    return pd.DataFrame(rows)


def membership(bars: dict[str, pd.DataFrame], days: pd.DatetimeIndex) -> pd.DataFrame:
    """How many assets the as-of universe has at the end of each year."""
    panel = build_daily_panel(bars, days)
    rows: list[dict[str, object]] = []
    for year in sorted(set(panel.day.year)):
        row = int(np.flatnonzero(panel.day.year == year)[-1])
        inside = eligible(panel, row, WINDOW_DAYS)
        rows.append(
            {
                "as_of": panel.day[row].date().isoformat(),
                "assets": int(inside.sum()),
                "not_yet": " ".join(
                    s for s, ok in zip(panel.symbols, inside, strict=True) if not ok
                ),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    """Build or top up the daily research database and write the data-quality report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "mc_daily_research.db")
    parser.add_argument("--source-db", type=Path, default=REPO_ROOT / "data" / "stride.db")
    parser.add_argument("--intraday-db", type=Path, default=REPO_ROOT / "data" / "mc_research.db")
    parser.add_argument("--start", default="2007-01-01")
    parser.add_argument("--end", default=None, help="last session wanted (default: the latest)")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "mc_daily")
    parser.add_argument("--no-download", action="store_true", help="only rebuild the report")
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else last_completed_session(datetime.now(UTC))
    conn = connect(args.db)
    if not args.no_download:
        download(conn, start, end)
        if args.source_db.exists():
            copy_spot(conn, args.source_db)
    days = trading_days(start, end)
    bars = {symbol: read_prices(conn, symbol) for symbol in (*UNIVERSE, *CRYPTO_SPOT)}
    quality = quality_table(bars, days)
    members = membership({symbol: bars[symbol] for symbol in UNIVERSE}, days)
    args.out.mkdir(parents=True, exist_ok=True)
    quality.to_csv(args.out / "data_quality.csv", index=False)
    members.to_csv(args.out / "universe_as_of.csv", index=False)
    print(quality.to_string(index=False))
    print(members.to_string(index=False))
    if args.source_db.exists():
        check = cross_check(conn, args.source_db)
        check.to_csv(args.out / "cross_check.csv", index=False)
        print(check.to_string(index=False))
    if args.intraday_db.exists():
        intraday = cross_check_intraday(conn, args.intraday_db)
        intraday.to_csv(args.out / "cross_check_intraday.csv", index=False)
        print(intraday.to_string(index=False))
    conn.close()


if __name__ == "__main__":
    main()

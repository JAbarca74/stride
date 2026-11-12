"""Download the 30-minute history of the Monte Carlo universe into a research database.

Read-only on Alpaca (paper keys from the environment or `.env`, GET requests only).
The database is `data/mc_research.db` by default (git-ignored). Reruns are
incremental. Usage:

    python scripts/research/mc_data.py [--db PATH] [--start 2016-01-01]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from stride.data.intraday import update_intraday
from stride.data.sources import AlpacaSource

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


def load_env(path: Path) -> None:
    """Put the Alpaca keys of `.env` in the environment without printing them."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().startswith("ALPACA_") and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip("'\"")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "mc_research.db")
    parser.add_argument("--start", default="2016-01-01")
    args = parser.parse_args()
    load_env(REPO_ROOT / ".env")
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    now = datetime.now(UTC)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    source = AlpacaSource()
    for symbol in UNIVERSE:
        result = update_intraday(conn, source, symbol, start, now)
        first, last, count = conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM intraday_bars WHERE symbol = ?", (symbol,)
        ).fetchone()
        note = " (history re-adjusted, replaced)" if result.refetched else ""
        print(f"{symbol}: +{result.rows} rows{note}; {count} stored, {first} to {last}")
    conn.close()


if __name__ == "__main__":
    main()

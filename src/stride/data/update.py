"""Incremental price download and the Phase 1 data checks (SPEC sections 5 and 12).

Usage, from the repository root (keys come from `.env`, never from the command line):

    python -m stride.data.update prices        # incremental download into SQLite
    python -m stride.data.update validate      # section 5 validations on the universe
    python -m stride.data.update check-alpaca  # Alpaca free-plan closes vs Tiingo
    python -m stride.data.update t21           # synthetic vs real leveraged ETFs

Downloads are incremental: only bars after the last stored date are requested, plus
an overlap window. If a provider has revised any overlapping adjusted close (a new
dividend or split), the symbol's full history is downloaded again, because adjusted
prices change all the way back. HTTP 429 and transient errors are retried with
exponential backoff by the sources themselves.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from stride.config import StrideConfig, load_config
from stride.data.calendar import is_trading_day, previous_trading_day, trading_days
from stride.data.leveraged import (
    Calibration,
    EtfFacts,
    calibrate_spread,
    expense_ratio_for,
    load_etf_facts,
    rate_known_at_t_minus_1,
)
from stride.data.quality import CheckResult, validate
from stride.data.sources import (
    CRYPTO_SPOT_PRODUCTS,
    AlpacaSource,
    CoinbaseSource,
    FredRateSource,
    PriceSource,
    SourceError,
    TiingoSource,
    UnadjustedCloseSource,
    splice_spot_with_etf,
)
from stride.data.store import connect, last_price_date, read_prices, upsert_prices
from stride.settings import CredentialError, load_secrets

OVERLAP_DAYS = 10  # calendar days re-downloaded to detect revised adjusted prices
REVISION_TOLERANCE = 1e-6  # relative change in an overlapping close that means a revision
RUN_HOUR_NEW_YORK = 18  # SPEC section 8: the daily run starts at 18:00 New York time
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PlanItem:
    symbol: str
    source: str  # "tiingo" or "coinbase"
    start: date


@dataclass(frozen=True)
class UpdateResult:
    symbol: str
    rows: int = 0
    first: date | None = None
    last: date | None = None
    full_refresh: bool = False
    error: str | None = None


def download_plan(config: StrideConfig) -> list[PlanItem]:
    """Every series Phase 1 needs: the universe and the leveraged ETFs from Tiingo,
    and the crypto spot history from Coinbase."""
    items = [PlanItem(symbol, "tiingo", config.data.history_start) for symbol in config.universe]
    for versions in config.leveraged_etfs.values():
        items += [PlanItem(etf, "tiingo", config.data.history_start) for etf in versions.values()]
    items += [
        PlanItem(product, "coinbase", config.data.crypto_history_start)
        for etf, product in CRYPTO_SPOT_PRODUCTS.items()
        if etf in config.universe
    ]
    return items


def _revised(stored: pd.DataFrame, fresh: pd.DataFrame) -> bool:
    common = stored.index.intersection(fresh.index)
    if len(common) == 0:
        return False
    old = stored.loc[common, "close"].astype(float)
    new = fresh.loc[common, "close"].astype(float)
    return bool(((new - old).abs() / old).gt(REVISION_TOLERANCE).any())


def update_symbol(
    conn: sqlite3.Connection, source: PriceSource, item: PlanItem, end: date
) -> UpdateResult:
    """Download only what is missing for one symbol; refresh it all if revised."""
    last = last_price_date(conn, item.symbol)
    full = last is None
    if last is None:
        frame = source.fetch(item.symbol, item.start, end)
    else:
        start = max(item.start, last - timedelta(days=OVERLAP_DAYS))
        if start > end:
            return UpdateResult(item.symbol)
        frame = source.fetch(item.symbol, start, end)
        if _revised(read_prices(conn, item.symbol, start, last), frame):
            frame = source.fetch(item.symbol, item.start, end)
            full = True
    if frame.empty:
        return UpdateResult(item.symbol, full_refresh=full)
    rows = upsert_prices(conn, frame)
    index = pd.DatetimeIndex(frame.index)
    return UpdateResult(item.symbol, rows, index.min().date(), index.max().date(), full)


def update_all(
    conn: sqlite3.Connection,
    plan: Sequence[PlanItem],
    sources: Mapping[str, PriceSource],
    end: date,
) -> list[UpdateResult]:
    """Update every plan item; one failing symbol does not stop the others."""
    results = []
    for item in plan:
        try:
            results.append(update_symbol(conn, sources[item.source], item, end))
        except (SourceError, ValueError, sqlite3.IntegrityError) as exc:
            results.append(UpdateResult(item.symbol, error=f"{type(exc).__name__}: {exc}"))
    return results


def series_for(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    """Stored bars for a universe symbol; crypto ETFs are spliced with spot history."""
    etf = read_prices(conn, symbol)
    product = CRYPTO_SPOT_PRODUCTS.get(symbol)
    if product is None or etf.empty:
        return etf
    spot = read_prices(conn, product)
    if spot.empty:
        return etf
    first = pd.DatetimeIndex(spot.index).min().date()
    last = pd.DatetimeIndex(etf.index).max().date()
    return splice_spot_with_etf(spot, etf, symbol, trading_days(first, last))


def validate_universe(
    conn: sqlite3.Connection, config: StrideConfig, evaluation_date: date
) -> list[CheckResult]:
    """Section 5 validations 1-3 on every universe symbol (spliced for crypto)."""
    results: list[CheckResult] = []
    for symbol in config.universe:
        results += validate(symbol, series_for(conn, symbol), evaluation_date, config.data.quality)
    return results


@dataclass(frozen=True)
class CloseComparison:
    symbol: str
    days: int
    max_diff: float
    over_tolerance: int


def compare_unadjusted_closes(
    primary: UnadjustedCloseSource,
    secondary: UnadjustedCloseSource,
    symbols: Sequence[str],
    *,
    start: date,
    end: date,
    tolerance: float,
) -> list[CloseComparison]:
    """How far the secondary source's raw closes are from the primary's."""
    rows = []
    for symbol in symbols:
        a = primary.fetch_unadjusted_close(symbol, start, end)
        b = secondary.fetch_unadjusted_close(symbol, start, end)
        common = a.index.intersection(b.index)
        diff = ((b[common] - a[common]).abs() / a[common]) if len(common) else pd.Series()
        rows.append(
            CloseComparison(
                symbol,
                len(common),
                float(diff.max()) if len(diff) else float("nan"),
                int((diff > tolerance).sum()),
            )
        )
    return rows


@dataclass(frozen=True)
class TrackingResult:
    etf: str
    underlying: str
    multiple: int
    fee: float
    calibration: Calibration | None
    error: str | None = None


def t21_tracking(
    conn: sqlite3.Connection,
    config: StrideConfig,
    facts: Mapping[str, EtfFacts],
    rate: pd.Series,
) -> list[TrackingResult]:
    """Calibrate each leveraged ETF against its underlying (SPEC section 9)."""
    results = []
    for underlying, versions in config.leveraged_etfs.items():
        base = read_prices(conn, underlying)["close"]
        for multiple, etf in versions.items():
            fee = expense_ratio_for(etf, facts, config.costs.leveraged_etf_fee)
            real = read_prices(conn, etf)["close"]
            if base.empty or len(real) < 2:  # noqa: PLR2004
                results.append(
                    TrackingResult(etf, underlying, multiple, fee, None, "no stored prices")
                )
                continue
            aligned = rate_known_at_t_minus_1(rate, pd.DatetimeIndex(base.index))
            calibration = calibrate_spread(base, real, multiple, aligned, fee)
            results.append(TrackingResult(etf, underlying, multiple, fee, calibration))
    return results


def last_completed_session(now: datetime) -> date:
    """The latest NYSE day whose bar should be available at `now` (run at 18:00 ET)."""
    local = now.astimezone(NEW_YORK)
    today = local.date()
    if is_trading_day(today) and local.hour >= RUN_HOUR_NEW_YORK:
        return today
    return previous_trading_day(today)


def _print(line: str) -> None:
    sys.stdout.write(line + "\n")


def main(argv: Sequence[str] | None = None, now: Callable[[], datetime] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stride.data.update")
    parser.add_argument("command", choices=["prices", "validate", "check-alpaca", "t21"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--date", type=date.fromisoformat, help="evaluation day (default: last)")
    parser.add_argument("--days", type=int, default=30, help="check-alpaca window (days)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    clock = now or (lambda: datetime.now(NEW_YORK))
    day = args.date or last_completed_session(clock())
    conn = connect(Path(args.config).resolve().parent / config.data.db_path)
    try:
        return _run(args.command, args, config, conn, day)
    except CredentialError as exc:
        _print(f"error: {exc}")
        return 2
    finally:
        conn.close()


def _run(
    command: str,
    args: argparse.Namespace,
    config: StrideConfig,
    conn: sqlite3.Connection,
    day: date,
) -> int:
    secrets = load_secrets(args.env_file)
    if command == "prices":
        sources: dict[str, PriceSource] = {
            "tiingo": TiingoSource(api_key=secrets.tiingo_key()),
            "coinbase": CoinbaseSource(),
        }
        results = update_all(conn, download_plan(config), sources, day)
        for r in results:
            status = r.error or f"{r.rows} rows {r.first}..{r.last}" + (
                " (full refresh)" if r.full_refresh else ""
            )
            _print(f"{r.symbol:6} {status}")
        return 1 if any(r.error for r in results) else 0

    if command == "validate":
        checks = validate_universe(conn, config, day)
        for check in checks:
            mark = "ok  " if check.passed else "FAIL"
            _print(f"{mark} {check.symbol:5} {check.name:22} {check.severity:6} {check.detail}")
        return 1 if any(not check.passed for check in checks) else 0

    if command == "check-alpaca":
        key, secret = secrets.alpaca_paper_keys()
        symbols = list(config.universe)
        rows = compare_unadjusted_closes(
            TiingoSource(api_key=secrets.tiingo_key()),
            AlpacaSource(api_key=key, secret_key=secret),
            symbols,
            start=day - timedelta(days=args.days),
            end=day,
            tolerance=config.data.quality.source_close_tolerance,
        )
        for c in rows:
            _print(
                f"{c.symbol:6} days={c.days:3} max_diff={c.max_diff:.4%} over={c.over_tolerance}"
            )
        return 1 if any(c.over_tolerance or c.days == 0 for c in rows) else 0

    rate = FredRateSource().fetch_rate()
    tracking = t21_tracking(conn, config, load_etf_facts(), rate)
    for t in tracking:
        if t.calibration is None:
            _print(f"{t.etf:5} {t.multiple}x {t.underlying:5} {t.error}")
            continue
        cal = t.calibration
        verdict = "pass" if cal.passes else "FAIL"
        _print(
            f"{t.etf:5} {t.multiple}x {t.underlying:5} fee={t.fee:.2%} spread={cal.spread:.2%}"
            f" diff={cal.tracking_diff_per_year:.2%}/yr {verdict}"
        )
    return 1 if any(t.calibration is None or not t.calibration.passes for t in tracking) else 0


if __name__ == "__main__":
    raise SystemExit(main())

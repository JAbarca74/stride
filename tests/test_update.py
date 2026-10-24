from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from importlib.metadata import entry_points
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stride.config import load_config
from stride.data.calendar import trading_days
from stride.data.events import PriceEvent
from stride.data.leveraged import EtfFacts, synthetic_prices, synthetic_returns
from stride.data.sources import SourceError, to_contract
from stride.data.store import connect, read_prices, upsert_prices
from stride.data.update import (
    OVERLAP_DAYS,
    PlanItem,
    compare_unadjusted_closes,
    download_plan,
    last_completed_session,
    main,
    series_for,
    t21_tracking,
    update_all,
    update_symbol,
    validate_universe,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
NOW = datetime(2026, 10, 19, 22, 0, tzinfo=UTC)
DAYS = trading_days(date(2025, 6, 2), date(2026, 10, 16))


def _bars(
    symbol: str, days: pd.DatetimeIndex, start: float = 100.0, source: str = "fake"
) -> pd.DataFrame:
    close = start * np.cumprod(np.full(len(days), 1.001))
    frame = pd.DataFrame(
        {
            "date": days,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(len(days), 1000),
        }
    )
    return to_contract(frame, symbol, source, NOW)


class FakeSource:
    """Serves a fixed history per symbol and records requested ranges."""

    name = "fake"

    def __init__(self, history: dict[str, pd.DataFrame]) -> None:
        self.history = history
        self.calls: list[tuple[str, date, date]] = []

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self.calls.append((symbol, start, end))
        if symbol not in self.history:
            raise SourceError(f"fake has no {symbol}")
        frame = self.history[symbol]
        index = pd.DatetimeIndex(frame.index)
        return frame[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    yield connection
    connection.close()


ITEM = PlanItem("SPY", "fake", date(2025, 6, 2))


def test_plan_covers_universe_leveraged_etfs_and_crypto_spot() -> None:
    plan = download_plan(CONFIG)
    symbols = {item.symbol for item in plan}
    assert set(CONFIG.universe) <= symbols
    assert {"SSO", "UPRO", "UBT", "TMF", "UGL", "AGQ", "BITX", "ETHU"} <= symbols
    assert set(CONFIG.experiments.extra_universe) <= symbols  # data for the experiment
    assert set(CONFIG.experiments.search_universe) | {"QLD", "TQQQ"} <= symbols  # search
    spot = {item.symbol: item for item in plan if item.source == "coinbase"}
    assert set(spot) == {"BTC-USD", "ETH-USD"}
    assert spot["BTC-USD"].start == CONFIG.data.crypto_history_start


def test_first_update_downloads_full_history(conn: sqlite3.Connection) -> None:
    source = FakeSource({"SPY": _bars("SPY", DAYS)})
    result = update_symbol(conn, source, ITEM, DAYS[-1].date())
    assert result.rows == len(DAYS)
    assert result.full_refresh
    assert source.calls == [("SPY", ITEM.start, DAYS[-1].date())]


def test_next_update_requests_only_new_bars_plus_overlap(conn: sqlite3.Connection) -> None:
    full = _bars("SPY", DAYS)
    upsert_prices(conn, full.iloc[:-5])
    source = FakeSource({"SPY": full})
    result = update_symbol(conn, source, ITEM, DAYS[-1].date())
    last_stored = DAYS[-6].date()
    assert source.calls == [("SPY", last_stored - pd.Timedelta(days=OVERLAP_DAYS), DAYS[-1].date())]
    assert not result.full_refresh
    assert len(read_prices(conn, "SPY")) == len(DAYS)


def test_revised_history_triggers_a_full_refresh(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, _bars("SPY", DAYS[:-5]))
    revised = _bars("SPY", DAYS, start=99.0)  # e.g. a new dividend adjusted every past close
    source = FakeSource({"SPY": revised})
    result = update_symbol(conn, source, ITEM, DAYS[-1].date())
    assert result.full_refresh
    assert len(source.calls) == 2
    assert source.calls[1][1] == ITEM.start
    assert read_prices(conn, "SPY")["close"].iloc[0] == pytest.approx(99.0 * 1.001)


def test_up_to_date_symbol_makes_no_request_beyond_overlap(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, _bars("SPY", DAYS))
    source = FakeSource({"SPY": _bars("SPY", DAYS)})
    result = update_symbol(conn, source, ITEM, DAYS[-1].date())
    assert not result.full_refresh
    assert len(source.calls) == 1


def test_one_failing_symbol_does_not_stop_the_others(conn: sqlite3.Connection) -> None:
    source = FakeSource({"SPY": _bars("SPY", DAYS)})
    plan = [PlanItem("XXX", "fake", ITEM.start), ITEM]
    results = update_all(conn, plan, {"fake": source}, DAYS[-1].date())
    assert results[0].error is not None and "XXX" in results[0].error
    assert results[1].rows == len(DAYS)


def test_crypto_series_is_spliced_from_stored_spot_and_etf(conn: sqlite3.Connection) -> None:
    switch = DAYS[-100]
    upsert_prices(conn, _bars("BTC-USD", DAYS, start=60_000.0, source="coinbase"))
    upsert_prices(conn, _bars("IBIT", DAYS[switch <= DAYS], start=40.0, source="tiingo"))
    series = series_for(conn, "IBIT")
    assert list(series.index) == list(DAYS)
    assert set(series["source"]) == {"coinbase", "tiingo"}
    assert (series["symbol"] == "IBIT").all()


def test_non_crypto_series_is_stored_as_is(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, _bars("SPY", DAYS))
    assert len(series_for(conn, "SPY")) == len(DAYS)


def test_validate_universe_runs_every_symbol(conn: sqlite3.Connection) -> None:
    upsert_prices(conn, _bars("SPY", DAYS))
    results = validate_universe(conn, CONFIG, DAYS[-1].date())
    spy = [r for r in results if r.symbol == "SPY"]
    assert all(r.passed for r in spy)
    assert {r.symbol for r in results} == set(CONFIG.universe)


def test_flagged_event_lets_a_large_move_pass(conn: sqlite3.Connection) -> None:
    bars = _bars("SLV", DAYS)
    crash = DAYS[-30]
    bars.loc[bars.index >= crash, ["open", "high", "low", "close"]] *= 0.7
    upsert_prices(conn, bars)
    event = PriceEvent(date=crash.date(), kind="market_move", detail="test", confirmed_by="t")

    def jump_check(events: dict[str, list[PriceEvent]] | None) -> bool:
        results = validate_universe(conn, CONFIG, DAYS[-1].date(), events)
        return next(r.passed for r in results if r.symbol == "SLV" and r.name == "MAX_DAILY_CHANGE")

    assert not jump_check(None)
    assert jump_check({"SLV": [event]})
    assert not jump_check({"SPY": [event]})  # a flag is per symbol


class RawCloses:
    def __init__(self, closes: pd.Series) -> None:
        self.name = "raw"
        self.closes = closes

    def fetch_unadjusted_close(self, symbol: str, start: date, end: date) -> pd.Series:
        return self.closes


def test_compare_unadjusted_closes_counts_mismatches() -> None:
    a = pd.Series([100.0, 101.0, 102.0], index=DAYS[:3])
    b = pd.Series([100.0, 101.0, 103.0], index=DAYS[:3])
    [row] = compare_unadjusted_closes(
        RawCloses(a),
        RawCloses(b),
        ["SPY"],
        start=DAYS[0].date(),
        end=DAYS[2].date(),
        tolerance=0.005,
    )
    assert row.days == 3
    assert row.over_tolerance == 1
    assert row.max_diff == pytest.approx(1 / 102)


def test_t21_tracking_calibrates_stored_series(conn: sqlite3.Connection) -> None:
    rng = np.random.default_rng(3)
    underlying = pd.Series(400 * np.cumprod(1 + rng.normal(0.0004, 0.01, len(DAYS))), index=DAYS)
    rate = pd.Series(0.04, index=DAYS)
    returns = synthetic_returns(underlying, 2, rate, spread=0.01, annual_fee=0.0084)
    sso = synthetic_prices(returns, DAYS[0], DAYS[0], 50.0)[DAYS[150] :]

    def stored(symbol: str, close: pd.Series) -> pd.DataFrame:
        frame = pd.DataFrame(
            {
                "date": close.index,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1,
            }
        )
        return to_contract(frame, symbol, "fake", NOW)

    upsert_prices(conn, stored("SPY", underlying))
    upsert_prices(conn, stored("SSO", sso))
    facts: dict[str, EtfFacts] = {}
    results = {r.etf: r for r in t21_tracking(conn, CONFIG, facts, rate)}
    sso_result = results["SSO"]
    assert sso_result.calibration is not None
    assert sso_result.fee == CONFIG.costs.leveraged_etf_fee  # fallback: no facts given
    assert sso_result.calibration.passes
    assert results["UPRO"].error == "no stored prices"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 16, 22, 30, tzinfo=UTC), date(2026, 10, 16)),  # Fri 18:30 ET
        (datetime(2026, 10, 16, 20, 0, tzinfo=UTC), date(2026, 10, 15)),  # Fri 16:00 ET
        (datetime(2026, 10, 18, 15, 0, tzinfo=UTC), date(2026, 10, 16)),  # Sunday
        (datetime(2026, 11, 26, 23, 0, tzinfo=UTC), date(2026, 11, 25)),  # Thanksgiving
    ],
)
def test_last_completed_session(now: datetime, expected: date) -> None:
    assert last_completed_session(now) == expected


def test_cli_without_keys_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("DATA_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "config.yaml"
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    config.write_text(text, encoding="utf-8")
    code = main(
        ["prices", "--config", str(config), "--env-file", str(tmp_path / "none.env")],
        now=lambda: NOW,
    )
    assert code == 2
    assert "DATA_API_KEY is not set" in capsys.readouterr().out
    assert (tmp_path / "data" / "stride.db").exists()


def test_stride_data_entrypoint_resolves_to_main() -> None:
    eps = [ep for ep in entry_points(group="console_scripts") if ep.name == "stride-data"]
    assert len(eps) == 1
    assert eps[0].load() == main

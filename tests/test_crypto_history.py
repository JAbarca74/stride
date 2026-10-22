from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest

from stride.data.sources import (
    CONTRACT_COLUMNS,
    CRYPTO_SPOT_PRODUCTS,
    CoinbaseSource,
    SourceError,
    splice_spot_with_etf,
    to_contract,
)

NOW = datetime(2026, 10, 19, 20, 30, tzinfo=UTC)
TRADING_DAYS = pd.bdate_range("2024-01-01", "2024-01-31")
SWITCH = pd.Timestamp("2024-01-11")


STEP = pd.Timedelta(minutes=15)


def _candles_for(day: str, *, spike_outside: bool = True) -> dict[pd.Timestamp, list[float]]:
    """15-minute candles for a whole UTC day; price = minutes since 00:00 UTC.

    With `spike_outside`, extreme candles sit just before the open and just after
    the close so a correct session filter must ignore them.
    """
    start = pd.Timestamp(day, tz="UTC")
    candles: dict[pd.Timestamp, list[float]] = {}
    for i in range(96):
        ts = start + i * STEP
        price = 100.0 + i
        # Layout: [time, low, high, open, close, volume]
        candles[ts] = [ts.timestamp(), price - 0.5, price + 0.5, price, price + 0.25, 1.0]
    if spike_outside:
        for ts in candles:
            if ts.hour in (12, 22):
                candles[ts] = [ts.timestamp(), 1.0, 10_000.0, 500.0, 500.0, 99.0]
    return candles


class CandleSession:
    """Serves Coinbase-style 15-minute candles (newest first) for the requested window."""

    def __init__(self, candles: dict[pd.Timestamp, list[float]]) -> None:
        self.candles = candles
        self.calls: list[Mapping[str, str]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> Any:
        self.calls.append(params)
        start = pd.Timestamp(params["start"])
        end = pd.Timestamp(params["end"])
        rows = [row for ts, row in sorted(self.candles.items(), reverse=True) if start <= ts <= end]

        class Response:
            status_code = 200

            def json(self) -> Any:
                return rows

        return Response()


def _source(session: CandleSession, sleeps: list[float] | None = None) -> CoinbaseSource:
    log = sleeps if sleeps is not None else []
    return CoinbaseSource(session=session, clock=lambda: NOW, sleep=log.append)


def _price_at(ts: str) -> float:
    stamp = pd.Timestamp(ts, tz="UTC")
    return 100.0 + (stamp.hour * 60 + stamp.minute) // 15


def test_summer_session_bar_runs_from_930_to_1600_new_york() -> None:
    # 2024-07-01, daylight saving: session is 13:30-20:00 UTC.
    session = CandleSession(_candles_for("2024-07-01"))
    frame = _source(session).fetch("BTC-USD", date(2024, 7, 1), date(2024, 7, 1))
    assert tuple(frame.columns) == CONTRACT_COLUMNS
    bar = frame.iloc[0]
    assert frame.index[0] == pd.Timestamp("2024-07-01")
    assert bar["open"] == _price_at("2024-07-01 13:30")
    assert bar["close"] == _price_at("2024-07-01 19:45") + 0.25  # candle ending 16:00 ET
    assert bar["high"] == _price_at("2024-07-01 19:45") + 0.5
    assert bar["low"] == _price_at("2024-07-01 13:30") - 0.5
    assert bar["volume"] == 26  # 26 quarter-hours in 6.5 hours
    assert bar["source"] == "coinbase"


def test_winter_session_bar_shifts_with_daylight_saving() -> None:
    # 2024-01-10, standard time: session is 14:30-21:00 UTC.
    frame = _source(CandleSession(_candles_for("2024-01-10"))).fetch(
        "BTC-USD", date(2024, 1, 10), date(2024, 1, 10)
    )
    assert frame["open"].iloc[0] == _price_at("2024-01-10 14:30")
    assert frame["close"].iloc[0] == _price_at("2024-01-10 20:45") + 0.25


def test_early_close_day_ends_at_1300_new_york() -> None:
    # Black Friday 2024-11-29 closes at 13:00 ET = 18:00 UTC.
    frame = _source(CandleSession(_candles_for("2024-11-29"))).fetch(
        "BTC-USD", date(2024, 11, 29), date(2024, 11, 29)
    )
    assert frame["close"].iloc[0] == _price_at("2024-11-29 17:45") + 0.25
    assert frame["volume"].iloc[0] == 14


def test_weekends_and_holidays_have_no_bar() -> None:
    candles: dict[pd.Timestamp, list[float]] = {}
    for day in pd.date_range("2024-11-27", "2024-12-02"):
        candles.update(_candles_for(day.date().isoformat(), spike_outside=False))
    frame = _source(CandleSession(candles)).fetch("BTC-USD", date(2024, 11, 27), date(2024, 12, 2))
    assert [d.date() for d in frame.index] == [
        date(2024, 11, 27),
        date(2024, 11, 29),
        date(2024, 12, 2),
    ]


def test_session_without_candles_is_missing() -> None:
    candles = _candles_for("2024-01-10")
    frame = _source(CandleSession(candles)).fetch("BTC-USD", date(2024, 1, 10), date(2024, 1, 11))
    assert [d.date() for d in frame.index] == [date(2024, 1, 10)]


def test_requests_stay_within_300_candles_and_are_throttled() -> None:
    candles: dict[pd.Timestamp, list[float]] = {}
    for day in pd.date_range("2024-01-01", "2024-01-31"):
        candles.update(_candles_for(day.date().isoformat(), spike_outside=False))
    session = CandleSession(candles)
    sleeps: list[float] = []
    frame = _source(session, sleeps).fetch("BTC-USD", date(2024, 1, 1), date(2024, 1, 31))
    assert len(frame) == 21  # NYSE trading days in January 2024
    for params in session.calls:
        span = pd.Timestamp(params["end"]) - pd.Timestamp(params["start"])
        assert span / STEP + 1 <= 300
        assert params["granularity"] == "900"
    assert len(sleeps) == len(session.calls) - 1


def test_empty_range_returns_empty_contract() -> None:
    frame = _source(CandleSession({})).fetch("BTC-USD", date(2024, 1, 6), date(2024, 1, 7))
    assert frame.empty
    assert tuple(frame.columns) == CONTRACT_COLUMNS


def test_spot_products_cover_both_crypto_etfs() -> None:
    assert CRYPTO_SPOT_PRODUCTS == {"IBIT": "BTC-USD", "ETHA": "ETH-USD"}


def _spot() -> pd.DataFrame:
    days = pd.date_range("2024-01-01", "2024-01-31")  # 7 days a week
    close = 40_000.0 * np.cumprod(np.full(len(days), 1.01))
    bars = pd.DataFrame(
        {
            "date": days,
            "open": close,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": np.full(len(days), 1_000),
        }
    )
    return to_contract(bars, "BTC-USD", "coinbase", NOW)


def _etf(spot: pd.DataFrame) -> pd.DataFrame:
    days = TRADING_DAYS[TRADING_DAYS >= SWITCH]
    close = spot["close"].reindex(days).to_numpy() / 1_600.0
    bars = pd.DataFrame(
        {
            "date": days,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(len(days), 5_000_000),
        }
    )
    return to_contract(bars, "IBIT", "tiingo", NOW)


def test_splice_is_continuous_with_no_gaps() -> None:
    spot = _spot()
    spliced = splice_spot_with_etf(spot, _etf(spot), "IBIT", TRADING_DAYS)
    assert list(spliced.index) == list(TRADING_DAYS)
    assert tuple(spliced.columns) == CONTRACT_COLUMNS
    assert (spliced["symbol"] == "IBIT").all()


def test_source_marks_the_spot_to_etf_switch() -> None:
    spot = _spot()
    spliced = splice_spot_with_etf(spot, _etf(spot), "IBIT", TRADING_DAYS)
    assert (spliced.loc[spliced.index < SWITCH, "source"] == "coinbase").all()
    assert (spliced.loc[spliced.index >= SWITCH, "source"] == "tiingo").all()


def test_splice_keeps_spot_returns_and_has_no_jump_at_switch() -> None:
    spot = _spot()
    spliced = splice_spot_with_etf(spot, _etf(spot), "IBIT", TRADING_DAYS)
    spliced_returns = spliced["close"].pct_change().dropna()
    spot_returns = spot["close"].reindex(TRADING_DAYS).pct_change().dropna()
    np.testing.assert_allclose(spliced_returns.to_numpy(), spot_returns.to_numpy())


def test_splice_drops_weekend_spot_bars() -> None:
    spot = _spot()
    spliced = splice_spot_with_etf(spot, _etf(spot), "IBIT", TRADING_DAYS)
    assert not pd.DatetimeIndex(spliced.index).dayofweek.isin([5, 6]).any()


def test_splice_converts_spot_volume_to_etf_shares() -> None:
    spot = _spot()
    spliced = splice_spot_with_etf(spot, _etf(spot), "IBIT", TRADING_DAYS)
    assert spliced["volume"].iloc[0] == 1_000 * 1_600


def test_splice_rejects_gap_in_spot_history() -> None:
    spot = _spot().drop(index=[pd.Timestamp("2024-01-05")])
    with pytest.raises(SourceError, match="1 missing trading days after splice: 2024-01-05"):
        splice_spot_with_etf(spot, _etf(_spot()), "IBIT", TRADING_DAYS)


def test_splice_requires_spot_bar_on_switch_day() -> None:
    spot = _spot()
    etf = _etf(spot)
    with pytest.raises(SourceError, match="no bar on the switch day 2024-01-11"):
        splice_spot_with_etf(spot.drop(index=[SWITCH]), etf, "IBIT", TRADING_DAYS)


def test_splice_rejects_empty_etf() -> None:
    spot = _spot()
    with pytest.raises(SourceError, match="ETF series is empty"):
        splice_spot_with_etf(spot, _etf(spot).iloc[:0], "IBIT", TRADING_DAYS)

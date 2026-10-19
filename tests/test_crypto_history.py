from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
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


def _epoch(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), UTC).timestamp())


class CandleSession:
    """Serves Coinbase-style candles (newest first) for any requested window."""

    def __init__(self, prices: Mapping[date, float]) -> None:
        self.prices = prices
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
        start = datetime.fromisoformat(params["start"]).date()
        end = datetime.fromisoformat(params["end"]).date()
        candles = [
            [_epoch(day), price * 0.99, price * 1.01, price, price, 10.0]
            for day, price in sorted(self.prices.items(), reverse=True)
            if start <= day <= end
        ]

        class Response:
            status_code = 200

            def json(self) -> Any:
                return candles

        return Response()


def _daily_prices(start: date, days: int) -> dict[date, float]:
    return {start + timedelta(days=i): 100.0 + i for i in range(days)}


def test_coinbase_returns_contract_columns_for_every_calendar_day() -> None:
    session = CandleSession(_daily_prices(date(2024, 1, 1), 10))
    frame = CoinbaseSource(session=session, clock=lambda: NOW).fetch(
        "BTC-USD", date(2024, 1, 1), date(2024, 1, 10)
    )
    assert tuple(frame.columns) == CONTRACT_COLUMNS
    assert len(frame) == 10
    assert frame.index[0] == pd.Timestamp("2024-01-01")
    assert frame.index.is_monotonic_increasing
    assert (frame["source"] == "coinbase").all()
    assert frame["close"].iloc[0] == 100.0
    assert frame["open"].iloc[0] == 100.0
    assert frame["low"].iloc[0] == pytest.approx(99.0)


def test_coinbase_paginates_in_windows_of_300_candles() -> None:
    session = CandleSession(_daily_prices(date(2023, 1, 1), 700))
    frame = CoinbaseSource(session=session, clock=lambda: NOW).fetch(
        "BTC-USD", date(2023, 1, 1), date(2024, 11, 30)
    )
    assert len(session.calls) == 3
    assert len(frame) == 700
    assert not frame.index.duplicated().any()


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

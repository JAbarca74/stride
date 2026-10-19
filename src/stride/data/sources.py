"""Daily price download per provider (SPEC section 5).

Every source returns a DataFrame indexed by `date` (timezone-naive) with the
section 5 contract columns: symbol, open, high, low, close, volume, source,
fetched_at. Prices are adjusted for dividends and splits.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Protocol

import pandas as pd
import requests

CONTRACT_COLUMNS = ("symbol", "open", "high", "low", "close", "volume", "source", "fetched_at")
DATA_API_KEY_ENV = "DATA_API_KEY"
REQUEST_TIMEOUT_SECONDS = 30

Clock = Callable[[], datetime]


class SourceError(Exception):
    """A provider could not deliver prices."""


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse: ...


class PriceSource(Protocol):
    name: str

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Daily bars for `symbol` from `start` to `end`, both inclusive."""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def to_contract(bars: pd.DataFrame, symbol: str, source: str, fetched_at: datetime) -> pd.DataFrame:
    """Shape provider bars (columns date, open, high, low, close, volume) into the contract."""
    if bars.empty:
        empty = pd.DataFrame(columns=list(CONTRACT_COLUMNS))
        empty.index = pd.DatetimeIndex([], name="date")
        return empty
    frame = bars.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize(None).dt.normalize()
    frame = frame.drop_duplicates("date", keep="last").set_index("date").sort_index()
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column].astype(float)
    frame["volume"] = frame["volume"].fillna(0).round().astype("int64")
    frame["symbol"] = symbol
    frame["source"] = source
    frame["fetched_at"] = pd.Timestamp(fetched_at).tz_convert("UTC")
    return frame[list(CONTRACT_COLUMNS)]


class TiingoSource:
    """Tiingo end-of-day prices (free plan), using the split/dividend-adjusted fields."""

    name = "tiingo"
    url = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"

    def __init__(
        self,
        api_key: str | None = None,
        session: HttpSession | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        env = os.environ if env is None else env
        key = api_key if api_key is not None else env.get(DATA_API_KEY_ENV, "")
        if not key:
            raise SourceError(f"{DATA_API_KEY_ENV} is not set")
        self._key = key
        self._session: HttpSession = session or requests.Session()
        self._clock = clock

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        # The key travels in a header so it never appears in URLs, logs or errors.
        try:
            response = self._session.get(
                self.url.format(symbol=symbol.lower()),
                params={"startDate": start.isoformat(), "endDate": end.isoformat()},
                headers={"Authorization": f"Token {self._key}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise SourceError(f"tiingo request for {symbol} failed: {type(exc).__name__}") from None
        if response.status_code != 200:  # noqa: PLR2004
            raise SourceError(f"tiingo returned HTTP {response.status_code} for {symbol}")
        payload: list[dict[str, Any]] = response.json()
        bars = pd.DataFrame(
            {
                "date": [row["date"] for row in payload],
                "open": [row["adjOpen"] for row in payload],
                "high": [row["adjHigh"] for row in payload],
                "low": [row["adjLow"] for row in payload],
                "close": [row["adjClose"] for row in payload],
                "volume": [row["adjVolume"] for row in payload],
            }
        )
        return to_contract(bars, symbol, self.name, self._clock())


def fetch_daily(source: PriceSource, symbol: str, start: date, end: date) -> pd.DataFrame:
    """Download daily bars for one symbol from `source`."""
    if end < start:
        raise ValueError("end must not be before start")
    return source.fetch(symbol, start, end)

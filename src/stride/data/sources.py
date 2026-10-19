"""Daily price download per provider (SPEC section 5).

Every source returns a DataFrame indexed by `date` (timezone-naive) with the
section 5 contract columns: symbol, open, high, low, close, volume, source,
fetched_at. Prices are adjusted for dividends and splits.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Protocol

import pandas as pd
import requests

CONTRACT_COLUMNS = ("symbol", "open", "high", "low", "close", "volume", "source", "fetched_at")
DATA_API_KEY_ENV = "DATA_API_KEY"
ALPACA_KEY_ENV = "ALPACA_API_KEY"
ALPACA_SECRET_ENV = "ALPACA_SECRET_KEY"
HTTP_OK = 200
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
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


class UnadjustedCloseSource(Protocol):
    name: str

    def fetch_unadjusted_close(self, symbol: str, start: date, end: date) -> pd.Series:
        """Raw (unadjusted) closes from `start` to `end`, indexed by `date`."""
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


def _get_json(
    session: HttpSession,
    *,
    provider: str,
    symbol: str,
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
) -> Any:
    """GET `url` and return its JSON. Errors never echo the URL, params or headers.

    Network errors and HTTP 429/5xx are retried up to `MAX_RETRIES` times with
    exponential backoff (SPEC section 8); other HTTP errors fail at once.
    """
    for attempt in range(MAX_RETRIES + 1):
        retry = attempt < MAX_RETRIES
        try:
            response = session.get(
                url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            if retry:
                time.sleep(RETRY_BACKOFF_SECONDS * 2**attempt)
                continue
            raise SourceError(
                f"{provider} request for {symbol} failed: {type(exc).__name__}"
            ) from None
        if response.status_code in RETRY_STATUS and retry:
            time.sleep(RETRY_BACKOFF_SECONDS * 2**attempt)
            continue
        if response.status_code != HTTP_OK:
            raise SourceError(f"{provider} returned HTTP {response.status_code} for {symbol}")
        return response.json()
    raise AssertionError("unreachable")  # pragma: no cover


def _close_series(dates: list[Any], closes: list[float]) -> pd.Series:
    """Unadjusted closes indexed by timezone-naive `date`, oldest first."""
    index = pd.DatetimeIndex(pd.to_datetime(dates)).tz_localize(None).normalize()
    series = pd.Series(closes, index=index, name="close", dtype=float)
    series.index.name = "date"
    return series[~series.index.duplicated(keep="last")].sort_index()


class TiingoSource:
    """Tiingo end-of-day prices (free plan). The primary source (SPEC section 5)."""

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

    def _rows(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        # The key travels in a header so it never appears in URLs, logs or errors.
        rows: list[dict[str, Any]] = _get_json(
            self._session,
            provider=self.name,
            symbol=symbol,
            url=self.url.format(symbol=symbol.lower()),
            params={"startDate": start.isoformat(), "endDate": end.isoformat()},
            headers={"Authorization": f"Token {self._key}"},
        )
        return rows

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Split- and dividend-adjusted bars in the section 5 contract."""
        payload = self._rows(symbol, start, end)
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

    def fetch_unadjusted_close(self, symbol: str, start: date, end: date) -> pd.Series:
        """Raw closes, for the two-source comparison (validation 4)."""
        payload = self._rows(symbol, start, end)
        return _close_series([row["date"] for row in payload], [row["close"] for row in payload])


class AlpacaSource:
    """Alpaca Market Data daily bars from the consolidated SIP feed.

    The second source for validation 4 (SPEC section 5). The free Basic plan serves
    SIP history except the latest 15 minutes, so day t is available at the 18:00 run.
    Keys are the paper-trading keys from `.env`; they travel only in headers.
    """

    name = "alpaca"
    url = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    page_limit = 10_000

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        session: HttpSession | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        env = os.environ if env is None else env
        key = api_key if api_key is not None else env.get(ALPACA_KEY_ENV, "")
        secret = secret_key if secret_key is not None else env.get(ALPACA_SECRET_ENV, "")
        if not key or not secret:
            raise SourceError(f"{ALPACA_KEY_ENV} and {ALPACA_SECRET_ENV} must be set")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._session: HttpSession = session or requests.Session()
        self._clock = clock

    def _bars(self, symbol: str, start: date, end: date, adjustment: str) -> list[dict[str, Any]]:
        bars: list[dict[str, Any]] = []
        params = {
            "timeframe": "1Day",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "feed": "sip",
            "adjustment": adjustment,
            "limit": str(self.page_limit),
        }
        while True:
            payload = _get_json(
                self._session,
                provider=self.name,
                symbol=symbol,
                url=self.url.format(symbol=symbol.upper()),
                params=params,
                headers=self._headers,
            )
            bars.extend(payload.get("bars") or [])
            token = payload.get("next_page_token")
            if not token:
                return bars
            params = {**params, "page_token": token}

    @staticmethod
    def _dates(bars: list[dict[str, Any]]) -> list[date]:
        # Daily bars are stamped at midnight New York time, expressed in UTC.
        stamps = pd.DatetimeIndex(pd.to_datetime([bar["t"] for bar in bars], utc=True))
        return [ts.date() for ts in stamps.tz_convert("America/New_York")]

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Adjusted bars (`adjustment=all`) in the section 5 contract."""
        bars = self._bars(symbol, start, end, "all")
        frame = pd.DataFrame(
            {
                "date": self._dates(bars),
                "open": [bar["o"] for bar in bars],
                "high": [bar["h"] for bar in bars],
                "low": [bar["l"] for bar in bars],
                "close": [bar["c"] for bar in bars],
                "volume": [bar["v"] for bar in bars],
            }
        )
        return to_contract(frame, symbol, self.name, self._clock())

    def fetch_unadjusted_close(self, symbol: str, start: date, end: date) -> pd.Series:
        """Raw SIP closes (`adjustment=raw`), for validation 4."""
        bars = self._bars(symbol, start, end, "raw")
        return _close_series(list(self._dates(bars)), [bar["c"] for bar in bars])


def fetch_daily(source: PriceSource, symbol: str, start: date, end: date) -> pd.DataFrame:
    """Download daily bars for one symbol from `source`."""
    if end < start:
        raise ValueError("end must not be before start")
    return source.fetch(symbol, start, end)

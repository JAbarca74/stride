"""Daily price download per provider (SPEC section 5).

Every source returns a DataFrame indexed by `date` (timezone-naive) with the
section 5 contract columns: symbol, open, high, low, close, volume, source,
fetched_at. Prices are adjusted for dividends and splits.
"""

from __future__ import annotations

import io
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import pandas as pd
import requests

from stride.data.calendar import sessions

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
    sip_delay = pd.Timedelta(minutes=16)  # Basic plan withholds the latest 15 minutes

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
            "end": self._end_param(end),
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

    def _end_param(self, end: date) -> str:
        """The last second of `end` in New York, capped at `now - sip_delay`.

        The Basic plan answers HTTP 403 for any range that reaches into the latest
        15 minutes, so a bare date for today fails even after the close. A timestamp
        old enough still returns today's bar at the 18:00 run.
        """
        day_end = pd.Timestamp(end + timedelta(days=1), tz="America/New_York") - pd.Timedelta(
            seconds=1
        )
        latest = pd.Timestamp(self._clock()).tz_convert("UTC") - self.sip_delay
        return min(day_end.tz_convert("UTC"), latest).strftime("%Y-%m-%dT%H:%M:%SZ")

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


# Spot pairs used before each crypto ETF existed (SPEC section 5, task T19).
CRYPTO_SPOT_PRODUCTS = {"IBIT": "BTC-USD", "ETHA": "ETH-USD"}


class CoinbaseSource:
    """USD spot session bars built from Coinbase Exchange 15-minute candles (no key).

    `symbol` is a Coinbase product id such as `BTC-USD`. Per SPEC 1.1 section 5, a bar
    exists only for NYSE trading days and covers the NYSE session: open = first trade
    from 9:30 ET, high/low = extremes during the session, close = last trade before
    the session close (16:00 ET, or the early close). Weekend moves therefore appear as
    gaps, exactly as in the ETF.
    """

    name = "coinbase"
    url = "https://api.exchange.coinbase.com/products/{product}/candles"
    granularity = timedelta(minutes=15)
    max_candles = 300  # API limit per request

    def __init__(
        self,
        session: HttpSession | None = None,
        clock: Clock = _utc_now,
        min_interval_seconds: float = 0.15,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session: HttpSession = session or requests.Session()
        self._clock = clock
        self._min_interval = min_interval_seconds
        self._sleep = sleep

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        nyse = sessions(start, end)
        if nyse.empty:
            return to_contract(pd.DataFrame(), symbol, self.name, self._clock())
        candles = self._candles(symbol, nyse)
        bars = _session_bars(candles, nyse)
        return to_contract(bars, symbol, self.name, self._clock())

    def _windows(self, nyse: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Group consecutive sessions so each request stays within the candle limit."""
        span = self.granularity * self.max_candles
        windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        opens = list(nyse["market_open"])
        closes = list(nyse["market_close"])
        first = 0
        for i in range(1, len(opens) + 1):
            if i == len(opens) or closes[i] - opens[first] > span:
                windows.append((opens[first], closes[i - 1] - self.granularity))
                first = i
        return windows

    def _candles(self, product: str, nyse: pd.DataFrame) -> pd.DataFrame:
        rows: list[list[float]] = []
        for number, (window_start, window_end) in enumerate(self._windows(nyse)):
            if number and self._min_interval:
                self._sleep(self._min_interval)
            rows.extend(
                _get_json(
                    self._session,
                    provider=self.name,
                    symbol=product,
                    url=self.url.format(product=product),
                    params={
                        "granularity": str(int(self.granularity.total_seconds())),
                        "start": window_start.isoformat(),
                        "end": window_end.isoformat(),
                    },
                    headers={"User-Agent": "stride"},
                )
            )
        # Candle layout: [time, low, high, open, close, volume]; time = candle start.
        candles = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
        candles["time"] = pd.to_datetime(candles["time"], unit="s", utc=True)
        return candles.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def _session_bars(candles: pd.DataFrame, nyse: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 15-minute candles into one bar per NYSE session."""
    rows: list[dict[str, Any]] = []
    for day, market_open, market_close in zip(
        nyse.index, nyse["market_open"], nyse["market_close"], strict=True
    ):
        inside = candles[(candles["time"] >= market_open) & (candles["time"] < market_close)]
        if inside.empty:
            continue  # no trades in the session: the day stays missing
        rows.append(
            {
                "date": day,
                "open": inside["open"].iloc[0],
                "high": inside["high"].max(),
                "low": inside["low"].min(),
                "close": inside["close"].iloc[-1],
                "volume": inside["volume"].sum(),
            }
        )
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def splice_spot_with_etf(
    spot: pd.DataFrame,
    etf: pd.DataFrame,
    symbol: str,
    trading_days: pd.DatetimeIndex,
) -> pd.DataFrame:
    """One continuous series for a crypto ETF: scaled spot history, then the ETF itself.

    Spot bars are kept only on NYSE trading days before the ETF's first bar. Their
    prices are multiplied by `etf_close / spot_close` on that first day, so the series
    has no jump at the switch and daily returns before it are the spot returns.
    Spot volume is converted to ETF-share equivalents with the same ratio. The `source`
    column keeps the provider of each row, which marks the spot-to-ETF switch.

    Raises `SourceError` if the spot series lacks the switch day or if any trading day
    between the first and last bar is missing.
    """
    if etf.empty:
        raise SourceError(f"{symbol}: ETF series is empty")
    etf = etf.sort_index()
    switch = pd.Timestamp(etf.index[0])
    spot_on_switch = spot["close"][spot.index == switch]
    if spot_on_switch.empty:
        raise SourceError(f"{symbol}: spot series has no bar on the switch day {switch.date()}")
    ratio = float(etf["close"].iloc[0]) / float(spot_on_switch.iloc[0])

    days = pd.DatetimeIndex(trading_days).normalize()
    before = spot[(spot.index < switch) & spot.index.isin(days)].copy()
    for column in ("open", "high", "low", "close"):
        before[column] = before[column] * ratio
    before["volume"] = (before["volume"] / ratio).round().astype("int64")

    combined = pd.concat([before, etf]).sort_index()
    combined["symbol"] = symbol
    combined.index.name = "date"

    first, last = combined.index.min(), combined.index.max()
    expected = days[(days >= first) & (days <= last)]
    missing = expected.difference(combined.index)
    if len(missing) > 0:
        shown = ", ".join(day.date().isoformat() for day in missing[:5])
        raise SourceError(f"{symbol}: {len(missing)} missing trading days after splice: {shown}")
    return combined.loc[:, list(CONTRACT_COLUMNS)]


class TextResponse(Protocol):
    status_code: int

    @property
    def text(self) -> str: ...


class TextSession(Protocol):
    def get(self, url: str, *, params: Mapping[str, str], timeout: float) -> TextResponse: ...


class FredRateSource:
    """Daily 3-month T-bill rate (FRED series DTB3) as an annual fraction. No key needed.

    FRED leaves holidays empty; those days are dropped. Use
    `leveraged.rate_known_at_t_minus_1` to align it without look-ahead.
    """

    name = "fred"
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"

    def __init__(self, series_id: str = "DTB3", session: TextSession | None = None) -> None:
        self._series_id = series_id
        self._session: TextSession = session or requests.Session()

    def fetch_rate(self) -> pd.Series:
        try:
            response = self._session.get(
                self.url, params={"id": self._series_id}, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise SourceError(
                f"fred request for {self._series_id} failed: {type(exc).__name__}"
            ) from None
        if response.status_code != HTTP_OK:
            raise SourceError(f"fred returned HTTP {response.status_code} for {self._series_id}")
        frame = pd.read_csv(io.StringIO(response.text))
        frame.columns = ["date", "value"]
        values = pd.to_numeric(frame["value"], errors="coerce")
        rate = pd.Series(
            values.to_numpy() / 100, index=pd.DatetimeIndex(frame["date"]), name="rate"
        )
        rate.index.name = "date"
        return rate.dropna().sort_index()

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest
import requests

from stride.data.sources import (
    CONTRACT_COLUMNS,
    SourceError,
    TiingoSource,
    fetch_daily,
    to_contract,
)
from stride.data.store import connect, read_prices, upsert_prices

NOW = datetime(2026, 10, 19, 20, 30, tzinfo=UTC)
FAKE_KEY = "test-key-not-real"

TIINGO_PAYLOAD = [
    {
        "date": "2026-10-15T00:00:00.000Z",
        "open": 580.0,
        "high": 585.0,
        "low": 578.0,
        "close": 584.0,
        "volume": 50_000_000,
        "adjOpen": 579.0,
        "adjHigh": 584.0,
        "adjLow": 577.0,
        "adjClose": 583.0,
        "adjVolume": 50_000_000.4,
        "divCash": 0.0,
        "splitFactor": 1.0,
    },
    {
        "date": "2026-10-16T00:00:00.000Z",
        "open": 584.0,
        "high": 590.0,
        "low": 583.0,
        "close": 589.0,
        "volume": 40_000_000,
        "adjOpen": 583.0,
        "adjHigh": 589.0,
        "adjLow": 582.0,
        "adjClose": 588.0,
        "adjVolume": 40_000_000.0,
        "divCash": 0.0,
        "splitFactor": 1.0,
    },
]


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _tiingo(session: FakeSession) -> TiingoSource:
    return TiingoSource(api_key=FAKE_KEY, session=session, clock=lambda: NOW)


def test_tiingo_returns_contract_columns() -> None:
    session = FakeSession(FakeResponse(200, TIINGO_PAYLOAD))
    frame = fetch_daily(_tiingo(session), "SPY", date(2026, 10, 15), date(2026, 10, 16))

    assert tuple(frame.columns) == CONTRACT_COLUMNS
    assert frame.index.name == "date"
    assert list(frame.index) == [pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-16")]
    assert pd.DatetimeIndex(frame.index).tz is None
    assert (frame["symbol"] == "SPY").all()
    assert (frame["source"] == "tiingo").all()
    assert (frame["fetched_at"] == pd.Timestamp(NOW)).all()
    assert frame["volume"].dtype == "int64"
    assert frame["volume"].iloc[0] == 50_000_000


def test_tiingo_uses_adjusted_prices() -> None:
    frame = _tiingo(FakeSession(FakeResponse(200, TIINGO_PAYLOAD))).fetch(
        "SPY", date(2026, 10, 15), date(2026, 10, 16)
    )
    first = frame.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"]) == (
        579.0,
        584.0,
        577.0,
        583.0,
    )


def test_tiingo_request_keeps_key_out_of_url_and_params() -> None:
    session = FakeSession(FakeResponse(200, TIINGO_PAYLOAD))
    _tiingo(session).fetch("SPY", date(2026, 10, 15), date(2026, 10, 16))
    call = session.calls[0]
    assert call["url"] == "https://api.tiingo.com/tiingo/daily/spy/prices"
    assert call["params"] == {"startDate": "2026-10-15", "endDate": "2026-10-16"}
    assert call["headers"] == {"Authorization": f"Token {FAKE_KEY}"}
    assert FAKE_KEY not in call["url"]
    assert FAKE_KEY not in str(call["params"])


def test_tiingo_http_error_does_not_leak_key() -> None:
    source = _tiingo(FakeSession(FakeResponse(401, {"detail": "bad token"})))
    with pytest.raises(SourceError, match="HTTP 401") as info:
        source.fetch("SPY", date(2026, 10, 15), date(2026, 10, 16))
    assert FAKE_KEY not in str(info.value)


def test_tiingo_network_error_becomes_source_error() -> None:
    source = _tiingo(FakeSession(requests.ConnectionError(f"boom {FAKE_KEY}")))
    with pytest.raises(SourceError, match="ConnectionError") as info:
        source.fetch("SPY", date(2026, 10, 15), date(2026, 10, 16))
    assert FAKE_KEY not in str(info.value)


def test_tiingo_reads_key_from_env() -> None:
    session = FakeSession(FakeResponse(200, []))
    source = TiingoSource(session=session, env={"DATA_API_KEY": FAKE_KEY}, clock=lambda: NOW)
    source.fetch("SPY", date(2026, 10, 15), date(2026, 10, 16))
    assert session.calls[0]["headers"]["Authorization"] == f"Token {FAKE_KEY}"


def test_tiingo_without_key_refuses_to_start() -> None:
    with pytest.raises(SourceError, match="DATA_API_KEY is not set"):
        TiingoSource(env={})


def test_empty_response_returns_empty_contract_frame() -> None:
    frame = _tiingo(FakeSession(FakeResponse(200, []))).fetch(
        "SPY", date(2026, 10, 15), date(2026, 10, 16)
    )
    assert frame.empty
    assert tuple(frame.columns) == CONTRACT_COLUMNS
    assert frame.index.name == "date"


def test_reversed_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="end must not be before start"):
        fetch_daily(
            _tiingo(FakeSession(FakeResponse(200, []))),
            "SPY",
            date(2026, 10, 16),
            date(2026, 10, 15),
        )


def test_to_contract_sorts_and_drops_duplicate_dates() -> None:
    bars = pd.DataFrame(
        {
            "date": ["2026-10-16", "2026-10-15", "2026-10-16"],
            "open": [1.0, 1.0, 2.0],
            "high": [1.0, 1.0, 2.0],
            "low": [1.0, 1.0, 2.0],
            "close": [1.0, 1.0, 2.0],
            "volume": [1, 1, 2],
        }
    )
    frame = to_contract(bars, "SPY", "test", NOW)
    assert list(frame.index) == [pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-16")]
    assert frame["close"].iloc[-1] == 2.0


def test_downloaded_frame_can_be_stored() -> None:
    frame = _tiingo(FakeSession(FakeResponse(200, TIINGO_PAYLOAD))).fetch(
        "SPY", date(2026, 10, 15), date(2026, 10, 16)
    )
    conn = connect(":memory:")
    assert upsert_prices(conn, frame) == 2
    assert read_prices(conn, "SPY")["close"].tolist() == [583.0, 588.0]
    conn.close()

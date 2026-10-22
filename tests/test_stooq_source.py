from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest
import requests

from stride.data.sources import CONTRACT_COLUMNS, SourceError, StooqSource

NOW = datetime(2026, 10, 19, 20, 30, tzinfo=UTC)
START, END = date(2026, 10, 15), date(2026, 10, 16)

CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-10-16,584.0,590.0,583.0,589.0,40000000\n"
    "2026-10-15,580.0,585.0,578.0,584.0,50000000.4\n"
)
CHALLENGE = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
    "<noscript>This site requires JavaScript to verify your browser.</noscript></body></html>"
)


class TextResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class TextSession:
    """Returns the given responses (or raises the given errors) in order."""

    def __init__(self, outcomes: list[TextResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: float,
    ) -> TextResponse:
        outcome = self.outcomes[len(self.calls)]
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers)})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry backoffs instead of sleeping."""
    recorded: list[float] = []
    monkeypatch.setattr("stride.data.sources.time.sleep", recorded.append)
    return recorded


def _stooq(*outcomes: TextResponse | Exception) -> tuple[StooqSource, TextSession]:
    session = TextSession(list(outcomes))
    return StooqSource(session=session, clock=lambda: NOW), session


def test_fetch_returns_contract_sorted_by_date() -> None:
    source, _ = _stooq(TextResponse(200, CSV))
    frame = source.fetch("SPY", START, END)
    assert tuple(frame.columns) == CONTRACT_COLUMNS
    assert list(frame.index) == [pd.Timestamp("2026-10-15"), pd.Timestamp("2026-10-16")]
    assert frame.index.name == "date"
    assert frame["close"].tolist() == [584.0, 589.0]
    assert frame["volume"].tolist() == [50_000_000, 40_000_000]
    assert set(frame["source"]) == {"stooq"}
    assert set(frame["symbol"]) == {"SPY"}


def test_request_uses_us_ticker_and_compact_dates() -> None:
    source, session = _stooq(TextResponse(200, CSV))
    source.fetch("SPY", START, END)
    assert session.calls[0]["url"] == "https://stooq.com/q/d/l/"
    assert session.calls[0]["params"] == {
        "s": "spy.us",
        "i": "d",
        "d1": "20261015",
        "d2": "20261016",
    }


def test_unadjusted_close_returns_close_series() -> None:
    source, _ = _stooq(TextResponse(200, "﻿" + CSV))
    closes = source.fetch_unadjusted_close("SPY", START, END)
    assert closes.tolist() == [584.0, 589.0]
    assert closes.index.name == "date"


def test_no_data_returns_empty_contract_frame() -> None:
    source, _ = _stooq(TextResponse(200, "No data"))
    frame = source.fetch("SPY", START, END)
    assert frame.empty
    assert tuple(frame.columns) == CONTRACT_COLUMNS


def test_browser_verification_page_is_an_error_and_not_retried(sleeps: list[float]) -> None:
    source, session = _stooq(TextResponse(200, CHALLENGE))
    with pytest.raises(SourceError, match="browser-verification page"):
        source.fetch_unadjusted_close("SPY", START, END)
    assert len(session.calls) == 1
    assert sleeps == []


def test_unexpected_body_is_an_error() -> None:
    source, _ = _stooq(TextResponse(200, "Exceeded the daily hits limit"))
    with pytest.raises(SourceError, match="unexpected response for SPY"):
        source.fetch("SPY", START, END)


def test_transient_errors_are_retried_with_backoff(sleeps: list[float]) -> None:
    source, session = _stooq(
        requests.ConnectionError("reset"), TextResponse(429, ""), TextResponse(200, CSV)
    )
    assert len(source.fetch("SPY", START, END)) == 2
    assert len(session.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_client_error_fails_at_once(sleeps: list[float]) -> None:
    source, session = _stooq(TextResponse(404, ""))
    with pytest.raises(SourceError, match="stooq returned HTTP 404 for SPY"):
        source.fetch("SPY", START, END)
    assert len(session.calls) == 1
    assert sleeps == []

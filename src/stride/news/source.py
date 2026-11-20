"""Alpaca news (Benzinga) with the paper keys of `.env` (design section 1).

Free with the Alpaca account; the keys travel only in headers and never appear in
an error text. Pages of 50, newest or oldest first, following `next_page_token`.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

from stride.data.sources import SourceError

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
PAGE_LIMIT = 50
TIMEOUT_SECONDS = 60
RETRIES = 3
BACKOFF_SECONDS = 2.0


# A `requests.Session` (or a fake with the same `get` in tests).
Session = Any


@dataclass(frozen=True)
class Article:
    id: int
    ts: pd.Timestamp  # created_at, UTC
    headline: str
    symbols: tuple[str, ...]
    url: str


def _article(raw: dict[str, Any]) -> Article:
    return Article(
        id=int(raw["id"]),
        ts=pd.Timestamp(raw["created_at"]).tz_convert("UTC"),
        headline=str(raw.get("headline") or ""),
        symbols=tuple(str(s) for s in raw.get("symbols") or ()),
        url=str(raw.get("url") or ""),
    )


class AlpacaNews:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        session: Session | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        if not api_key or not secret_key:
            raise SourceError("Alpaca keys are required for the news")
        self._headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
        self._session: Session = session or requests.Session()
        self._sleep = sleep

    def _page(self, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(RETRIES + 1):
            try:
                response = self._session.get(
                    NEWS_URL, params=params, headers=self._headers, timeout=TIMEOUT_SECONDS
                )
            except requests.RequestException as exc:
                if attempt == RETRIES:
                    raise SourceError(f"alpaca news failed: {type(exc).__name__}") from None
                self._sleep(BACKOFF_SECONDS * 2**attempt)
                continue
            status = int(response.status_code)
            if status == 200:  # noqa: PLR2004
                return dict(response.json())
            if status in {429, 500, 502, 503, 504} and attempt < RETRIES:
                self._sleep(BACKOFF_SECONDS * 2**attempt)
                continue
            raise SourceError(f"alpaca news failed: HTTP {status}")
        raise SourceError("alpaca news failed")  # pragma: no cover

    def articles(
        self,
        symbols: Sequence[str],
        start: pd.Timestamp,
        end: pd.Timestamp | None = None,
    ) -> Iterator[Article]:
        """Every article tagged with any of `symbols` published in [start, end),
        oldest first."""
        params = {
            "symbols": ",".join(symbols),
            "start": pd.Timestamp(start).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": str(PAGE_LIMIT),
            "sort": "asc",
            "include_content": "false",
        }
        if end is not None:
            params["end"] = pd.Timestamp(end).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        while True:
            payload = self._page(params)
            for raw in payload.get("news") or []:
                yield _article(raw)
            token = payload.get("next_page_token")
            if not token:
                return
            params = {**params, "page_token": str(token)}

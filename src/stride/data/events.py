"""Flagged price events for validation 2 (SPEC section 5).

A daily change above `data.quality.max_daily_change` stops the run unless its day
is flagged. Flags live in `reference/price_events.yaml` (versioned): each one names
the symbol, the day, the kind of event and who confirmed it, so every exception to
validation 2 is reviewed by a person and traceable in git history.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

DEFAULT_EVENTS_PATH = Path(__file__).resolve().parents[3] / "reference" / "price_events.yaml"


class PriceEvent(BaseModel):
    """One reviewed exception to validation 2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    kind: Literal["split", "provider_error", "market_move"]
    detail: str
    confirmed_by: str


def load_price_events(path: str | Path = DEFAULT_EVENTS_PATH) -> dict[str, list[PriceEvent]]:
    """Read and validate the events file, keyed by symbol. A missing file means no flags."""
    file = Path(path)
    if not file.exists():
        return {}
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return {symbol: [PriceEvent(**event) for event in events] for symbol, events in raw.items()}


def flagged_dates(events: Mapping[str, list[PriceEvent]], symbol: str) -> frozenset[date]:
    return frozenset(event.date for event in events.get(symbol, []))

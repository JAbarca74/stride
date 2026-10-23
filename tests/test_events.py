from __future__ import annotations

from datetime import date
from pathlib import Path

import pydantic
import pytest

from stride.config import load_config
from stride.data.events import flagged_dates, load_price_events

ROOT = Path(__file__).resolve().parents[1]


def test_reference_events_file_flags_the_silver_crash() -> None:
    events = load_price_events()
    config = load_config(ROOT / "config.yaml", env={})
    assert set(events) <= set(config.universe)
    assert flagged_dates(events, "SLV") == frozenset({date(2026, 1, 30)})
    assert all(e.confirmed_by for symbol_events in events.values() for e in symbol_events)


def test_missing_file_means_no_flags(tmp_path: Path) -> None:
    events = load_price_events(tmp_path / "absent.yaml")
    assert events == {}
    assert flagged_dates(events, "SLV") == frozenset()


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.yaml"
    path.write_text(
        "SLV:\n  - {date: 2026-01-30, kind: hunch, detail: x, confirmed_by: y}\n",
        encoding="utf-8",
    )
    with pytest.raises(pydantic.ValidationError):
        load_price_events(path)


def test_event_without_confirmation_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.yaml"
    path.write_text(
        "SLV:\n  - {date: 2026-01-30, kind: market_move, detail: x}\n", encoding="utf-8"
    )
    with pytest.raises(pydantic.ValidationError):
        load_price_events(path)

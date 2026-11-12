from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from stride.data.intraday import (
    SLOTS_PER_DAY,
    init_schema,
    last_intraday_ts,
    read_intraday,
    regular_session,
    session_slots,
    slot_bars,
    slot_inputs,
    update_intraday,
    upsert_intraday,
)

DAY = date(2026, 10, 15)  # a Thursday in daylight time: session 13:30-20:00 UTC
EARLY_CLOSE = date(2026, 11, 27)  # day after Thanksgiving: 14:30-18:00 UTC
OPEN = datetime(2026, 10, 15, 13, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 16, 23, 0, tzinfo=UTC)
HALF_HOUR = timedelta(minutes=30)


def _bars(start: datetime, count: int, first_close: float = 100.0) -> pd.DataFrame:
    """`count` consecutive 30-minute bars from `start`; close rises 1.0 per bar."""
    closes = [first_close + i for i in range(count)]
    return pd.DataFrame(
        {
            "ts": pd.to_datetime([start + i * HALF_HOUR for i in range(count)], utc=True),
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000 + i for i in range(count)],
        }
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return conn


class FakeSource:
    """Serves the bars of `history` that fall in the requested range."""

    name = "fake"

    def __init__(self, history: pd.DataFrame) -> None:
        self.history = history
        self.calls: list[tuple[datetime, datetime]] = []

    def fetch_intraday(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "30Min"
    ) -> pd.DataFrame:
        self.calls.append((start, end))
        stamps = self.history["ts"]
        inside = (stamps >= pd.Timestamp(start)) & (stamps <= pd.Timestamp(end))
        return self.history.loc[inside].reset_index(drop=True)


def test_session_slots_full_day_has_seven_slots() -> None:
    grid = session_slots(DAY, DAY)
    assert grid["slot"].tolist() == list(range(SLOTS_PER_DAY))
    assert grid["start"].iloc[0] == pd.Timestamp(OPEN)
    assert grid["end"].iloc[0] == pd.Timestamp(OPEN) + HALF_HOUR
    assert grid["end"].iloc[-1] == pd.Timestamp("2026-10-15T20:00:00Z")


def test_session_slots_early_close_and_holiday() -> None:
    grid = session_slots(date(2026, 11, 26), EARLY_CLOSE)  # Thanksgiving has no session
    assert grid["day"].unique().tolist() == [pd.Timestamp(EARLY_CLOSE)]
    assert grid["slot"].tolist() == [0, 1, 2, 3]
    assert grid["end"].iloc[-1] == pd.Timestamp("2026-11-27T18:00:00Z")


def test_regular_session_drops_extended_hours_and_holidays() -> None:
    premarket = _bars(OPEN - 2 * HALF_HOUR, 2)
    session = _bars(OPEN, 13)
    after = _bars(datetime(2026, 10, 15, 20, 0, tzinfo=UTC), 2)
    saturday = _bars(datetime(2026, 10, 17, 14, 0, tzinfo=UTC), 1)
    kept = regular_session(pd.concat([premarket, session, after, saturday], ignore_index=True))
    assert kept["ts"].tolist() == session["ts"].tolist()


def test_regular_session_respects_early_close() -> None:
    start = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    kept = regular_session(_bars(start, 13))
    assert len(kept) == 7  # 9:30 to 13:00 New York
    assert kept["ts"].iloc[-1] == pd.Timestamp("2026-11-27T17:30:00Z")


def test_upsert_twice_does_not_duplicate_and_updates() -> None:
    conn = _conn()
    bars = _bars(OPEN, 3)
    assert upsert_intraday(conn, "QQQ", bars, "fake", NOW) == 3
    revised = bars.assign(close=bars["close"] + 0.25)
    upsert_intraday(conn, "QQQ", revised, "fake", NOW)
    stored = read_intraday(conn, "QQQ")
    assert len(stored) == 3
    assert stored["close"].tolist() == revised["close"].tolist()
    assert last_intraday_ts(conn, "QQQ") == pd.Timestamp(OPEN) + 2 * HALF_HOUR
    assert last_intraday_ts(conn, "IWM") is None


def test_upsert_rejects_missing_values_and_bad_bars() -> None:
    conn = _conn()
    bars = _bars(OPEN, 2)
    with pytest.raises(ValueError, match="empty values"):
        upsert_intraday(conn, "QQQ", bars.assign(close=[1.0, float("nan")]), "fake", NOW)
    with pytest.raises(ValueError, match="missing intraday columns"):
        upsert_intraday(conn, "QQQ", bars.drop(columns="volume"), "fake", NOW)
    with pytest.raises(sqlite3.IntegrityError):
        upsert_intraday(conn, "QQQ", bars.assign(low=bars["high"] + 1.0), "fake", NOW)


def test_read_intraday_filters_by_time() -> None:
    conn = _conn()
    upsert_intraday(conn, "QQQ", _bars(OPEN, 13), "fake", NOW)
    part = read_intraday(conn, "QQQ", start=OPEN + HALF_HOUR, end=OPEN + 3 * HALF_HOUR)
    assert len(part) == 3
    with pytest.raises(ValueError, match="timezone-aware"):
        read_intraday(conn, "QQQ", start=datetime(2026, 10, 15, 13, 30))


def test_update_stores_only_completed_published_bars() -> None:
    conn = _conn()
    source = FakeSource(_bars(OPEN, 13))
    # 15:10 UTC: the 14:30 bar closed at 15:00 but is inside the 16-minute data delay.
    now = datetime(2026, 10, 15, 15, 10, tzinfo=UTC)
    result = update_intraday(conn, source, "QQQ", OPEN - timedelta(days=30), now)
    assert result.rows == 2
    assert last_intraday_ts(conn, "QQQ") == pd.Timestamp("2026-10-15T14:00:00Z")


def test_update_is_incremental() -> None:
    conn = _conn()
    start = OPEN - timedelta(days=30)
    history = pd.concat(
        [_bars(OPEN, 13), _bars(OPEN + timedelta(days=1), 13, 113.0)], ignore_index=True
    )
    source = FakeSource(history)
    update_intraday(conn, source, "QQQ", start, datetime(2026, 10, 15, 23, 0, tzinfo=UTC))
    second = update_intraday(conn, source, "QQQ", start, NOW)
    assert not second.refetched
    assert len(read_intraday(conn, "QQQ")) == 26
    # The second request starts at the overlap, not at the beginning of history.
    assert pd.Timestamp(source.calls[1][0]) > pd.Timestamp(start)


def test_update_refetches_when_history_was_readjusted() -> None:
    conn = _conn()
    start = OPEN - timedelta(days=30)
    first_day = _bars(OPEN, 13)
    update_intraday(
        conn, FakeSource(first_day), "TLT", start, datetime(2026, 10, 15, 23, 0, tzinfo=UTC)
    )
    # A dividend: the provider lowers every past price by 0.4%.
    readjusted = first_day.copy()
    for column in ("open", "high", "low", "close"):
        readjusted[column] *= 0.996
    history = pd.concat([readjusted, _bars(OPEN + timedelta(days=1), 13, 113.0)], ignore_index=True)
    source = FakeSource(history)
    result = update_intraday(conn, source, "TLT", start, NOW)
    assert result.refetched
    assert result.rows == 26
    stored = read_intraday(conn, "TLT")
    assert stored["close"].iloc[0] == pytest.approx(100.0 * 0.996)
    assert pd.Timestamp(source.calls[-1][0]) == pd.Timestamp(start)


def test_update_with_no_new_bars_writes_nothing() -> None:
    conn = _conn()
    result = update_intraday(conn, FakeSource(_bars(OPEN, 0)), "QQQ", OPEN, NOW)
    assert result.rows == 0
    assert not result.refetched


def test_slot_bars_aggregates_a_full_session() -> None:
    slots = slot_bars(_bars(OPEN, 13))
    assert slots["slot"].tolist() == list(range(SLOTS_PER_DAY))
    first, second = slots.iloc[0], slots.iloc[1]
    assert first["close"] == 100.0  # the 9:30 bar alone
    assert first["end"] == pd.Timestamp(OPEN) + HALF_HOUR
    # Slot 1 is the 10:00 and 10:30 bars.
    assert second["open"] == 100.5
    assert second["close"] == 102.0
    assert second["high"] == 103.0
    assert second["low"] == 100.0
    assert second["volume"] == 1001 + 1002
    assert slots["close"].iloc[-1] == 112.0


def test_slot_bars_asof_drops_unfinished_slots() -> None:
    bars = _bars(OPEN, 4)  # 9:30, 10:00, 10:30, 11:00: slot 2 has only its first half
    asof = datetime(2026, 10, 15, 15, 30, tzinfo=UTC)
    assert slot_bars(bars, asof=asof)["slot"].tolist() == [0, 1]
    assert slot_bars(bars)["slot"].tolist() == [0, 1, 2]


def test_slot_bars_skips_slots_without_trades() -> None:
    bars = _bars(OPEN, 13).drop(index=[3, 4]).reset_index(drop=True)  # no 11:00-12:00 bars
    assert slot_bars(bars)["slot"].tolist() == [0, 1, 3, 4, 5, 6]


def test_slot_bars_empty() -> None:
    assert slot_bars(_bars(OPEN, 0)).empty


def test_slot_inputs_cut_the_grid_at_asof_and_start_at_the_earliest_bar() -> None:
    conn = _conn()
    upsert_intraday(conn, "AAA", _bars(OPEN, 13), "fake", NOW)
    upsert_intraday(conn, "BBB", _bars(OPEN + timedelta(days=1), 13), "fake", NOW)
    asof = datetime(2026, 10, 16, 15, 30, tzinfo=UTC)  # the first two slots of day 2 have ended
    grid, slots = slot_inputs(conn, ["AAA", "BBB"], asof)
    assert list(grid.columns) == ["day", "slot", "end"]
    assert len(grid) == SLOTS_PER_DAY + 2
    assert grid["end"].iloc[-1] == pd.Timestamp("2026-10-16T15:00:00Z")
    assert len(slots["AAA"]) == SLOTS_PER_DAY  # listed on day 1 only
    assert slots["BBB"]["slot"].tolist() == [0, 1]  # no slot is built from half its bars


def test_slot_inputs_need_stored_bars() -> None:
    with pytest.raises(ValueError, match="no intraday bars"):
        slot_inputs(_conn(), ["AAA"], NOW)

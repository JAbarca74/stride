from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stride.forecast.daily import DAILY_SLOTS, build_daily_panel, eligible, quality_table
from stride.forecast.returns import standardize
from stride.forecast.simulate import simulate
from stride.forecast.walkforward import last_slot_rows, realized


def _bars(days: pd.DatetimeIndex, seed: int, vol: float = 0.01) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, vol, size=len(days))))
    opens = np.concatenate([close[:1], close[:-1]])
    return pd.DataFrame({"open": opens, "close": close}, index=days)


DAYS = pd.bdate_range("2024-01-02", periods=300)


def test_panel_has_one_slot_per_session_and_close_to_close_returns() -> None:
    bars = {"AAA": _bars(DAYS, 1), "BBB": _bars(DAYS, 2)}
    panel = build_daily_panel(bars, DAYS)
    assert panel.symbols == ("AAA", "BBB")
    assert len(panel) == len(DAYS)
    assert (panel.slot == 0).all()
    assert panel.day.equals(DAYS)
    assert np.isnan(panel.ret[0]).all()
    expected = np.log(bars["AAA"]["close"].to_numpy()[1:] / bars["AAA"]["close"].to_numpy()[:-1])
    assert panel.ret[1:, 0] == pytest.approx(expected)
    # Every row closes a session, and the end is the 16:00 New York close in UTC.
    assert last_slot_rows(panel).tolist() == list(range(len(DAYS)))
    assert panel.end[0] == pd.Timestamp("2024-01-02 21:00", tz="UTC")
    assert realized(panel, 10, 5)[1] == pytest.approx(panel.close[15, 1] / panel.close[10, 1] - 1)


def test_late_start_and_missing_session() -> None:
    late = _bars(DAYS[100:], 3).drop(DAYS[150])
    panel = build_daily_panel({"AAA": _bars(DAYS, 1), "LATE": late}, DAYS)
    assert np.isnan(panel.close[:100, 1]).all()
    assert np.isnan(panel.ret[:101, 1]).all()  # the first bar has no previous close
    assert np.isfinite(panel.ret[101:, 1]).all()
    # The missing session repeats the previous close and has no open.
    assert panel.close[150, 1] == panel.close[149, 1]
    assert panel.ret[150, 1] == 0.0
    assert np.isnan(panel.open[150, 1])


def test_bars_outside_the_sessions_never_enter() -> None:
    bars = {"AAA": _bars(DAYS, 1)}
    cut = build_daily_panel(bars, DAYS[:200])
    assert len(cut) == 200
    changed = bars["AAA"].copy()
    changed.iloc[200:] *= 3.0  # rewrite the future
    again = build_daily_panel({"AAA": changed}, DAYS[:200])
    assert np.array_equal(cut.close, again.close)
    assert np.array_equal(cut.ret, again.ret, equal_nan=True)


def test_panel_rejects_unsorted_days_and_duplicate_bars() -> None:
    bars = {"AAA": _bars(DAYS, 1)}
    with pytest.raises(ValueError, match="sorted"):
        build_daily_panel(bars, DAYS[::-1])
    doubled = pd.concat([bars["AAA"], bars["AAA"].iloc[:1]])
    with pytest.raises(ValueError, match="more than one bar"):
        build_daily_panel({"AAA": doubled}, DAYS)


def test_eligible_is_the_as_of_universe() -> None:
    panel = build_daily_panel({"AAA": _bars(DAYS, 1), "LATE": _bars(DAYS[100:], 3)}, DAYS)
    assert eligible(panel, 10, 20).tolist() == [False, False]  # not enough rows yet
    assert eligible(panel, 20, 20).tolist() == [True, False]
    # LATE has its first return at row 101, so a 20-return window is full at row 120.
    assert eligible(panel, 119, 20).tolist() == [True, False]
    assert eligible(panel, 120, 20).tolist() == [True, True]
    with pytest.raises(ValueError, match="outside"):
        eligible(panel, len(panel), 20)
    with pytest.raises(ValueError, match="window"):
        eligible(panel, 5, 0)


def test_eligible_does_not_look_ahead() -> None:
    bars = {"AAA": _bars(DAYS, 1), "LATE": _bars(DAYS[100:], 3)}
    full = build_daily_panel(bars, DAYS)
    for row in (50, 120, 200):
        assert eligible(full, row, 20).tolist() == eligible(full.upto(row + 1), row, 20).tolist()


def test_the_30_minute_simulator_runs_on_the_daily_panel() -> None:
    """The filtered bootstrap is reused unchanged with one slot per day."""
    calm, wild = _bars(DAYS, 1, vol=0.005), _bars(DAYS, 2, vol=0.02)
    panel = build_daily_panel({"CALM": calm, "WILD": wild}, DAYS)
    window = panel.ret[-252:]
    slot = panel.slot[-252:]
    filtered = standardize(window, slot, span=60)
    assert filtered.factors[0] == pytest.approx(np.ones(2))  # a single slot: no seasonality

    def run(seed: int) -> np.ndarray:
        return simulate(
            filtered,
            slot,
            start_slot=0,
            horizons=(5, 20),
            n_paths=2000,
            mean_block=2.0,
            drift_shrink=0.0,
            vol_span=60,
            rng=np.random.default_rng(seed),
            n_slots=DAILY_SLOTS,
        )

    paths = run(7)
    assert paths.shape == (2, 2000, 2)
    assert np.array_equal(paths, run(7))  # reproducible
    sigma = paths.std(axis=1)
    assert (sigma[1] > sigma[0]).all()  # risk grows with the horizon
    assert sigma[1, 1] > 2.0 * sigma[1, 0]  # the wilder asset is simulated as riskier
    # 20-day risk of a 2% a day asset is about 9%: the scale is the data's, not a slot's.
    assert 0.05 < sigma[1, 1] < 0.15


def test_quality_table_counts_gaps_and_suspect_values() -> None:
    clean = _bars(DAYS, 1)
    dirty = _bars(DAYS[10:], 2).drop([DAYS[20], DAYS[21]])
    dirty.loc[DAYS[30], "close"] = float(np.asarray(dirty.loc[DAYS[29], "close"])) * 1.5
    dirty.loc[DAYS[40:44], "close"] = dirty.loc[DAYS[39], "close"]
    dirty.loc[pd.Timestamp("2024-03-02")] = dirty.loc[DAYS[39]]  # a Saturday
    table = quality_table({"CLEAN": clean, "DIRTY": dirty}, DAYS).set_index("symbol")
    assert table.loc["CLEAN", "bars"] == len(DAYS)
    assert table.loc["CLEAN", "missing_sessions"] == 0
    assert table.loc["CLEAN", "off_calendar_bars"] == 0
    assert table.loc["CLEAN", "non_positive_prices"] == 0
    assert table.loc["DIRTY", "first"] == DAYS[10].date().isoformat()
    assert table.loc["DIRTY", "missing_sessions"] == 2
    assert table.loc["DIRTY", "off_calendar_bars"] == 1
    assert table.loc["DIRTY", "max_abs_change"] == pytest.approx(0.5)
    assert table.loc["DIRTY", "max_abs_change_date"] == DAYS[30].date().isoformat()
    assert int(np.asarray(table.loc["DIRTY", "longest_flat_run"])) >= 4


def test_quality_table_handles_an_empty_symbol() -> None:
    empty = pd.DataFrame({"open": [], "close": []}, index=pd.DatetimeIndex([]))
    table = quality_table({"NONE": empty}, DAYS)
    assert table.loc[0, "symbol"] == "NONE"
    assert table.loc[0, "bars"] == 0

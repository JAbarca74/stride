from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from stride.config import load_config
from stride.risk.sizing import (
    UnitLot,
    apply_caps,
    group_limits,
    target_usd,
    unit_shares,
    unit_usd,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})  # asset 4, group 6, crypto 4, total 12
DAY0 = date(2026, 1, 5)


def _lots(symbol: str, count: int, start_day: int = 0) -> list[UnitLot]:
    return [UnitLot(DAY0 + timedelta(days=start_day + i), 0, symbol) for i in range(count)]


# Unit size.


def test_unit_size_by_hand() -> None:
    # 1% of $10,000 = $100 risk; N = $2 -> 50 shares; at $40 -> $2,000.
    assert unit_shares(10_000, 2.0, 0.01) == pytest.approx(50)
    assert unit_usd(10_000, 2.0, 40.0, 0.01) == pytest.approx(2_000)


def test_unit_size_allows_fractional_shares() -> None:
    assert unit_shares(100, 3.0, 0.01) == pytest.approx(1 / 3)


@pytest.mark.parametrize("n", [0.0, -1.0, math.nan, math.inf])
def test_invalid_n_is_rejected(n: float) -> None:
    with pytest.raises(ValueError, match="N must"):
        unit_shares(10_000, n, 0.01)


def test_invalid_price_and_equity_are_rejected() -> None:
    with pytest.raises(ValueError, match="price"):
        unit_usd(10_000, 2.0, 0.0, 0.01)
    with pytest.raises(ValueError, match="equity"):
        unit_shares(-1, 2.0, 0.01)


def test_target_usd_multiplies_units() -> None:
    result = target_usd({"SPY": 2, "GLD": 0}, 10_000, {"SPY": 2.0}, {"SPY": 40.0}, 0.01)
    assert result == {"SPY": pytest.approx(4_000)}


# Caps, one test each; trimming always cuts the most recent unit first.


def test_group_limits_from_config() -> None:
    group_of, limits = group_limits(CONFIG)
    assert group_of["SPY"] == "equities"
    assert "DBC" not in group_of and "UUP" not in group_of
    assert limits == {"equities": 6, "bonds": 6, "metals": 6, "crypto": 4}


def test_per_asset_cap_of_4_cuts_the_newest_unit() -> None:
    result = apply_caps(_lots("SPY", 5), CONFIG)
    assert result.units() == {"SPY": 4}
    assert result.trimmed == [_lots("SPY", 5)[-1]]


def test_group_cap_of_6_cuts_the_newest_units() -> None:
    lots = _lots("SPY", 4) + _lots("EFA", 4, start_day=10)  # EFA added later
    result = apply_caps(lots, CONFIG)
    assert result.units() == {"SPY": 4, "EFA": 2}
    assert [lot.added for lot in result.trimmed] == [lot.added for lot in lots[-2:]]


@pytest.mark.parametrize(("a", "b"), [("IEF", "TLT"), ("GLD", "SLV")])
def test_bond_and_metal_groups_cap_at_6(a: str, b: str) -> None:
    result = apply_caps(_lots(a, 4) + _lots(b, 4, start_day=10), CONFIG)
    assert sum(result.units().values()) == 6


def test_crypto_group_cap_of_4() -> None:
    lots = _lots("IBIT", 3) + _lots("ETHA", 3, start_day=10)
    result = apply_caps(lots, CONFIG)
    assert result.units() == {"IBIT": 3, "ETHA": 1}


def test_ungrouped_assets_only_count_against_asset_and_total_caps() -> None:
    result = apply_caps(_lots("DBC", 4) + _lots("UUP", 4, start_day=10), CONFIG)
    assert result.units() == {"DBC": 4, "UUP": 4}


def test_total_cap_of_12_cuts_the_newest_units() -> None:
    lots = (
        _lots("SPY", 4)
        + _lots("IEF", 4, start_day=10)
        + _lots("GLD", 4, start_day=20)
        + _lots("DBC", 4, start_day=30)  # the newest: all four cut
    )
    result = apply_caps(lots, CONFIG)
    assert sum(result.units().values()) == 12
    assert "DBC" not in result.units()
    assert result.trimmed == lots[-4:]


def test_trimming_is_independent_of_input_order() -> None:
    lots = _lots("SPY", 4) + _lots("EFA", 4, start_day=10)
    assert apply_caps(lots[::-1], CONFIG).units() == apply_caps(lots, CONFIG).units()


def test_same_day_units_are_ordered_by_sequence() -> None:
    lots = [UnitLot(DAY0, seq, symbol) for seq, symbol in enumerate(["IBIT", "ETHA"] * 3)]
    result = apply_caps(lots, CONFIG)  # crypto cap 4: the last two signals are cut
    assert result.trimmed == lots[-2:]
    assert result.units() == {"IBIT": 2, "ETHA": 2}


def test_trimmed_unit_frees_no_room_for_older_ones() -> None:
    # A cut unit never pushes out an older one.
    lots = _lots("SPY", 6)
    result = apply_caps(lots, CONFIG)
    assert result.kept == lots[:4]

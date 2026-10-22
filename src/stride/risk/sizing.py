"""Volatility-based unit sizing and exposure caps (SPEC section 6). Pure, no I/O.

Unit size (Larry Hite / Turtles): each unit risks `risk_per_unit` of equity against a
1N move, so

    unit_shares = risk_per_unit * equity / N
    unit_usd    = unit_shares * price

Exposure caps, in units, before leverage: per asset (`max_units_per_asset`), per
correlated group (`max_units_group`; the `crypto` group uses `max_units_crypto`) and
for the whole portfolio (`max_units_total`). Symbols in no group (DBC, UUP) only
count towards their own and the total cap. When signals exceed a cap, the most
recent units are cut first (last in, first out).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from stride.config import StrideConfig

CRYPTO_GROUP = "crypto"


def unit_shares(equity: float, n: float, risk_per_unit: float) -> float:
    """Shares (fractional) in one unit: a 1N move costs `risk_per_unit` of equity."""
    if not (n > 0 and math.isfinite(n)):
        raise ValueError("N must be a positive number")
    if equity < 0:
        raise ValueError("equity must not be negative")
    return risk_per_unit * equity / n


def unit_usd(equity: float, n: float, price: float, risk_per_unit: float) -> float:
    """Dollar size of one unit: `(risk_per_unit * equity / N) * price`."""
    if not (price > 0 and math.isfinite(price)):
        raise ValueError("price must be a positive number")
    return unit_shares(equity, n, risk_per_unit) * price


@dataclass(frozen=True, order=True)
class UnitLot:
    """One unit of one asset. Lots sort oldest first: by day, then by `seq` within
    the day (the order in which the signals were produced)."""

    added: date
    seq: int
    symbol: str


@dataclass(frozen=True)
class CapResult:
    kept: list[UnitLot]
    trimmed: list[UnitLot]

    def units(self) -> dict[str, int]:
        """Units kept per symbol."""
        return dict(Counter(lot.symbol for lot in self.kept))


def group_limits(config: StrideConfig) -> tuple[dict[str, str], dict[str, int]]:
    """Map each grouped symbol to its group, and each group to its unit cap."""
    group_of = {symbol: name for name, members in config.groups.items() for symbol in members}
    limits = {
        name: config.risk.max_units_crypto if name == CRYPTO_GROUP else config.risk.max_units_group
        for name in config.groups
    }
    return group_of, limits


def apply_caps(lots: Iterable[UnitLot], config: StrideConfig) -> CapResult:
    """Keep units oldest first while every cap allows them; cut the rest.

    Accepting the oldest units first is the same as cutting the most recent ones
    first: a unit is cut only if older units already fill one of its caps.
    """
    group_of, group_cap = group_limits(config)
    per_asset: Counter[str] = Counter()
    per_group: Counter[str] = Counter()
    kept: list[UnitLot] = []
    trimmed: list[UnitLot] = []
    for lot in sorted(lots):
        group = group_of.get(lot.symbol)
        fits = (
            per_asset[lot.symbol] < config.strategy.max_units_per_asset
            and (group is None or per_group[group] < group_cap[group])
            and len(kept) < config.risk.max_units_total
        )
        if fits:
            kept.append(lot)
            per_asset[lot.symbol] += 1
            if group is not None:
                per_group[group] += 1
        else:
            trimmed.append(lot)
    return CapResult(kept, trimmed)


def target_usd(
    units: Mapping[str, int],
    equity: float,
    n: Mapping[str, float],
    price: Mapping[str, float],
    risk_per_unit: float,
) -> dict[str, float]:
    """Dollar target per symbol: units * unit size (before leverage)."""
    return {
        symbol: count * unit_usd(equity, n[symbol], price[symbol], risk_per_unit)
        for symbol, count in units.items()
        if count > 0
    }

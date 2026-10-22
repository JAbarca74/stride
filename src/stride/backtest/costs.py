"""Trading and financing costs for the backtest (SPEC section 9). Pure, no I/O.

- Spread and slippage: `spread_bps` per side, `spread_bps_crypto` for the crypto
  group and its leveraged versions. Buys fill above the reference price, sells below.
- Commission, IBKR Pro Fixed, per order:
  `min(max(commission_per_share * shares, commission_min_per_order),
       commission_max_pct_of_value * notional)`.
  With small orders the 1% cap binds, so every order costs about 1%.
- Margin interest: (risk-free + `margin_spread`) per year on the borrowed amount.
- Uninvested cash earns the risk-free rate.

Interest accrues per calendar day on an actual/360 basis, the convention IBKR uses
for USD balances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from stride.config import StrideConfig

Side = Literal["buy", "sell"]
BPS = 10_000
DAY_COUNT = 360  # actual/360


def crypto_symbols(config: StrideConfig) -> set[str]:
    """The crypto group plus its leveraged versions."""
    members = set(config.groups.get("crypto", []))
    leveraged = {etf for asset in members for etf in config.leveraged_etfs.get(asset, {}).values()}
    return members | leveraged


def spread_bps(symbol: str, config: StrideConfig) -> float:
    """Spread and slippage per side for `symbol`, in basis points."""
    if symbol in crypto_symbols(config):
        return config.costs.spread_bps_crypto
    return config.costs.spread_bps


def fill_price(reference: float, side: Side, symbol: str, config: StrideConfig) -> float:
    """Price after spread and slippage: above the reference to buy, below to sell."""
    move = spread_bps(symbol, config) / BPS
    return reference * (1 + move) if side == "buy" else reference * (1 - move)


def commission(shares: float, price: float, config: StrideConfig) -> float:
    """IBKR Pro Fixed commission for one order of `shares` at `price`."""
    if shares <= 0 or price <= 0:
        raise ValueError("shares and price must be positive")
    costs = config.costs
    per_share = max(costs.commission_per_share * shares, costs.commission_min_per_order)
    return min(per_share, costs.commission_max_pct_of_value * shares * price)


@dataclass(frozen=True)
class FillCost:
    price: float
    shares: float
    notional: float
    spread_cost: float
    commission: float

    @property
    def total(self) -> float:
        return self.spread_cost + self.commission


def order_cost(
    notional: float, reference: float, side: Side, symbol: str, config: StrideConfig
) -> FillCost:
    """Fill an order for `notional` dollars at `reference` (e.g. the next open)."""
    if notional <= 0 or reference <= 0:
        raise ValueError("notional and reference price must be positive")
    price = fill_price(reference, side, symbol, config)
    shares = notional / price
    spread_cost = abs(price - reference) * shares
    return FillCost(price, shares, notional, spread_cost, commission(shares, price, config))


def margin_interest(borrowed: float, risk_free: float, days: int, config: StrideConfig) -> float:
    """Interest on `borrowed` dollars for `days` calendar days."""
    if borrowed <= 0:
        return 0.0
    return borrowed * (risk_free + config.costs.margin_spread) * days / DAY_COUNT


def cash_interest(cash: float, risk_free: float, days: int) -> float:
    """Interest earned by uninvested cash for `days` calendar days."""
    if cash <= 0:
        return 0.0
    return cash * risk_free * days / DAY_COUNT

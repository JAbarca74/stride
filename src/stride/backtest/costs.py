"""Trading and financing costs for the backtest (SPEC section 9). Pure, no I/O.

- Spread and slippage: `spread_bps` per side, `spread_bps_crypto` for the crypto
  group and its leveraged versions. Buys fill above the reference price, sells below.
- Commission per order, by `costs.pricing`:
  - IBKR Pro Fixed: `min(max(commission_per_share * shares, commission_min_per_order),
    commission_max_pct_of_value * notional)`. With small orders the 1% cap binds.
  - IBKR Pro Tiered: `min(max(tiered_per_share * shares, tiered_min_per_order),
    tiered_max_pct_of_value * notional) + tiered_third_party_per_share * shares`
    (exchange, clearing and regulatory fees). Cheaper than Fixed for orders of
    about $35 to a few hundred dollars, where Fixed charges its $1 minimum.
- Margin interest: (risk-free + `margin_spread`) per year on the borrowed amount.
- Uninvested cash earns the risk-free rate only above `cash_interest_free_usd`
  (IBKR pays nothing on the first $10,000), at a rate scaled by NAV up to
  `cash_interest_full_rate_nav` (IBKR pays less below $100,000 of NAV).

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
    """IBKR Pro commission for one order of `shares` at `price` (Fixed or Tiered)."""
    if shares <= 0 or price <= 0:
        raise ValueError("shares and price must be positive")
    costs = config.costs
    notional = shares * price
    if costs.pricing == "tiered":
        base = max(costs.tiered_per_share * shares, costs.tiered_min_per_order)
        return min(base, costs.tiered_max_pct_of_value * notional) + (
            costs.tiered_third_party_per_share * shares
        )
    per_share = max(costs.commission_per_share * shares, costs.commission_min_per_order)
    return min(per_share, costs.commission_max_pct_of_value * notional)


def max_commission_rate(config: StrideConfig) -> float:
    """An upper bound on commission / notional for an order of at least $1 of an
    ETF priced at $1 or more, used to keep a cash reserve for fees."""
    costs = config.costs
    if costs.pricing == "tiered":
        return costs.tiered_max_pct_of_value + costs.tiered_third_party_per_share
    return costs.commission_max_pct_of_value


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


def account_cash_interest(
    cash: float, equity: float, risk_free: float, days: int, config: StrideConfig
) -> float:
    """Interest the broker pays on uninvested cash: nothing on the first
    `cash_interest_free_usd`, and the rest at the risk-free rate scaled by
    equity / `cash_interest_full_rate_nav` (capped at the full rate)."""
    costs = config.costs
    earning = cash - costs.cash_interest_free_usd
    if earning <= 0 or equity <= 0:
        return 0.0
    scale = min(1.0, equity / costs.cash_interest_full_rate_nav)
    return cash_interest(earning, risk_free * scale, days)

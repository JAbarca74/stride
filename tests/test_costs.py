from __future__ import annotations

from pathlib import Path

import pytest

from stride.backtest.costs import (
    account_cash_interest,
    cash_interest,
    commission,
    crypto_symbols,
    fill_price,
    margin_interest,
    max_commission_rate,
    order_cost,
    spread_bps,
)
from stride.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})  # Tiered pricing (owner decision)
FIXED = CONFIG.model_copy(  # IBKR Pro Fixed: $0.005 per share, $1 minimum, 1% cap
    update={"costs": CONFIG.costs.model_copy(update={"pricing": "fixed"})}
)


def test_crypto_symbols_include_leveraged_versions() -> None:
    assert crypto_symbols(CONFIG) == {"IBIT", "ETHA", "BITX", "ETHU"}


@pytest.mark.parametrize(("symbol", "bps"), [("SPY", 5), ("TMF", 5), ("IBIT", 10), ("BITX", 10)])
def test_spread_per_symbol(symbol: str, bps: float) -> None:
    assert spread_bps(symbol, CONFIG) == bps


def test_fill_price_moves_against_the_trader() -> None:
    assert fill_price(100.0, "buy", "SPY", CONFIG) == pytest.approx(100.05)
    assert fill_price(100.0, "sell", "SPY", CONFIG) == pytest.approx(99.95)
    assert fill_price(100.0, "buy", "IBIT", CONFIG) == pytest.approx(100.10)


@pytest.mark.parametrize(
    ("shares", "price", "expected", "case"),
    [
        (0.04, 500.0, 0.20, "small $20 order: 1% cap binds"),
        (0.2, 100.0, 0.20, "$20 at $100: still the 1% cap"),
        (100, 50.0, 1.00, "$5,000: the $1 minimum binds"),
        (1_000, 100.0, 5.00, "$100,000: $0.005 per share"),
        (300, 10.0, 1.50, "$3,000: 300 x $0.005 = $1.50, above the $1 minimum"),
    ],
)
def test_ibkr_fixed_commission(shares: float, price: float, expected: float, case: str) -> None:
    assert commission(shares, price, FIXED) == pytest.approx(expected), case


def test_every_small_order_costs_about_one_percent() -> None:
    for notional in (5.0, 20.0, 60.0, 99.0):
        assert commission(notional / 400.0, 400.0, FIXED) == pytest.approx(0.01 * notional)


def test_invalid_order_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        commission(0, 100.0, CONFIG)
    with pytest.raises(ValueError, match="positive"):
        order_cost(-5, 100.0, "buy", "SPY", CONFIG)


def test_order_cost_of_a_small_buy() -> None:
    cost = order_cost(20.0, 400.0, "buy", "SPY", FIXED)
    assert cost.price == pytest.approx(400.2)
    assert cost.shares == pytest.approx(20.0 / 400.2)
    assert cost.spread_cost == pytest.approx(0.2 * 20.0 / 400.2)  # ~ 5 bps of $20
    assert cost.commission == pytest.approx(0.20)  # 1% cap
    assert cost.total == pytest.approx(cost.spread_cost + 0.20)


def test_margin_interest_by_hand() -> None:
    # $1,000 borrowed at 4% + 1.5% for 30 days, actual/360: 1000 * 0.055 * 30/360
    assert margin_interest(1_000, 0.04, 30, CONFIG) == pytest.approx(1_000 * 0.055 * 30 / 360)
    assert margin_interest(0, 0.04, 30, CONFIG) == 0.0


def test_cash_interest_by_hand() -> None:
    assert cash_interest(1_000, 0.04, 360) == pytest.approx(40.0)
    assert cash_interest(-5, 0.04, 30) == 0.0


@pytest.mark.parametrize(
    ("shares", "price", "expected", "case"),
    [
        (0.04, 500.0, 0.20 + 0.003 * 0.04, "$20: the 1% cap binds, plus fees per share"),
        (0.5, 100.0, 0.35 + 0.003 * 0.5, "$50: the $0.35 minimum"),
        (2, 100.0, 0.35 + 0.003 * 2, "$200: $0.35 instead of Fixed's $1"),
        (1_000, 100.0, 3.5 + 3.0, "$100,000: $0.0035 per share plus fees"),
    ],
)
def test_ibkr_tiered_commission(shares: float, price: float, expected: float, case: str) -> None:
    assert commission(shares, price, CONFIG) == pytest.approx(expected), case


def test_tiered_is_cheaper_than_fixed_from_about_35_dollars() -> None:
    for notional in (50.0, 100.0, 200.0, 500.0):
        shares = notional / 100.0
        assert commission(shares, 100.0, CONFIG) < commission(shares, 100.0, FIXED)
    assert max_commission_rate(CONFIG) >= 0.01 and max_commission_rate(FIXED) == 0.01


def test_broker_pays_no_interest_on_the_first_10000() -> None:
    assert account_cash_interest(9_000, 9_500, 0.04, 30, CONFIG) == 0.0
    # $5,000 above the free band, NAV $15,000: 15% of the full rate.
    expected = cash_interest(5_000, 0.04 * 15_000 / 100_000, 30)
    assert account_cash_interest(15_000, 15_000, 0.04, 30, CONFIG) == pytest.approx(expected)
    full = account_cash_interest(110_000, 200_000, 0.04, 30, CONFIG)
    assert full == pytest.approx(cash_interest(100_000, 0.04, 30))

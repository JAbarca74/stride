from __future__ import annotations

from pathlib import Path

import pytest

from stride.backtest.costs import (
    cash_interest,
    commission,
    crypto_symbols,
    fill_price,
    margin_interest,
    order_cost,
    spread_bps,
)
from stride.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})  # 5/10 bps, $0.005, $1 min, 1% cap


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
    assert commission(shares, price, CONFIG) == pytest.approx(expected), case


def test_every_small_order_costs_about_one_percent() -> None:
    for notional in (5.0, 20.0, 60.0, 99.0):
        assert commission(notional / 400.0, 400.0, CONFIG) == pytest.approx(0.01 * notional)


def test_invalid_order_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        commission(0, 100.0, CONFIG)
    with pytest.raises(ValueError, match="positive"):
        order_cost(-5, 100.0, "buy", "SPY", CONFIG)


def test_order_cost_of_a_small_buy() -> None:
    cost = order_cost(20.0, 400.0, "buy", "SPY", CONFIG)
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

"""SPEC 1.5: the opt-in answers to the open spec questions (docs/design/open_questions.md).

Every new key defaults to the behaviour before SPEC 1.5, so the running paper
instances and the documented backtests are unchanged until a config opts in.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from stride.backtest.run import etf_fee_estimate, leveraged_etf_fees
from stride.config import StrideConfig, load_config
from stride.execution.sim_broker import SimBroker
from stride.risk.sizing import UnitLot, apply_caps
from stride.strategy.rules import (
    ENTRY_HH55,
    HOLD,
    PYRAMID,
    STOP_2N,
    Indicators,
    PositionState,
    decide,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})


def _with(config: StrideConfig, section: str, **values: object) -> StrideConfig:
    part = getattr(config, section).model_copy(update=values)
    return config.model_copy(update={section: part})


# Defaults keep the old behaviour.


def test_defaults_match_the_behaviour_before_spec_1_5() -> None:
    assert CONFIG.strategy.stop_basis == "daily_n"
    assert CONFIG.risk.vol_span == 60
    assert CONFIG.risk.cap_tie_break == "signal_order"
    assert CONFIG.execution.sim_limit_fill == "limit_plus_spread"
    assert CONFIG.reporting.correlation_days == 756
    assert CONFIG.reporting.correlation_flag == pytest.approx(0.7)


@pytest.mark.parametrize("name", ["paper_b", "paper_dip"])
def test_paper_instances_keep_the_defaults(name: str) -> None:
    config = load_config(ROOT / f"config.{name}.yaml", env={})
    assert config.strategy.stop_basis == "daily_n"
    assert config.risk.vol_span == 60
    assert config.risk.cap_tie_break == "signal_order"
    assert config.execution.sim_limit_fill == "limit_plus_spread"


# Question 2: the N of the stop.


def test_entry_stores_the_n_of_the_entry_day() -> None:
    ind = Indicators(sma200=100.0, hh55=110.0, ll20=95.0, n=4.0)
    decision = decide(111.0, ind, PositionState(), CONFIG.strategy)
    assert (decision.rule, decision.entry_n) == (ENTRY_HH55, 4.0)


def test_entry_n_basis_keeps_the_stop_of_the_entry() -> None:
    # Entry at 108 with N 4 -> stop at 100. Today N doubled to 8 (daily stop at 92).
    state = PositionState(units=1, last_entry=108.0, entry_n=4.0)
    today = Indicators(sma200=90.0, hh55=110.0, ll20=95.0, n=8.0)
    daily = decide(99.0, today, state, CONFIG.strategy)
    entry = decide(99.0, today, state, _with(CONFIG, "strategy", stop_basis="entry_n").strategy)
    assert daily.rule != STOP_2N
    assert entry.rule == STOP_2N


def test_entry_n_basis_uses_the_stored_n_for_the_pyramid_step() -> None:
    # last entry 108, stored N 4 -> next add at 110; today's N 8 would need 112.
    state = PositionState(units=1, last_entry=108.0, entry_n=4.0)
    today = Indicators(sma200=90.0, hh55=120.0, ll20=95.0, n=8.0)
    strategy = _with(CONFIG, "strategy", stop_basis="entry_n").strategy
    added = decide(110.5, today, state, strategy)
    assert (added.rule, added.target_units, added.entry_n) == (PYRAMID, 2, 8.0)
    assert decide(110.5, today, state, CONFIG.strategy).rule == HOLD


def test_entry_n_basis_without_a_stored_n_falls_back_to_daily_n() -> None:
    state = PositionState(units=1, last_entry=108.0)
    today = Indicators(sma200=90.0, hh55=110.0, ll20=95.0, n=4.0)
    strategy = _with(CONFIG, "strategy", stop_basis="entry_n").strategy
    assert decide(99.0, today, state, strategy).rule == STOP_2N


def test_hold_keeps_the_stored_n() -> None:
    state = PositionState(units=1, last_entry=108.0, entry_n=4.0)
    today = Indicators(sma200=90.0, hh55=120.0, ll20=95.0, n=8.0)
    assert decide(108.0, today, state, CONFIG.strategy).entry_n == 4.0


# Question 9: cap tie-break.


def test_tie_break_by_volatility_cuts_the_more_volatile_asset() -> None:
    config = _with(CONFIG, "risk", max_units_total=1, cap_tie_break="higher_vol_first")
    day = date(2026, 1, 5)
    lots = [UnitLot(day, 0, "SPY"), UnitLot(day, 1, "GLD")]
    vol = {"SPY": 0.30, "GLD": 0.10}
    assert apply_caps(lots, config, vol).units() == {"GLD": 1}
    old = _with(config, "risk", cap_tie_break="signal_order")
    assert apply_caps(lots, old, vol).units() == {"SPY": 1}


def test_tie_break_never_reorders_days() -> None:
    config = _with(CONFIG, "risk", max_units_total=1, cap_tie_break="higher_vol_first")
    lots = [UnitLot(date(2026, 1, 6), 0, "GLD"), UnitLot(date(2026, 1, 5), 1, "SPY")]
    assert apply_caps(lots, config, {"SPY": 0.9, "GLD": 0.1}).units() == {"SPY": 1}


def test_tie_break_without_vol_counts_as_most_volatile() -> None:
    config = _with(CONFIG, "risk", max_units_total=1, cap_tie_break="higher_vol_first")
    day = date(2026, 1, 5)
    lots = [UnitLot(day, 0, "SPY"), UnitLot(day, 1, "GLD")]
    assert apply_caps(lots, config, {"GLD": 0.5}).units() == {"GLD": 1}


# Question 8: limit fills in the simulator.

T0, T1 = date(2026, 10, 14), date(2026, 10, 15)
BARS = {
    "SPY": pd.DataFrame(
        [(99, 101, 98, 100), (102, 104, 100, 103)],
        index=pd.to_datetime([T0, T1]),
        columns=["open", "high", "low", "close"],
    )
}
SPREAD_IN_TRIGGER = _with(CONFIG, "execution", sim_limit_fill="spread_in_trigger")


def _buy(config: StrideConfig, limit: float) -> tuple[str, float | None]:
    broker = SimBroker(BARS, config, cash=10_000.0, start=T0)
    order_id = broker.submit_order("SPY", "buy", 100.0, "limit", limit)
    broker.step(T1)
    order = broker.get_order(order_id)
    return order.status, order.average_price


def test_limit_is_the_final_price_with_spread_in_trigger() -> None:
    # low 100 reaches 100.04 but not 100.04 x (1 - 5 bps) = 99.99
    assert _buy(SPREAD_IN_TRIGGER, 100.04)[0] == "cancelled"
    status, price = _buy(SPREAD_IN_TRIGGER, 100.06)  # 100.06 x 0.9995 = 100.01
    assert status == "filled"
    assert price == pytest.approx(100.06)


def test_buy_at_the_open_includes_the_spread_in_the_limit_check() -> None:
    # open 102 plus 5 bps = 102.051: a 102.03 limit no longer fills at the open
    status, price = _buy(SPREAD_IN_TRIGGER, 102.03)
    assert status == "filled"
    assert price == pytest.approx(102.03)
    _, price = _buy(SPREAD_IN_TRIGGER, 103.0)
    assert price == pytest.approx(102 * 1.0005)


def test_old_limit_model_adds_the_spread_to_the_limit() -> None:
    status, price = _buy(CONFIG, 101.0)
    assert status == "filled"
    assert price == pytest.approx(101.0 * 1.0005)


def test_sell_limit_with_spread_in_trigger() -> None:
    broker = SimBroker(BARS, SPREAD_IN_TRIGGER, cash=10_000.0, start=T0)
    broker.submit_order("SPY", "buy", 1_000.0)
    broker.step(T1)
    # high 104 reaches 103.96 but not 103.96 x (1 + 5 bps) = 104.01
    missed = broker.submit_order("SPY", "sell", 100.0, "limit", 103.96)
    broker.step(date(2026, 10, 16))
    assert broker.get_order(missed).status in {"cancelled", "pending"}


# Question 5: leveraged ETF fee estimate.


def test_etf_fee_estimate_is_fee_times_average_value_held() -> None:
    days = pd.DatetimeIndex(
        pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"])
    )
    bars = {"QLD": pd.DataFrame({"close": [10.0, 10.0, 10.0, 10.0]}, index=days)}
    fills = pd.DataFrame(
        {"date": [days[0], days[2]], "symbol": ["QLD", "QLD"], "side": ["buy", "sell"],
         "quantity": [100.0, 100.0]}
    )  # fmt: skip
    # held 1,000 USD on two of four days -> average 500; fee 1% -> 5 USD/yr
    assert etf_fee_estimate(fills, bars, {"QLD": 0.01}, days) == pytest.approx(5.0)
    assert etf_fee_estimate(fills.iloc[0:0], bars, {"QLD": 0.01}, days) == 0.0


def test_leveraged_etf_fees_lists_every_leveraged_version() -> None:
    fees = leveraged_etf_fees(CONFIG)
    expected = {etf for v in CONFIG.leveraged_etfs.values() for etf in v.values()}
    assert set(fees) == expected
    assert all(f == CONFIG.costs.leveraged_etf_fee for f in fees.values())

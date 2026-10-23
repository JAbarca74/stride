"""Backtest engine (SPEC sections 7, 8 and 9).

The anti-look-ahead test of T14 is extended to the whole engine: fake future
prices after day t change no record dated on or before t.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stride.backtest.engine import (
    KILLED,
    BacktestSettings,
    MarketData,
    PlannedOrder,
    _switch_summary,
    eligible_instruments,
    fit_buys,
    plan_orders,
    run_backtest,
)
from stride.config import load_config
from stride.data.calendar import trading_days
from stride.risk import guard

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
DAYS = trading_days(date(2018, 1, 2), date(2020, 12, 31))
UNDERLYING = guard.underlying_map(CONFIG)
VOLS = {"SPY": 0.18, "TLT": 0.15, "GLD": 0.16, "DBC": 0.2, "IBIT": 0.7, "ETHA": 0.9}


def _bars(close: np.ndarray, days: pd.DatetimeIndex, seed: int, vol: float) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    daily = vol / math.sqrt(252)
    open_ = close * (1 + rng.normal(0, daily / 3, len(close)))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, daily / 2, len(close))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, daily / 2, len(close))))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=days)


def _market(seed: int = 0, drift: float = 0.0008, days: pd.DatetimeIndex = DAYS) -> MarketData:
    bars = {}
    for i, (symbol, vol) in enumerate(VOLS.items()):
        rng = np.random.default_rng(seed * 100 + i)
        returns = rng.normal(drift, vol / math.sqrt(252), len(days))
        bars[symbol] = _bars(100 * np.cumprod(1 + returns), days, seed * 100 + i, vol)
    rate = pd.Series(0.02, index=pd.date_range(days[0] - pd.Timedelta(days=30), days[-1]))
    return MarketData(bars, rate)


def _settings(start: date = date(2019, 1, 2), end: date = date(2020, 12, 31)) -> BacktestSettings:
    return BacktestSettings(start=start, end=end, initial_cash=10_000.0)


# Anti-look-ahead, extended to the engine.


def _tamper(data: MarketData, cut: pd.Timestamp, factor: float) -> MarketData:
    bars = {}
    for symbol, frame in data.bars.items():
        fake = frame.copy()
        fake.loc[fake.index > cut] *= factor
        bars[symbol] = fake
    rate = data.risk_free.copy()
    rate[rate.index >= cut] = 0.5  # a future rate the engine must not read
    return MarketData(bars, rate, data.eligible)


def _upto(frame: pd.DataFrame, cut: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame
    dates = (
        pd.DatetimeIndex(frame.index.get_level_values("date"))
        if "date" in (frame.index.names or [])
        else pd.DatetimeIndex(frame["date"])
    )
    return frame[dates <= cut]


@pytest.mark.parametrize("cut_at", [120, 300, 450])
@pytest.mark.parametrize("factor", [0.3, 4.0])
def test_fake_future_prices_change_no_past_record(cut_at: int, factor: float) -> None:
    data = _market(seed=3)
    days = trading_days(date(2019, 1, 2), date(2020, 12, 31))
    cut = days[cut_at]
    real = run_backtest(data, CONFIG, _settings())
    fake = run_backtest(_tamper(data, cut, factor), CONFIG, _settings())
    for name in ("equity", "signals", "risk_decisions", "orders", "fills"):
        before, after = _upto(getattr(real, name), cut), _upto(getattr(fake, name), cut)
        pd.testing.assert_frame_equal(before, after, check_exact=True, obj=name)
    assert len(real.orders) > 0  # the run actually traded before the cut
    assert not real.equity.equals(fake.equity)  # and the future did change


# Deposits and the time-weighted return.


def test_deposits_with_zero_return_give_zero_twr() -> None:
    flat = pd.DataFrame(
        {"open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0}, index=DAYS
    )  # no trend: no signal ever fires
    rate = pd.Series(0.0, index=pd.date_range("2017-12-01", "2021-01-31"))
    result = run_backtest(
        MarketData({"SPY": flat}, rate),
        CONFIG,
        BacktestSettings(date(2019, 1, 2), date(2019, 12, 31), initial_cash=100.0),
    )
    deposits = result.cash_flows
    assert len(deposits) == 11  # February to December, the first trading day of each
    assert set(deposits) == {CONFIG.cash_flows.monthly_deposit_usd}
    assert all(pd.DatetimeIndex(deposits.index).day <= 4)
    assert result.equity["equity"].iloc[-1] == pytest.approx(100.0 + deposits.sum())
    assert result.returns.abs().max() == pytest.approx(0.0)
    assert result.equity["twr_index"].iloc[-1] == pytest.approx(1.0)
    assert result.orders.empty


def test_idle_cash_earns_the_rate_known_at_t_minus_1() -> None:
    flat = pd.DataFrame({"open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0}, index=DAYS)
    rate = pd.Series(0.036, index=pd.date_range("2017-12-01", "2021-01-31"))
    result = run_backtest(
        MarketData({"SPY": flat}, rate),
        CONFIG,
        BacktestSettings(date(2019, 3, 4), date(2019, 3, 8), initial_cash=1_000.0),
    )
    # Four overnight periods of one calendar day each, actual/360.
    assert result.equity["equity"].iloc[-1] == pytest.approx(1_000 * (1 + 0.036 / 360) ** 4)
    assert result.returns.iloc[0] == pytest.approx(0.036 / 360)


# Kill switch.


def _crash_market() -> tuple[MarketData, pd.Timestamp]:
    """DBC trends up, then gaps down 80% on one day."""
    days = trading_days(date(2018, 1, 2), date(2019, 12, 31))
    rng = np.random.default_rng(7)
    close = 100 * np.cumprod(1 + rng.normal(0.004, 0.025, len(days)))
    crash = 330
    close[crash:] = close[crash:] * 0.2
    bars = _bars(close, days, 7, 0.2)
    bars.loc[days[crash], "open"] = close[crash] * 1.01
    bars.loc[days[crash], "high"] = close[crash] * 1.02
    rate = pd.Series(0.0, index=pd.date_range("2017-12-01", "2020-01-31"))
    return MarketData({"DBC": bars}, rate), days[crash]


def test_kill_switch_at_minus_40_liquidates_and_stops() -> None:
    data, crash = _crash_market()
    # Single orders up to 95% of equity, so the position is built before the crash
    # (with the 60% limit the guard vetoes the first unit: see the PR questions).
    execution = CONFIG.execution.model_copy(update={"max_order_pct_equity": 0.95})
    config = CONFIG.model_copy(update={"execution": execution})
    result = run_backtest(
        data, config, BacktestSettings(date(2018, 1, 2), date(2019, 12, 31), initial_cash=10_000.0)
    )
    held_before = result.equity.loc[: crash - pd.Timedelta(days=1), "gross_exposure"]
    assert held_before.iloc[-1] > 0  # invested when the crash comes

    kills = result.events[result.events["type"] == "KILL_SWITCH"]
    assert list(kills["at"]) == [crash.date().isoformat()]
    assert float(result.equity["twr_index"][crash]) <= 1 - CONFIG.risk.kill_switch_dd + 1e-12

    orders = result.orders
    liquidation = orders[orders["rule"] == KILLED]
    assert len(liquidation) == 1
    assert liquidation.iloc[0]["side"] == "sell" and liquidation.iloc[0]["approved"]
    assert (pd.DatetimeIndex(orders["date"]) <= crash).all()  # never trades again

    fill = result.fills[result.fills["order_id"] == liquidation.iloc[0]["order_id"]]
    assert pd.Timestamp(fill.iloc[0]["date"]) > crash  # at the next open
    after = result.equity.loc[crash + pd.Timedelta(days=1) :]
    assert (after["gross_exposure"] == 0).all()
    assert (result.risk_decisions.loc[crash:, "binding_limit"] == KILLED).all()


# Records.


def test_records_cover_every_day_and_table() -> None:
    result = run_backtest(_market(seed=5), CONFIG, _settings())
    days = trading_days(date(2019, 1, 2), date(2020, 12, 31))
    assert list(result.equity.index) == list(days)
    assert list(result.risk_decisions.index) == list(days)
    signals = result.signals.reset_index()
    assert set(signals["symbol"]) == set(VOLS)
    assert set(signals["rule"]) <= {
        "TREND_FILTER", "STOP_2N", "EXIT_LL20", "ENTRY_HH55", "PYRAMID", "HOLD"
    }  # fmt: skip
    assert (signals["target_units"] <= CONFIG.strategy.max_units_per_asset).all()
    per_day = signals.groupby("date")["target_units"].sum()
    assert (per_day <= CONFIG.risk.max_units_total).all()
    for vetoes in result.risk_decisions["vetoes"]:
        assert isinstance(json.loads(vetoes), list)
    assert (result.equity["cash"] >= -1e-9).all()
    assert (result.equity["equity"] > 0).all()


def test_every_approved_order_passed_the_guard_limits() -> None:
    result = run_backtest(_market(seed=11), CONFIG, _settings())
    orders = result.orders
    approved = orders[orders["approved"]]
    equity = result.equity["equity"]
    for _, order in approved.iterrows():
        assert order["notional"] <= CONFIG.execution.max_order_pct_equity * equity[order["date"]]
    assert (approved.groupby("date").size() <= CONFIG.execution.max_orders_per_day).all()
    vetoed = orders[~orders["approved"]]
    assert vetoed["order_id"].isna().all()  # a vetoed order never reaches the broker


def test_fixed_leverage_replaces_the_dynamic_limit() -> None:
    result = run_backtest(
        _market(seed=5),
        CONFIG,
        BacktestSettings(date(2019, 1, 2), date(2020, 12, 31), 10_000.0, fixed_leverage=2.0),
    )
    risk = result.risk_decisions
    live = risk[risk["binding_limit"] != KILLED]
    assert (live["binding_limit"] == "FIXED").all()
    assert (live["leverage"] == 2.0).all()


def test_invalid_settings_are_rejected() -> None:
    data = _market()
    with pytest.raises(ValueError, match="initial_cash"):
        run_backtest(data, CONFIG, BacktestSettings(date(2019, 1, 2), date(2019, 2, 1), 0.0))
    with pytest.raises(ValueError, match="end"):
        run_backtest(data, CONFIG, BacktestSettings(date(2019, 2, 1), date(2019, 1, 2), 100.0))
    with pytest.raises(ValueError, match="risk-free"):
        run_backtest(
            MarketData(data.bars, pd.Series(0.02, index=pd.date_range("2020-01-01", "2020-02-01"))),
            CONFIG,
            BacktestSettings(date(2019, 1, 2), date(2019, 2, 1), 100.0),
        )
    with pytest.raises(ValueError, match="no bars"):
        run_backtest(
            MarketData({}, data.risk_free), CONFIG, _settings(date(2019, 1, 2), date(2019, 2, 1))
        )


def test_eligible_instruments_need_filters_and_bars() -> None:
    eligible = eligible_instruments(CONFIG, {"SSO", "UPRO", "TMF", "UBT"}, {"SSO", "TMF", "UBT"})
    assert eligible == {"SPY": {2: "SSO"}, "TLT": {2: "UBT", 3: "TMF"}}


def test_leveraged_route_uses_the_eligible_version() -> None:
    data = _market(seed=21, drift=0.0015)
    spy = data.bars["SPY"]
    doubled = 50 * np.cumprod(1 + 2 * spy["close"].pct_change().fillna(0.0).to_numpy())
    bars = dict(data.bars) | {"SSO": _bars(doubled, pd.DatetimeIndex(spy.index), 99, 0.36)}
    result = run_backtest(
        MarketData(bars, data.risk_free, {"SPY": {2: "SSO"}}), CONFIG, _settings()
    )
    orders = result.orders
    assert (orders["approved"] & (orders["symbol"] == "SSO")).any()
    assert (result.risk_decisions["k"] >= CONFIG.leverage_switch.enter_k).any()
    assert set(orders.loc[orders["switch"], "asset"]) <= {"SPY"}
    assert set(result.switches.index) <= {"SPY"}


def test_switch_summary_counts_switches_and_their_cost() -> None:
    switches = pd.DataFrame(
        [{"date": DAYS[0], "asset": "SPY", "from": "SPY", "to": "SSO"},
         {"date": DAYS[5], "asset": "SPY", "from": "SSO", "to": "SPY"}]
    )  # fmt: skip
    orders = pd.DataFrame(
        {"order_id": ["a", "b", "c"], "asset": ["SPY", "SPY", "GLD"],
         "switch": [True, True, False], "approved": [True, True, True]}
    )  # fmt: skip
    fills = pd.DataFrame(
        {"order_id": ["a", "b", "c"], "spread_cost": [0.5, 0.25, 9.0],
         "commission": [1.0, 1.0, 9.0]}
    )  # fmt: skip
    summary = _switch_summary(switches, orders, fills)
    assert summary.loc["SPY", "switches"] == 2
    assert summary.loc["SPY", "cost_usd"] == pytest.approx(2.75)
    assert _switch_summary(pd.DataFrame(), orders, fills).empty


# Provisional order planner.


def test_plan_orders_band_exits_and_switch_legs() -> None:
    positions = {"SPY": 1_000.0, "TLT": 500.0, "GLD": 400.0}
    targets = {"SSO": 520.0, "GLD": 450.0, "DBC": 300.0}  # SPY -> SSO, TLT exit, GLD in band
    rules = {"SPY": "PYRAMID", "TLT": "EXIT_LL20", "DBC": "ENTRY_HH55"}
    planned = plan_orders(
        targets, positions_usd=positions, underlying=UNDERLYING, rules=rules, config=CONFIG
    )
    as_tuples = {(o.symbol, o.side, round(o.notional, 2), o.switch) for o in planned}
    assert as_tuples == {
        ("SPY", "sell", 1_000.0, True),
        ("SSO", "buy", 520.0, True),
        ("TLT", "sell", 500.0, False),
        ("DBC", "buy", 300.0, False),
    }
    sides = [o.side for o in planned]
    assert sides == sorted(sides, key=lambda s: s != "sell")  # sells first


def test_plan_orders_reduces_outside_the_band_and_skips_dust() -> None:
    planned = plan_orders(
        {"SPY": 700.0, "GLD": 0.5},
        positions_usd={"SPY": 1_000.0, "IEF": 0.4},
        underlying=UNDERLYING,
        rules={},
        config=CONFIG,
    )
    assert [(o.symbol, o.side, o.notional) for o in planned] == [("SPY", "sell", 300.0)]


def test_fit_buys_uses_only_the_given_sells() -> None:
    buys = [
        PlannedOrder("SPY", "buy", 600.0, False, "SPY", "PYRAMID"),
        PlannedOrder("GLD", "buy", 400.0, False, "GLD", "ENTRY_HH55"),
    ]
    sell = PlannedOrder("TLT", "sell", 500.0, False, "TLT", "EXIT_LL20")
    alone = fit_buys(buys, cash=505.0, config=CONFIG)
    assert sum(o.notional for o in alone) * 1.01 == pytest.approx(505.0)
    assert alone[0].notional / alone[1].notional == pytest.approx(1.5)
    with_sale = fit_buys([sell, *buys], cash=520.0, config=CONFIG)  # 520 + 494.75 >= 1010
    assert sum(o.notional for o in with_sale if o.side == "buy") == pytest.approx(1_000.0)
    assert fit_buys(buys, cash=0.0, config=CONFIG) == []


SYMBOLS = ["SPY", "SSO", "UPRO", "TLT", "TMF", "GLD", "IBIT", "BITX", "DBC", "UUP"]
money = st.floats(min_value=0.0, max_value=1e6, allow_nan=False)


@settings(max_examples=300, deadline=None)
@given(
    positions=st.dictionaries(st.sampled_from(SYMBOLS), money, max_size=6),
    targets=st.dictionaries(st.sampled_from(SYMBOLS), money, max_size=6),
    cash=money,
)
def test_planner_invariants(
    positions: dict[str, float], targets: dict[str, float], cash: float
) -> None:
    # One instrument per asset in the targets, as risk/leverage.py produces.
    one_each: dict[str, float] = {}
    seen: set[str] = set()
    for symbol, value in targets.items():
        if UNDERLYING[symbol] not in seen:
            seen.add(UNDERLYING[symbol])
            one_each[symbol] = value
    planned = plan_orders(
        one_each, positions_usd=positions, underlying=UNDERLYING, rules={}, config=CONFIG
    )
    fitted = fit_buys(planned, cash, CONFIG)
    minimum = CONFIG.execution.min_order_usd
    for orders in (planned, fitted):
        sides = [o.side for o in orders]
        assert sides == sorted(sides, key=lambda s: s != "sell")  # sells first
        for o in orders:
            assert math.isfinite(o.notional) and o.notional >= minimum
            if o.side == "sell":  # never more than held: no short
                assert o.notional <= positions.get(o.symbol, 0.0)
    assert len({o.symbol for o in planned}) == len(planned)  # one order per symbol
    fee = CONFIG.costs.commission_max_pct_of_value
    proceeds = sum(o.notional for o in fitted if o.side == "sell")
    spent = sum(o.notional * (1 + fee) for o in fitted if o.side == "buy")
    assert spent <= cash + proceeds + 1e-6
    # Applying the orders lands each target within the band (when cash allows).
    if spent == sum(o.notional * (1 + fee) for o in planned if o.side == "buy"):
        after = dict(positions)
        for o in fitted:
            after[o.symbol] = after.get(o.symbol, 0.0) + (
                o.notional if o.side == "buy" else -o.notional
            )
        for symbol, target in one_each.items():
            gap = abs(after.get(symbol, 0.0) - target)
            assert gap <= max(CONFIG.execution.rebalance_band * target, minimum) + 1e-6


@settings(max_examples=15, deadline=None)
@given(seed=st.integers(min_value=0, max_value=10_000), drift=st.floats(-0.001, 0.002))
def test_engine_invariants_on_random_markets(seed: int, drift: float) -> None:
    days = trading_days(date(2019, 1, 2), date(2020, 6, 30))
    result = run_backtest(
        _market(seed=seed, drift=drift, days=days),
        CONFIG,
        BacktestSettings(date(2019, 11, 1), date(2020, 6, 30), initial_cash=1_000.0),
    )
    assert (result.equity["cash"] >= -1e-9).all()  # cash account: never borrows
    assert (result.equity["gross_exposure"] >= 0).all()  # long only
    assert (result.equity["leverage"] <= CONFIG.risk.max_etf_portfolio_leverage + 1e-9).all()
    approved = result.orders[result.orders["approved"]] if not result.orders.empty else None
    if approved is not None and not approved.empty:
        assert (approved.groupby("date").size() <= CONFIG.execution.max_orders_per_day).all()
    killed = result.events[result.events["type"] == "KILL_SWITCH"]
    drawdown = 1 - result.equity["twr_index"] / result.equity["twr_index"].cummax().clip(lower=1)
    if killed.empty:
        assert (drawdown < CONFIG.risk.kill_switch_dd).all()
    else:
        first = pd.Timestamp(killed["at"].iloc[0])
        assert drawdown[first] >= CONFIG.risk.kill_switch_dd - 1e-12
        assert (drawdown[:first].iloc[:-1] < CONFIG.risk.kill_switch_dd).all()


def test_limit_orders_and_a_missing_bar_block_the_symbol() -> None:
    data = _market(seed=5)
    missing = pd.Timestamp("2019-06-14")
    bars = dict(data.bars) | {"SPY": data.bars["SPY"].drop(index=missing)}
    result = run_backtest(
        MarketData(bars, data.risk_free),
        CONFIG,
        BacktestSettings(date(2019, 1, 2), date(2020, 12, 31), 10_000.0, order_type="limit"),
    )
    signals = result.signals.reset_index()
    assert missing not in set(signals.loc[signals["symbol"] == "SPY", "date"])
    orders = result.orders
    on_missing = orders[(pd.DatetimeIndex(orders["date"]) == missing) & (orders["asset"] == "SPY")]
    assert on_missing["reasons"].str.contains("SYMBOL_BLOCKED").all()  # vetoed, if any
    approved = orders[orders["approved"]]
    assert (approved["type"] == "limit").all()
    offset = CONFIG.execution.limit_offset
    for _, order in approved.iterrows():
        close = float(bars[order["symbol"]]["close"][order["date"]])
        expected = close * (1 + offset if order["side"] == "buy" else 1 - offset)
        assert order["limit_price"] == pytest.approx(expected, abs=0.006)

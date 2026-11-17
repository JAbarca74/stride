"""T23: the parallel Kelly paper account and the simulator's opt-in margin (SPEC 1.5, Q4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from stride import run_daily
from stride.backtest.costs import margin_interest
from stride.backtest.targets import borrow_leverage, kelly_level
from stride.config import StrideConfig, load_config
from stride.execution.sim_broker import MARGIN_CALL_REASON, SimBroker

ROOT = Path(__file__).resolve().parents[1]
DIP = load_config(ROOT / "config.paper_dip.yaml", env={})
KELLY = load_config(ROOT / "config.paper_kelly.yaml", env={})


# The file.


def test_kelly_config_differs_from_dip_only_where_allowed() -> None:
    expected: dict[str, Any] = DIP.model_dump()
    expected["data"]["db_path"] = "data/paper_kelly.db"
    expected["risk"]["allow_margin"] = True
    expected["execution"]["broker"] = "shadow"
    expected["cash_flows"]["initial_deposit_usd"] = 10_000
    expected["cash_flows"]["monthly_deposit_usd"] = 0
    expected["experiments"]["allocation"]["leverage_source"] = "kelly"
    assert KELLY.model_dump() == expected


def test_kelly_instance_never_shares_files_with_the_others() -> None:
    kelly = run_daily.load_instance(ROOT / "config.paper_kelly.yaml")
    others = [run_daily.load_instance(ROOT / f"config.paper_{n}.yaml") for n in ("b", "dip")]
    assert kelly.title == "STRIDE KELLY"
    for other in others:
        assert kelly.db != other.db and kelly.log_file != other.log_file
        assert kelly.kill_switch != other.kill_switch and kelly.halt != other.halt


def test_running_instances_keep_a_cash_account_and_a_fixed_leverage() -> None:
    for name in ("b", "dip"):
        config = load_config(ROOT / f"config.paper_{name}.yaml", env={})
        assert config.risk.allow_margin is False
        assert config.experiments.allocation.leverage_source == "fixed"


def test_kelly_instance_uses_the_shadow_broker_without_keys(tmp_path: Path) -> None:
    from stride.data import store  # noqa: PLC0415
    from stride.execution.shadow_broker import ShadowBroker  # noqa: PLC0415
    from stride.settings import Secrets  # noqa: PLC0415

    conn = store.connect(tmp_path / "kelly.db")
    days = pd.bdate_range("2026-01-05", periods=5)
    for symbol in ("SPY", "IBIT"):
        frame = pd.DataFrame(
            {"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1.0}, index=days
        )
        frame.index.name = "date"
        frame["source"], frame["fetched_at"] = "test", pd.Timestamp("2026-01-01", tz="UTC")
        frame["symbol"] = symbol
        store.upsert_prices(conn, frame)
    broker = run_daily.make_broker(KELLY, Secrets(_env_file=None), conn)  # type: ignore[call-arg]
    assert isinstance(broker, ShadowBroker)
    assert broker.get_account().cash == pytest.approx(10_000)


# Margin in the simulator.

T0, T1, T2 = date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 19)


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    index = pd.to_datetime([T0, T1, T2][: len(rows)])
    return pd.DataFrame(rows, index=index, columns=["open", "high", "low", "close"])


def _margin(config: StrideConfig, **risk: object) -> StrideConfig:
    part = config.risk.model_copy(update={"allow_margin": True, **risk})
    return config.model_copy(update={"risk": part})


FLAT = {"SPY": _bars([(100, 100, 100, 100)] * 3)}


def test_cash_account_buys_only_with_cash() -> None:
    broker = SimBroker(FLAT, DIP, cash=10_000.0, start=T0)
    order_id = broker.submit_order("SPY", "buy", 15_000.0)
    broker.step(T1)
    order = broker.get_order(order_id)
    assert order.filled_quantity * (order.average_price or 0) < 10_000
    assert broker.get_account().cash >= -1e-9
    assert broker.get_account().margin_requirement == 0


def test_margin_account_borrows_up_to_reg_t() -> None:
    config = _margin(DIP)
    broker = SimBroker(FLAT, config, cash=10_000.0, start=T0)
    assert broker.get_account().buying_power == pytest.approx(20_000)  # L_borrow 2
    order_id = broker.submit_order("SPY", "buy", 15_000.0)
    broker.step(T1)
    order = broker.get_order(order_id)
    assert order.status == "filled"
    assert order.filled_quantity * (order.average_price or 0) == pytest.approx(15_000)
    account = broker.get_account()
    assert account.cash < -4_900
    assert account.margin_requirement == pytest.approx(0.25 * 15_000 / 1.0005 * 1, rel=1e-3)


def test_margin_needs_the_minimum_equity() -> None:
    config = _margin(DIP)  # margin_min_equity 2,000
    broker = SimBroker(FLAT, config, cash=1_000.0, start=T0)
    assert broker.get_account().buying_power == pytest.approx(1_000)


def test_negative_cash_pays_margin_interest_per_calendar_day() -> None:
    config = _margin(DIP)
    broker = SimBroker(FLAT, config, cash=10_000.0, start=T0)
    broker.set_borrow_rate(0.04)
    broker.submit_order("SPY", "buy", 15_000.0)
    broker.step(T1)
    borrowed = -broker.get_account().cash
    broker.step(T2)  # 4 calendar days
    expected = borrowed + margin_interest(borrowed, 0.04, 4, config)
    assert -broker.get_account().cash == pytest.approx(expected)


def test_margin_call_sells_the_largest_position_at_the_close() -> None:
    config = _margin(DIP)
    crash = {
        "SPY": _bars([(100, 100, 100, 100), (100, 100, 100, 100), (50, 50, 50, 50)]),
        "IBIT": _bars([(10, 10, 10, 10)] * 3),
    }
    broker = SimBroker(crash, config, cash=10_000.0, start=T0)
    broker.submit_order("SPY", "buy", 18_000.0)
    broker.submit_order("IBIT", "buy", 1_000.0)
    broker.step(T1)
    handled = broker.step(T2)  # SPY halves: equity about 1,000 < 25% x 9,000
    forced = [o for o in handled if o.reason == MARGIN_CALL_REASON]
    assert forced and forced[0].symbol == "SPY"
    assert broker.margin_calls and broker.margin_calls[0][1] == "SPY"
    account = broker.get_account()
    assert account.cash >= 0 or account.equity >= account.margin_requirement


def test_leveraged_etfs_carry_their_multiple_in_the_requirement() -> None:
    config = _margin(DIP)
    leveraged = next(iter(config.leveraged_etfs["SPY"].items()))
    multiple, etf = leveraged
    broker = SimBroker({etf: _bars([(50, 50, 50, 50)] * 3)}, config, cash=10_000.0, start=T0)
    broker.submit_order(etf, "buy", 4_000.0)
    broker.step(T1)
    held = broker.get_positions()[etf].quantity * 50
    assert broker.get_account().margin_requirement == pytest.approx(0.25 * multiple * held)


def test_snapshot_keeps_the_borrow_rate() -> None:
    config = _margin(DIP)
    broker = SimBroker(FLAT, config, cash=10_000.0, start=T0)
    broker.set_borrow_rate(0.05)
    copy = SimBroker(FLAT, config, cash=0.0, start=T0)
    copy.restore(broker.snapshot())
    assert copy.snapshot()["borrow_rate"] == pytest.approx(0.05)


def test_borrow_rate_must_be_non_negative() -> None:
    with pytest.raises(ValueError, match="borrow rate"):
        SimBroker(FLAT, DIP, cash=1.0, start=T0).set_borrow_rate(-0.01)


# Leverage of the allocation.


def test_borrow_leverage() -> None:
    assert borrow_leverage(DIP, 10_000) == 1.0
    assert borrow_leverage(KELLY, 10_000) == KELLY.risk.sim_broker_leverage
    assert borrow_leverage(KELLY, 100) == 1.0  # below margin_min_equity


def _closes(drift: float, vol: float, days: int = 900, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=days)
    returns = rng.normal(drift, vol, size=(days, 2))
    return pd.DataFrame(100 * np.cumprod(1 + returns, axis=0), index=index, columns=["SPY", "IBIT"])


def test_kelly_level_is_capped_by_the_section_7_limits() -> None:
    closes = _closes(0.002, 0.005)  # a very high Sharpe: Kelly far above the caps
    level = kelly_level(KELLY, closes, {"SPY": 0.8, "IBIT": 0.2}, 0.0, equity=10_000, drawdown=0)
    caps = KELLY.risk
    assert 1.0 < level <= min(caps.max_leverage, 2.0 * caps.max_etf_portfolio_leverage)


def test_kelly_level_falls_back_without_history_or_weights() -> None:
    short = _closes(0.001, 0.01, days=100)
    assert kelly_level(KELLY, short, {}, 0.0, equity=10_000, drawdown=0) == 1.0
    level = kelly_level(KELLY, short, {"SPY": 1.0}, 0.0, equity=10_000, drawdown=0)
    assert level <= 1.0 + 1e-9  # Kelly term 1, vol target may cut it


def test_kelly_level_halves_in_a_deep_drawdown() -> None:
    closes = _closes(0.002, 0.005)
    weights = {"SPY": 0.8, "IBIT": 0.2}
    calm = kelly_level(KELLY, closes, weights, 0.0, equity=10_000, drawdown=0.0)
    deep = kelly_level(KELLY, closes, weights, 0.0, equity=10_000, drawdown=0.3)
    assert deep == pytest.approx(calm / 2)


def test_kelly_level_reads_no_future_rows() -> None:
    closes = _closes(0.001, 0.01)
    cut = closes.iloc[:700]
    changed = closes.copy()
    changed.iloc[700:] *= 3.0
    weights = {"SPY": 0.5, "IBIT": 0.5}
    level = kelly_level(KELLY, cut, weights, 0.01, equity=10_000, drawdown=0)
    again = kelly_level(KELLY, changed.iloc[:700], weights, 0.01, equity=10_000, drawdown=0)
    assert level == again


# First start of a new instance (found 2026-11-17: an empty database made the
# morning phase fail on every tick before the instance's first evening).


@pytest.mark.parametrize(("hour", "minute"), [(9, 20), (12, 10), (16, 10)])
def test_new_instance_waits_for_its_first_evening(tmp_path: Path, hour: int, minute: int) -> None:
    from datetime import datetime  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from stride.data import store  # noqa: PLC0415

    (tmp_path / "config.paper_kelly.yaml").write_text(
        (ROOT / "config.paper_kelly.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    instance = run_daily.load_instance(tmp_path / "config.paper_kelly.yaml")
    alerts: list[Any] = []

    class Alerts:
        def send(self, alert: Any) -> None:
            alerts.append(alert)

    now = datetime(2026, 11, 17, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    deps = run_daily.Deps(now=lambda: now, alerter=Alerts(), git_hash=lambda: "abc1234")  # type: ignore[arg-type]
    from stride.settings import Secrets  # noqa: PLC0415

    secrets = Secrets(_env_file=None)  # type: ignore[call-arg]
    assert run_daily.run_phase(instance, "auto", deps, secrets=secrets) == 0
    assert not alerts
    conn = store.connect(instance.db)
    runs = store.read_runs(conn)
    conn.close()
    assert list(runs["status"]) == ["ok"]
    assert run_daily.NOT_STARTED in next(iter(runs["detail"]))

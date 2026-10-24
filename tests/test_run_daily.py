from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import SecretStr

from stride import run_daily
from stride.config import StrideConfig, load_config
from stride.data import store
from stride.data.calendar import next_trading_day, trading_days
from stride.data.update import UpdateResult
from stride.execution.broker import Broker
from stride.execution.sim_broker import SimBroker
from stride.reporting import daily
from stride.reporting.alerts import Alert
from stride.settings import Secrets

ROOT = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")
COSTA_RICA = ZoneInfo("America/Costa_Rica")
DAY1 = date(2026, 10, 22)  # Thursday
DAY2 = date(2026, 10, 23)  # Friday
DAY3 = date(2026, 10, 26)  # Monday
SYMBOLS = ("SPY", "IBIT", "ETHA")
FAKE_SECRET = "PKFAKESECRETVALUE123"


def _secrets(**values: SecretStr) -> Secrets:
    """No `.env`: only the values given."""
    return Secrets(_env_file=None, **values)  # type: ignore[call-arg]


def _write_config(folder: Path, name: str = "config.paper_test.yaml", **changes: object) -> Path:
    raw = load_config(ROOT / "config.yaml", env={}).model_dump()
    raw["mode"] = "paper"
    raw["data"]["db_path"] = f"data/{name.removesuffix('.yaml')}.db"
    raw["experiments"]["enabled"] = ["allocation", "drawdown_scaling"]
    raw["experiments"]["allocation"].update(
        mode="sleeve", core=["SPY"], assets=["IBIT", "ETHA"], refuge="cash", sleeve=0.2,
        signal="dip", rebalance="daily", kelly_fraction=0.0,
    )  # fmt: skip
    raw.update(changes)
    path = folder / name
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _bars(days: pd.DatetimeIndex, seed: int, drift: float, vol: float) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(drift, vol, len(days)))
    return pd.DataFrame(
        {"open": close * 0.999, "high": close * 1.01, "low": close * 0.99, "close": close},
        index=days,
    )


ALL_DAYS = trading_days(date(2024, 6, 3), DAY3)
BARS = {
    "SPY": _bars(ALL_DAYS, 1, 0.0005, 0.01),
    "IBIT": _bars(ALL_DAYS, 2, 0.001, 0.04),
    "ETHA": _bars(ALL_DAYS, 3, 0.001, 0.05),
}


def _rows(symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["symbol"], out["volume"], out["source"] = symbol, 1000, "test"
    out["fetched_at"] = pd.Timestamp("2026-10-01", tz="UTC")
    out.index.name = "date"
    return out


def _store_until(conn: sqlite3.Connection, last: date) -> None:
    for symbol, frame in BARS.items():
        store.upsert_prices(conn, _rows(symbol, frame.loc[: pd.Timestamp(last)]))


@dataclass
class Recorder:
    alerts: list[Alert] = field(default_factory=list)
    documents: list[tuple[str, str]] = field(default_factory=list)

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def send_document(self, filename: str, content: bytes, caption: str) -> None:
        self.documents.append((filename, caption))


@dataclass
class World:
    """A fake clock, a price feed that may lack today's bar, one SimBroker."""

    instance: run_daily.Instance
    clock: datetime
    sim: SimBroker
    recorder: Recorder = field(default_factory=Recorder)
    publish: bool = True  # whether the feed has today's bar
    downloads: int = 0
    broker_error: Exception | None = None

    def deps(self) -> run_daily.Deps:
        def update(
            conn: sqlite3.Connection, config: StrideConfig, day: date, secrets: Secrets,
            only_missing: bool,
        ) -> list[UpdateResult]:  # fmt: skip
            self.downloads += 1
            _store_until(conn, day if self.publish else day - timedelta(days=1))
            return [UpdateResult(s) for s in SYMBOLS]

        def broker(config: StrideConfig, secrets: Secrets, conn: sqlite3.Connection) -> Broker:
            if self.broker_error is not None:
                raise self.broker_error
            return self.sim

        return run_daily.Deps(
            now=lambda: self.clock,
            broker=broker,
            update_prices=update,
            risk_free=lambda: pd.Series(0.04, index=pd.date_range("2006-01-01", "2026-12-31")),
            alerter=self.recorder,
            git_hash=lambda: "abc1234",
            facts=dict,
        )

    def at(self, day: date, hour: int, minute: int = 0) -> None:
        self.clock = datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)

    def run(self, phase: str = "auto", secrets: Secrets | None = None) -> int:
        return run_daily.run_phase(self.instance, phase, self.deps(), secrets=secrets or _secrets())

    def conn(self) -> sqlite3.Connection:
        return store.connect(self.instance.db)


def _world(tmp_path: Path) -> World:
    path = _write_config(tmp_path)
    instance = run_daily.load_instance(path)
    conn = store.connect(instance.db)
    _store_until(conn, DAY1 - timedelta(days=1))
    conn.close()
    sim = SimBroker(BARS, instance.config, cash=10_000.0, start=DAY1)
    return World(instance, datetime(2026, 10, 22, 18, 0, tzinfo=NY), sim)


# Which phase is due.


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        # Costa Rica is UTC-6 all year; New York is UTC-4 in summer, UTC-5 in winter.
        (datetime(2026, 7, 15, 16, 30, tzinfo=COSTA_RICA), "evening"),  # 18:30 EDT
        (datetime(2026, 1, 15, 17, 30, tzinfo=COSTA_RICA), "evening"),  # 18:30 EST
        (datetime(2026, 1, 15, 16, 30, tzinfo=COSTA_RICA), None),  # 17:30 EST
        (datetime(2026, 7, 15, 7, 20, tzinfo=COSTA_RICA), "morning"),  # 09:20 EDT
        (datetime(2026, 1, 15, 8, 20, tzinfo=COSTA_RICA), "morning"),  # 09:20 EST
        (datetime(2026, 1, 15, 7, 20, tzinfo=COSTA_RICA), None),  # 08:20 EST
        (datetime(2026, 7, 15, 16, 30, tzinfo=UTC), "midday"),  # 12:30 EDT
        (datetime(2026, 1, 15, 21, 10, tzinfo=UTC), "close"),  # 16:10 EST
        (datetime(2026, 1, 15, 21, 0, tzinfo=UTC), None),  # 16:00 EST: not yet
    ],
)
def test_auto_picks_the_phase_in_summer_and_winter(local: datetime, expected: str | None) -> None:
    due = run_daily.due_phase(local)
    assert (due[1] if due else None) == expected


def test_nothing_due_on_holidays_and_weekends() -> None:
    assert run_daily.due_phase(datetime(2026, 11, 26, 18, 30, tzinfo=NY)) is None  # Thanksgiving
    assert run_daily.due_phase(datetime(2026, 10, 24, 18, 30, tzinfo=NY)) is None  # Saturday


def test_manual_evening_before_18_works_on_the_previous_session() -> None:
    assert run_daily.phase_day("evening", datetime(2026, 10, 23, 14, 0, tzinfo=NY)) == DAY1
    assert run_daily.phase_day("evening", datetime(2026, 10, 23, 18, 5, tzinfo=NY)) == DAY2
    with pytest.raises(run_daily.RunError, match="closed"):
        run_daily.phase_day("morning", datetime(2026, 10, 24, 9, 20, tzinfo=NY))


def test_instance_names_and_files(tmp_path: Path) -> None:
    assert run_daily.instance_name(Path("config.paper_b.yaml")) == "B"
    assert run_daily.instance_name(Path("config.paper_dip.yaml")) == "DIP"
    assert run_daily.instance_name(Path("config.yaml")) == "STRIDE"
    world = _world(tmp_path)
    assert world.instance.title == "STRIDE TEST"
    assert world.instance.log_file == tmp_path / "data" / "logs" / "test.log"
    assert world.instance.kill_switch == tmp_path / "KILL_SWITCH_TEST"
    assert world.instance.halt == tmp_path / "HALT_TEST"
    assert world.instance.kill_switch_files == (
        tmp_path / "KILL_SWITCH_TEST",
        tmp_path / "KILL_SWITCH",
    )


def test_download_plan_is_only_what_the_instance_trades(tmp_path: Path) -> None:
    config = _world(tmp_path).instance.config
    plan = {p.symbol for p in run_daily.download_plan(config)}
    assert plan == {"SPY", "SSO", "UPRO", "IBIT", "BITX", "ETHA", "ETHU", "BTC-USD", "ETH-USD"}


def test_scrub_removes_every_secret_value() -> None:
    secrets = _secrets(alpaca_secret_key=SecretStr(FAKE_SECRET))
    assert run_daily.scrub(f"boom {FAKE_SECRET} boom", secrets) == "boom *** boom"


# A full trading day, phase by phase.


def test_every_phase_runs_end_to_end(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.at(DAY1, 18, 0)
    assert world.run() == 0
    conn = world.conn()
    planned = store.read_orders(conn, statuses=("planned",), day=DAY1)
    assert not planned.empty
    assert set(planned["symbol"]) <= {"SPY", "IBIT", "ETHA"}
    assert planned["client_id"].str.startswith("stride-20261022-").all()
    assert list(store.read_runs(conn)["status"]) == ["ok"]
    assert len(store.read_equity(conn)) == 1
    assert not store.read_table(conn, "signals").empty
    assert world.recorder.documents[0][1].startswith("STRIDE TEST [paper] 2026-10-22")

    world.at(DAY2, 9, 20)  # morning: send last evening's orders
    assert world.run() == 0
    sent = store.read_orders(conn, statuses=("pending",))
    assert len(sent) == len(planned)
    world.sim.step(DAY2)  # the open of DAY2 fills them

    world.at(DAY2, 12, 15)  # midday: statuses and fills
    assert world.run() == 0
    assert len(store.read_table(conn, "fills")) == len(planned)
    assert set(store.read_orders(conn)["status"]) == {"filled"}

    world.at(DAY2, 16, 10)  # close: cancel, reconcile
    assert world.run() == 0
    assert store.read_events(conn, run_daily.RECONCILIATION_BREAK) == []
    last = store.last_positions(conn)
    assert last is not None and set(last[1]) == set(world.sim.get_positions())

    world.at(DAY2, 18, 0)  # the next evening
    assert world.run() == 0
    phases = list(store.read_runs(conn)["phase"])
    assert phases == ["evening", "morning", "midday", "close", "evening"]
    assert len(store.read_equity(conn)) == 2
    assert [d[0] for d in world.recorder.documents] == [
        "test-2026-10-22.html",
        "test-2026-10-23.html",
    ]
    conn.close()


def test_auto_is_idempotent_per_date_and_phase(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.at(DAY1, 18, 0)
    assert world.run() == 0
    world.at(DAY1, 18, 15)
    assert world.run() == 0
    world.at(DAY1, 18, 30)
    assert world.run("evening") == 0
    conn = world.conn()
    assert len(store.read_runs(conn)) == 1
    assert world.downloads == 1
    assert len(world.recorder.documents) == 1
    conn.close()


def test_auto_does_nothing_outside_the_windows_and_on_holidays(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.at(DAY1, 14, 0)
    assert world.run() == 0
    world.at(date(2026, 11, 26), 18, 30)  # Thanksgiving
    assert world.run() == 0
    conn = world.conn()
    assert store.read_runs(conn).empty and world.downloads == 0
    conn.close()


def test_missing_bar_is_retried_then_no_data(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.publish = False
    world.at(DAY1, 18, 0)
    assert world.run() == 0  # waiting: no runs row, no alert
    conn = world.conn()
    assert store.read_runs(conn).empty and world.recorder.alerts == []
    world.at(DAY1, 19, 45)  # the last tick before 20:00
    assert world.run() == 0
    runs = store.read_runs(conn)
    assert list(runs["status"]) == ["no_data"]
    assert [a.kind for a in world.recorder.alerts] == ["DATA_FAILED"]
    assert store.read_orders(conn).empty
    world.at(DAY1, 19, 50)
    assert world.run() == 0  # done for the day: not retried again
    assert len(store.read_runs(conn)) == 1
    conn.close()


def test_kill_switch_cancels_and_liquidates_from_broker_positions(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.at(DAY1, 18, 0)
    world.run()
    world.at(DAY2, 9, 20)
    world.run()
    world.sim.step(DAY2)
    held = world.sim.get_positions()
    assert held
    world.instance.kill_switch.write_text("fired by hand in a test", encoding="utf-8")
    world.at(DAY2, 18, 0)
    assert world.run() == 0
    conn = world.conn()
    assert store.read_orders(conn, statuses=("planned",), day=DAY2).empty  # nothing planned
    world.at(DAY3, 9, 20)
    assert world.run() == 0
    sells = store.read_orders(conn, day=DAY3)
    assert set(sells["rule"]) == {"KILL_SWITCH"}
    assert set(sells["side"]) == {"sell"}
    assert dict(zip(sells["symbol"], sells["quantity"], strict=True)) == {
        s: p.quantity for s, p in held.items()
    }
    world.sim.step(DAY3)
    assert world.sim.get_positions() == {}
    conn.close()


@pytest.mark.parametrize("shared", [False, True])
def test_halt_sends_nothing(tmp_path: Path, shared: bool) -> None:
    world = _world(tmp_path)
    world.at(DAY1, 18, 0)
    world.run()
    path = tmp_path / "HALT" if shared else world.instance.halt
    path.write_text("halted", encoding="utf-8")
    world.at(DAY2, 9, 20)
    assert world.run() == 0
    conn = world.conn()
    assert store.read_orders(conn, statuses=("pending", "sending")).empty
    assert set(store.read_orders(conn)["status"]) == {"vetoed"}
    conn.close()


def test_error_is_recorded_alerted_without_secrets_and_sends_nothing(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.broker_error = RuntimeError(f"connection refused for key {FAKE_SECRET}")
    world.at(DAY1, 18, 0)
    secrets = _secrets(alpaca_api_key=SecretStr(FAKE_SECRET))
    assert world.run(secrets=secrets) == 1
    conn = world.conn()
    [run] = store.read_runs(conn).to_dict("records")
    assert run["status"] == "error" and FAKE_SECRET not in run["detail"]
    [alert] = world.recorder.alerts
    assert alert.kind == "RUN_FAILED" and FAKE_SECRET not in alert.message
    assert "***" in alert.message
    assert store.read_orders(conn).empty
    log = world.instance.log_file
    assert not log.exists() or FAKE_SECRET not in log.read_text(encoding="utf-8")
    world.broker_error = None
    world.at(DAY1, 18, 15)  # an error is retried on the next tick
    assert world.run() == 0
    assert list(store.read_runs(conn)["status"]) == ["error", "ok"]
    conn.close()


def test_deposit_is_detected_and_not_a_gain(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.at(DAY1, 18, 0)
    world.run()
    world.sim.step(DAY2)
    world.sim.deposit(20.0)
    world.at(DAY2, 18, 0)
    world.run("evening")
    conn = world.conn()
    flows = store.net_cash_flows(conn)
    assert flows.to_dict() == {pd.Timestamp(DAY2): pytest.approx(20.0)}
    index, peak = store.load_twr(conn)
    assert index == pytest.approx(1.0) and peak == pytest.approx(1.0)  # nothing traded
    conn.close()


def test_reconciliation_break_is_an_event_and_an_alert(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.at(DAY1, 16, 10)
    world.run()  # first close: the snapshot is the broker's (empty)
    world.at(DAY1, 18, 0)
    world.run()
    world.at(DAY2, 9, 20)
    world.run()
    world.sim.step(DAY2)
    world.at(DAY2, 16, 10)
    conn = world.conn()
    with conn:  # the system loses track of the fills: it expects nothing
        conn.execute("DELETE FROM fills")
        conn.execute("UPDATE orders SET status = 'cancelled'")
    world.run()
    breaks = store.read_events(conn, run_daily.RECONCILIATION_BREAK)
    assert breaks and set(breaks[0][2].split(",")) == set(world.sim.get_positions())
    assert "RECONCILIATION" in {a.kind for a in world.recorder.alerts}
    conn.close()


def test_refuses_any_mode_but_backtest_or_paper(tmp_path: Path) -> None:
    world = _world(tmp_path)
    live = run_daily.Instance(
        world.instance.config_path,
        world.instance.config.model_copy(update={"mode": "live"}),
        world.instance.name,
    )
    with pytest.raises(run_daily.RunError, match="refusing"):
        run_daily.run_phase(live, "auto", world.deps())
    backtest = run_daily.Instance(
        world.instance.config_path,
        world.instance.config.model_copy(update={"mode": "backtest"}),
        world.instance.name,
    )
    with pytest.raises(run_daily.RunError, match="need mode: paper"):
        run_daily.run_phase(backtest, "evening", world.deps())


def test_mode_backtest_ends_with_exit_code_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    conn = world.conn()
    _store_until(conn, DAY2)
    conn.close()
    path = str(world.instance.config_path)
    assert run_daily.main(["--config", path, "--mode", "backtest"], deps=world.deps()) == 0
    out = capsys.readouterr().out
    assert "STRIDE TEST backtest" in out and "ok" in out
    conn = world.conn()
    assert store.read_runs(conn).empty and store.read_equity(conn).empty  # writes nothing
    conn.close()


def test_main_needs_a_phase_or_the_backtest_mode(tmp_path: Path) -> None:
    path = str(_world(tmp_path).instance.config_path)
    with pytest.raises(SystemExit):
        run_daily.main(["--config", path])


def test_next_trading_day_helper_is_consistent() -> None:
    assert next_trading_day(DAY2) == DAY3


def test_runner_alias_exists() -> None:
    assert run_daily.Runner is run_daily.DailyJob


def test_phase4_verdict_not_emitted_under_40_days(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.at(DAY1, 18, 0)
    assert world.run("evening") == 0
    conn = world.conn()
    assert not store.read_events(conn, daily.PHASE4_VERDICT_EVENT)
    assert not any(a.kind == "PHASE4_VERDICT" for a in world.recorder.alerts)
    conn.close()


def test_phase4_verdict_emitted_at_40_days_and_idempotent(tmp_path: Path) -> None:
    world = _world(tmp_path)
    conn = world.conn()
    all_prior = list(trading_days(date(2026, 1, 1), DAY1 - timedelta(days=1)))
    prior_39 = all_prior[-39:]

    eq_rows = [
        {
            "date": pd.Timestamp(d),
            "equity": 10_000.0 * (1.005**i),
            "cash": 10_000.0,
            "gross_exposure": 0.0,
            "leverage": 0.0,
        }
        for i, d in enumerate(prior_39)
    ]
    store.upsert_equity(conn, pd.DataFrame(eq_rows).set_index("date"))
    conn.close()

    # Day 40 evening (DAY1)
    world.at(DAY1, 18, 0)
    assert world.run("evening") == 0

    conn = world.conn()
    events = store.read_events(conn, daily.PHASE4_VERDICT_EVENT)
    assert len(events) == 1
    verdict_alerts = [a for a in world.recorder.alerts if a.kind == "PHASE4_VERDICT"]
    assert len(verdict_alerts) == 1
    assert "40 días hábiles" in verdict_alerts[0].message
    conn.close()

    # Day 41 evening (DAY2): does not duplicate
    world.at(DAY2, 18, 0)
    assert world.run("evening") == 0

    conn = world.conn()
    events2 = store.read_events(conn, daily.PHASE4_VERDICT_EVENT)
    assert len(events2) == 1
    verdict_alerts2 = [a for a in world.recorder.alerts if a.kind == "PHASE4_VERDICT"]
    assert len(verdict_alerts2) == 1
    conn.close()

"""The risk-allocation paper instance: its config file and the daily job (offline)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from stride.data.calendar import trading_days
from stride.data.sources import SourceError, to_contract
from stride.reporting.alerts import Alert
from stride.risk_paper.config import RiskPaperConfigError, load_risk_config
from stride.risk_paper.job import Deps, RiskJob, session_seed
from stride.run_risk import main

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ["QQQ", "IWM", "GLD", "TLT"]
CALENDAR = trading_days(date(2025, 1, 2), date(2026, 6, 30))
DAYS = [d.date() for d in CALENDAR if d >= pd.Timestamp("2026-03-16")]  # sessions the tests tick on


def evening(day: date) -> datetime:
    """23:30 UTC: 19:30 in New York (daylight time), after the daily bar is due."""
    return datetime(day.year, day.month, day.day, 23, 30, tzinfo=UTC)


# --- the config file ----------------------------------------------------------------


def _real_config() -> dict[str, Any]:
    text = (ROOT / "config.paper_risk.yaml").read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_the_shipped_config_is_the_measured_variant_and_paper_only() -> None:
    config = load_risk_config(ROOT / "config.paper_risk.yaml")
    assert isinstance(config.enabled, bool)  # the owner's switch; its value is not pinned
    assert config.mode == "paper"
    assert config.capital_usd == 10_000
    assert config.kill_switch_dd == 0.20
    approved = "QQQ,IWM,XLE,XLV,XLU,EFA,FXI,INDA,EWZ,TLT,GLD,SLV,CPER,USO,UNG,DBA,VNQ,UUP,IBIT,ETHA"
    assert config.universe == approved.split(",")
    rule = config.rule()
    assert (rule.name, rule.allocator, rule.brake, rule.trend_days) == (
        "IV_trend",
        "IV",
        "trend",
        200,
    )
    limits = rule.limits
    assert (limits.max_weight, limits.max_total, limits.min_order_usd) == (0.25, 1.0, 150.0)
    assert (limits.risk_horizon, limits.risk_quantile, limits.max_portfolio_loss) == (5, 0.01, 0.08)
    assert limits.group_caps == {"us_equity": 0.40, "metals": 0.35, "crypto": 0.30}
    params = config.risk_params()
    assert (params.window_days, params.vol_span_days, params.n_paths) == (252, 60, 2000)
    assert params.horizons == (5, 20)
    assert config.simulation.seed == 20261112


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(mode="live"), "mode"),
        (lambda d: d["allocation"].update(allocator="KELLY"), "allocator"),
        (lambda d: d["groups"].update(extra=["NOPE"]), "not in the universe"),
        (lambda d: d["groups"].update(crypto2=["QQQ"]), "more than one group"),
        (lambda d: d["group_caps"].update(ghost=0.5), "group that does not exist"),
        (lambda d: d["universe"].append("QQQ"), "duplicate"),
        (lambda d: d.update(history_days=100), "cover the simulation window"),
        (lambda d: d.update(surprise=1), "surprise"),
        (lambda d: d.update(kill_switch_dd=1.5), "kill_switch_dd"),
    ],
)
def test_bad_configs_are_refused(
    tmp_path: Path, change: Callable[[dict[str, Any]], object], message: str
) -> None:
    data = _real_config()
    change(data)
    path = tmp_path / "config.paper_risk.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RiskPaperConfigError, match=message):
        load_risk_config(path)


def test_a_missing_config_is_reported() -> None:
    with pytest.raises(RiskPaperConfigError):
        load_risk_config(ROOT / "no_such_file.yaml")
    assert main(["--config", str(ROOT / "no_such_file.yaml")]) == 2


def test_session_seed_depends_on_the_date_only() -> None:
    day = pd.Timestamp("2026-03-16")
    assert session_seed(7, day) == session_seed(7, pd.Timestamp("2026-03-16"))
    assert session_seed(7, day) != session_seed(7, pd.Timestamp("2026-03-17"))
    assert session_seed(7, day) != session_seed(8, day)


# --- the job ------------------------------------------------------------------------


class FakeSource:
    """Daily bars that are a total-return series times a scale.

    A dividend of `dividend` paid by `payer` on `ex_day` shows the way a provider of
    adjusted prices shows it: every query made for a day on or after `ex_day` returns
    the whole history multiplied by `1 - dividend`.
    """

    name = "fake"

    def __init__(
        self,
        *,
        payer: str | None = None,
        ex_day: date | None = None,
        dividend: float = 0.0,
        spike: tuple[str, date] | None = None,
    ) -> None:
        self.payer, self.ex_day, self.dividend = payer, ex_day, dividend
        self.failing: set[str] = set()
        self.calls = 0
        self.missing: dict[str, date] = {}  # symbol -> a day whose bar is not published
        self.frames: dict[str, pd.DataFrame] = {}
        for number, symbol in enumerate(UNIVERSE):
            rng = np.random.default_rng(number)
            steps = rng.normal(0.0025 if symbol != "TLT" else -0.003, 0.004, size=len(CALENDAR))
            if spike is not None and spike[0] == symbol:
                steps[CALENDAR.get_indexer(pd.DatetimeIndex([spike[1]]))[0]] += 0.6
            close = 100.0 * np.exp(np.cumsum(steps))
            opened = np.r_[close[0], close[:-1]] * (1.0 + rng.normal(0.0, 0.001, size=len(close)))
            self.frames[symbol] = pd.DataFrame(
                {"date": CALENDAR, "open": opened, "close": close, "volume": 1000}
            )

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        self.calls += 1
        if symbol in self.failing:
            raise SourceError(f"fake source down for {symbol}")
        frame = self.frames[symbol]
        inside = (frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))
        bars = frame.loc[inside].copy()
        if symbol in self.missing:
            bars = bars[bars["date"] != pd.Timestamp(self.missing[symbol])]
        scale = 1.0
        if symbol == self.payer and self.ex_day is not None and end >= self.ex_day:
            scale = 1.0 - self.dividend
        bars["open"] *= scale
        bars["close"] *= scale
        bars["high"] = bars[["open", "close"]].max(axis=1)
        bars["low"] = bars[["open", "close"]].min(axis=1)
        return to_contract(bars, symbol, self.name, datetime(2026, 1, 1, tzinfo=UTC))


class Recorder:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def send_document(self, filename: str, content: bytes, caption: str) -> None:
        raise AssertionError("the job sends no documents")


def _write_config(root: Path, **overrides: Any) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "config.yaml", root / "config.yaml")
    data = _real_config()
    data.update(
        enabled=True,
        universe=UNIVERSE,
        groups={"us_equity": ["QQQ", "IWM"]},
        group_caps={"us_equity": 0.80},
        history_days=220,
    )
    data["allocation"].update(trend_days=50)
    data["simulation"].update(window_days=60, n_paths=300)
    data["limits"].update(max_weight=0.40)
    data.update(overrides)
    path = root / "config.paper_risk.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class Rig:
    """A job in a temporary folder with a fake clock, source and alert recorder."""

    def __init__(self, root: Path, source: FakeSource | None = None, **overrides: Any) -> None:
        self.root = root
        self.path = _write_config(root, **overrides)
        self.source = source or FakeSource()
        self.recorder = Recorder()
        self.now = evening(DAYS[0])
        self.lines: list[str] = []

    def tick(self, day: date | None = None, moment: datetime | None = None) -> int:
        if day is not None:
            self.now = evening(day)
        if moment is not None:
            self.now = moment
        deps = Deps(
            now=lambda: self.now,
            source=lambda root: self.source,
            alerter=lambda root: self.recorder,
            git_hash=lambda: "test",
        )
        return RiskJob(self.path, deps).run(self.lines.append)

    def job(self) -> RiskJob:
        deps = Deps(now=lambda: self.now, source=lambda root: self.source)
        return RiskJob(self.path, deps)

    def conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.root / "data" / "paper_risk.db")

    def state(self, key: str) -> Any:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT value FROM risk_paper_state WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else row[0]

    def equity(self) -> pd.Series:
        with self.conn() as conn:
            frame = pd.read_sql_query("SELECT day, equity FROM risk_equity ORDER BY day", conn)
        return frame.set_index("day")["equity"]

    def trades(self) -> pd.DataFrame:
        with self.conn() as conn:
            return pd.read_sql_query(
                "SELECT day, symbol, side, notional, price, reason FROM risk_trades ORDER BY id",
                conn,
            )

    def statuses(self) -> list[str]:
        with self.conn() as conn:
            return [r[0] for r in conn.execute("SELECT status FROM runs ORDER BY id")]


def test_disabled_is_a_no_op(tmp_path: Path) -> None:
    rig = Rig(tmp_path, enabled=False)
    assert rig.tick() == 0
    assert "disabled" in rig.lines[-1]
    assert not (tmp_path / "data").exists()  # not even a database


def test_first_ticks_start_the_account_and_fill_at_the_next_open(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.tick(DAYS[0]) == 0
    assert rig.state("first_day") == DAYS[0].isoformat()
    assert rig.state("last_day") == DAYS[0].isoformat()
    assert rig.equity().tolist() == [10_000.0]  # decided at the close, nothing filled yet
    assert rig.trades().empty
    assert rig.tick(DAYS[0]) == 0
    assert rig.lines[-1].endswith("up to date")
    # The next morning the bar of the new session is not due yet: still up to date.
    morning = datetime(DAYS[1].year, DAYS[1].month, DAYS[1].day, 15, 0, tzinfo=UTC)
    assert rig.tick(moment=morning) == 0
    assert rig.lines[-1].endswith("up to date")
    assert rig.tick(DAYS[1]) == 0
    trades = rig.trades()
    assert set(trades["day"]) == {DAYS[1].isoformat()}
    assert set(trades["reason"]) == {"RISK_ENTER"}
    assert "TLT" not in set(trades["symbol"])  # falling: below its average, left in cash
    for trade in trades.itertuples():
        frame = rig.source.frames[str(trade.symbol)]
        on_day = (frame["date"] == pd.Timestamp(DAYS[1])).to_numpy()
        opened = float(frame["open"].to_numpy()[on_day][0])
        assert trade.price == pytest.approx(opened * 1.0005)  # the open plus 5 bps of spread
    assert rig.statuses() == ["ok", "ok"]
    info = [a for a in rig.recorder.alerts if a.kind == "INFO"]
    assert len(info) == 2
    assert info[-1].message.startswith("RISK: close " + DAYS[1].isoformat())
    assert "Weights:" in info[-1].message


def test_a_job_that_missed_days_ends_in_the_same_account(tmp_path: Path) -> None:
    daily, lazy = Rig(tmp_path / "a"), Rig(tmp_path / "b")
    for day in DAYS[:30]:
        assert daily.tick(day) == 0
    assert lazy.tick(DAYS[0]) == 0
    assert lazy.tick(DAYS[12]) == 0  # eleven sessions replayed in one tick
    assert lazy.tick(DAYS[29]) == 0
    pd.testing.assert_series_equal(daily.equity(), lazy.equity())
    pd.testing.assert_frame_equal(daily.trades(), lazy.trades())
    assert json.loads(daily.state("trader")) == json.loads(lazy.state("trader"))
    months = {day.month for day in DAYS[:30]}
    assert len(months) >= 2  # the run crosses a monthly rebalance
    with daily.conn() as conn:
        rebalances = conn.execute("SELECT COUNT(*) FROM risk_log WHERE rebalance = 1").fetchone()[0]
    assert rebalances == len(months)


def test_verify_replays_the_account_and_report_prints_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    for day in DAYS[:8]:
        rig.tick(day)
    lines: list[str] = []
    assert rig.job().verify(lines.append) == 0
    assert "replay of 8 session(s)" in lines[-1]
    assert "0.000000%" in lines[-1]  # no dividend: the replay is exact
    # A recorded equity that the rule cannot reproduce is reported.
    with rig.conn() as conn:
        conn.execute(
            "UPDATE risk_equity SET equity = equity * 1.01 WHERE day = ?", (DAYS[5].isoformat(),)
        )
    assert rig.job().verify(lines.append) == 1
    assert "MISMATCH" in lines[-1]
    assert rig.job().report(lines.append) == 0
    assert any(line.startswith("  fill ") for line in lines)
    assert main(["--config", str(rig.path), "--report"]) == 0


def test_report_and_verify_without_an_account(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    lines: list[str] = []
    assert rig.job().report(lines.append) == 1
    assert rig.job().verify(lines.append) == 1


def test_a_dividend_is_reinvested_not_lost(tmp_path: Path) -> None:
    """The provider rescales the past at a dividend; the ledger must follow the
    total-return series, as if no dividend had been paid out."""
    ex_day = DAYS[6]
    plain = Rig(tmp_path / "plain")
    paying = Rig(tmp_path / "paying", FakeSource(payer="QQQ", ex_day=ex_day, dividend=0.03))
    for day in DAYS[:12]:
        assert plain.tick(day) == 0
        assert paying.tick(day) == 0
    with paying.conn() as conn:
        events = [
            r[0] for r in conn.execute("SELECT detail FROM events WHERE type = 'DIVIDEND_ADJUST'")
        ]
    assert len(events) == 1
    assert events[0].startswith("QQQ: history revised, shares multiplied by 1.03092")
    with plain.conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE type = 'DIVIDEND_ADJUST'").fetchone()[0]
            == 0
        )
    a, b = plain.equity(), paying.equity()
    assert b.to_numpy() == pytest.approx(a.to_numpy(), rel=2e-4)  # commissions differ by cents
    # Without the rule the account would have dropped by the dividend times the QQQ weight.
    weights = json.loads(
        paying.conn()
        .execute("SELECT weights FROM risk_log WHERE day = ?", (ex_day.isoformat(),))
        .fetchone()[0]
    )
    assert weights["QQQ"] * 0.03 > 2e-3
    shares = json.loads(paying.state("trader"))["trader"]["shares"]
    plain_shares = json.loads(plain.state("trader"))["trader"]["shares"]
    assert shares[0] == pytest.approx(plain_shares[0] / 0.97, rel=1e-3)
    # The from-scratch replay sees only the rescaled history and still agrees.
    lines: list[str] = []
    assert paying.job().verify(lines.append) == 0


def test_a_tick_waits_until_most_of_the_bars_are_published(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.tick(DAYS[0])
    for symbol in ("IWM", "GLD", "TLT"):
        rig.source.missing[symbol] = DAYS[1]
    assert rig.tick(DAYS[1]) == 0
    assert "no data: only 1 of 4 assets" in rig.lines[-1]
    assert rig.state("last_day") == DAYS[0].isoformat()
    assert rig.statuses()[-1] == "no_data"
    rig.source.missing.clear()
    assert rig.tick(moment=evening(DAYS[1]) + timedelta(minutes=31)) == 0
    assert rig.state("last_day") == DAYS[1].isoformat()
    assert len(rig.trades()) > 0


def test_waiting_for_the_bar_costs_one_request_and_is_not_repeated_every_tick(
    tmp_path: Path,
) -> None:
    """The data key is shared with B and DIP: a job that waits must not spend it."""
    rig = Rig(tmp_path)
    rig.tick(DAYS[0])
    rig.source.missing["QQQ"] = DAYS[1]  # the probe: the first symbol of the universe
    before = rig.source.calls
    assert rig.tick(DAYS[1]) == 0
    assert f"no data: the bar of {DAYS[1]} is not published yet" in rig.lines[-1]
    assert rig.source.calls == before + 1  # one request, not one per symbol
    for minutes in (5, 10, 25):
        assert rig.tick(moment=evening(DAYS[1]) + timedelta(minutes=minutes)) == 0
        assert rig.lines[-1].endswith("waiting to retry")
    assert rig.source.calls == before + 1  # the ticks in between ask for nothing
    assert rig.statuses().count("no_data") == 1
    rig.source.missing.clear()
    assert rig.tick(moment=evening(DAYS[1]) + timedelta(minutes=31)) == 0
    assert rig.state("last_day") == DAYS[1].isoformat()
    # An up-to-date job asks for nothing either.
    after = rig.source.calls
    assert rig.tick(moment=evening(DAYS[1]) + timedelta(minutes=40)) == 0
    assert rig.source.calls == after


def test_a_failed_tick_is_not_repeated_every_five_minutes(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.source.failing.add("IWM")
    assert rig.tick(DAYS[0]) == 2
    alerts = len(rig.recorder.alerts)
    assert rig.tick(moment=evening(DAYS[0]) + timedelta(minutes=5)) == 0
    assert rig.lines[-1].endswith("waiting to retry")
    assert len(rig.recorder.alerts) == alerts  # no second alert for the same failure


def test_bad_data_and_a_source_failure_stop_the_tick(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "spike", FakeSource(spike=("GLD", DAYS[1])))
    rig.tick(DAYS[0])
    assert rig.tick(DAYS[1]) == 2
    assert "GLD moved more than 40% in one day: bad data" in rig.lines[-1]
    assert rig.state("last_day") == DAYS[0].isoformat()
    assert rig.statuses()[-1] == "error"
    assert rig.recorder.alerts[-1].kind == "RUN_FAILED"

    down = Rig(tmp_path / "down")
    down.source.failing.add("IWM")
    assert down.tick(DAYS[0]) == 2
    assert down.recorder.alerts[-1].kind == "DATA_FAILED"
    assert down.state("trader") is None
    down.source.failing.clear()
    assert down.tick(moment=evening(DAYS[0]) + timedelta(minutes=31)) == 0  # a later tick recovers


def test_the_kill_switch_file_liquidates_and_stops(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    for day in DAYS[:3]:
        rig.tick(day)
    assert not rig.trades().empty
    (tmp_path / "KILL_SWITCH_RISK").write_text("stop", encoding="utf-8")
    rig.tick(DAYS[3])
    rig.tick(DAYS[4])
    trades = rig.trades()
    sold = trades[trades["reason"] == "KILL_SWITCH_FILE"]
    assert set(sold["day"]) == {DAYS[4].isoformat()}
    assert set(sold["side"]) == {"sell"}
    state = json.loads(rig.state("trader"))["trader"]
    assert state["stopped"] is True
    assert max(state["shares"]) == 0.0
    rig.tick(DAYS[5])
    assert len(rig.trades()) == len(trades)  # it does not trade again
    assert "STOPPED" in rig.recorder.alerts[-1].message


def test_the_drawdown_kill_switch_fires_and_writes_its_file(tmp_path: Path) -> None:
    rig = Rig(tmp_path, kill_switch_dd=0.001)  # any losing day reaches it
    for day in DAYS[:25]:
        rig.tick(day)
    assert (tmp_path / "KILL_SWITCH_RISK").exists()
    assert any(a.kind == "KILL_SWITCH" for a in rig.recorder.alerts)
    assert json.loads(rig.state("trader"))["trader"]["stopped"] is True
    assert not (tmp_path / "KILL_SWITCH").exists()  # the other instances are not stopped


def test_a_changed_universe_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.tick(DAYS[0])
    _write_config(tmp_path, universe=["QQQ", "IWM", "GLD"], groups={}, group_caps={})
    assert rig.tick(DAYS[1]) == 2
    assert "the universe changed" in rig.lines[-1]


def test_the_job_never_reads_a_later_bar(tmp_path: Path) -> None:
    """Two sources that differ only after a day give the same account up to that day."""
    one = Rig(tmp_path / "one")
    other = Rig(tmp_path / "other", FakeSource(spike=("GLD", DAYS[9])))
    for day in DAYS[:9]:
        one.tick(day)
        other.tick(day)
    pd.testing.assert_series_equal(one.equity(), other.equity())
    pd.testing.assert_frame_equal(one.trades(), other.trades())

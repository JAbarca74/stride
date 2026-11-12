"""The Monte Carlo paper instance: its config file and the hourly job (offline)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from stride import run_mc as run_mc_module
from stride.data.calendar import sessions
from stride.data.intraday import BAR
from stride.data.sources import SourceError
from stride.forecast.config import McConfigError, load_mc_config
from stride.forecast.walkforward import forecast_at as real_forecast_at
from stride.reporting.alerts import Alert
from stride.run_mc import Deps, McJob, closes_session, latest_ended_slot, main

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ["QQQ", "IWM", "GLD"]


def at(hour: int, minute: int = 0, day: int = 10) -> datetime:
    """A moment of November 2026 in UTC; New York is on standard time (open 14:30 UTC)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


# --- the config file ----------------------------------------------------------------


def _real_config() -> dict[str, Any]:
    text = (ROOT / "config.paper_mc.yaml").read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_the_shipped_config_is_valid_and_disabled() -> None:
    config = load_mc_config(ROOT / "config.paper_mc.yaml")
    assert config.enabled is False  # the calibration gate was not met
    assert config.mode == "paper"
    assert config.capital_usd == 1000
    assert config.kill_switch_dd == 0.20
    approved = "QQQ,IWM,XLE,XLV,XLU,EFA,FXI,INDA,EWZ,TLT,GLD,SLV,CPER,USO,UNG,DBA,VNQ,UUP,IBIT,ETHA"
    assert config.universe == approved.split(",")
    params = config.forecast_params()
    assert params.horizons == (1, 7, 35, 140)
    assert params.window_days == config.forecast.window_days
    rule = config.rule()
    assert rule.decision.horizon == config.decision.horizon
    assert rule.weights.group_caps == config.group_caps
    assert set(rule.weights.groups["crypto"]) == {"IBIT", "ETHA"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(mode="live"), "mode"),
        (lambda d: d["decision"].update(p_exit=0.70), "hysteresis"),
        (lambda d: d["decision"].update(horizon=10), "simulated horizons"),
        (lambda d: d["groups"].update(extra=["NOPE"]), "not in the universe"),
        (lambda d: d["groups"].update(crypto2=["QQQ"]), "more than one group"),
        (lambda d: d["group_caps"].update(ghost=0.5), "group that does not exist"),
        (lambda d: d["universe"].append("QQQ"), "duplicate"),
        (lambda d: d.update(history_days=100), "cover the forecast window"),
        (lambda d: d.update(surprise=1), "surprise"),
        (lambda d: d.update(kill_switch_dd=1.5), "kill_switch_dd"),
    ],
)
def test_bad_configs_are_refused(
    tmp_path: Path, change: Callable[[dict[str, Any]], object], message: str
) -> None:
    data = _real_config()
    change(data)
    path = tmp_path / "config.paper_mc.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(McConfigError, match=message):
        load_mc_config(path)


def test_a_missing_config_is_reported() -> None:
    with pytest.raises(McConfigError):
        load_mc_config(ROOT / "no_such_file.yaml")


# --- the clock ----------------------------------------------------------------------------


def test_latest_ended_slot_waits_for_the_data_delay() -> None:
    # The 10:00 New York slot ends at 15:00 UTC; its bars are published 16 minutes later.
    assert latest_ended_slot(at(15, 10)) == pd.Timestamp("2026-11-09T21:00:00Z")
    assert latest_ended_slot(at(15, 20)) == pd.Timestamp("2026-11-10T15:00:00Z")
    assert latest_ended_slot(at(21, 20)) == pd.Timestamp("2026-11-10T21:00:00Z")
    # Saturday: still the Friday close.
    saturday = datetime(2026, 11, 14, 15, tzinfo=UTC)
    assert latest_ended_slot(saturday) == pd.Timestamp("2026-11-13T21:00:00Z")


def test_closes_session_follows_early_closes() -> None:
    assert closes_session(pd.Timestamp("2026-11-10"), 6)
    assert not closes_session(pd.Timestamp("2026-11-10"), 5)
    assert closes_session(pd.Timestamp("2026-11-27"), 3)  # the day after Thanksgiving, 13:00 close
    assert not closes_session(pd.Timestamp("2026-11-26"), 3)  # a holiday: no session at all


# --- the job ------------------------------------------------------------------------------------


class FakeSource:
    """Trending 30-minute bars for every session between June and mid November."""

    name = "fake"

    def __init__(self, spike_at: pd.Timestamp | None = None) -> None:
        self.failing: set[str] = set()
        stamps: list[pd.Timestamp] = []
        for _, session in sessions(date(2026, 6, 1), date(2026, 11, 13)).iterrows():
            start = session["market_open"]
            while start + BAR <= session["market_close"]:
                stamps.append(start)
                start += BAR
        index = pd.DatetimeIndex(stamps)
        self.frames: dict[str, pd.DataFrame] = {}
        for number, symbol in enumerate(UNIVERSE):
            rng = np.random.default_rng(number)
            steps = rng.normal(0.0004, 0.0003, size=len(index))
            if spike_at is not None and symbol == "QQQ":
                steps[index.get_loc(spike_at)] += 0.7
            close = 100.0 * np.exp(np.cumsum(steps))
            opened = np.r_[close[0], close[:-1]]
            self.frames[symbol] = pd.DataFrame(
                {
                    "ts": index,
                    "open": opened,
                    "high": np.maximum(opened, close) * 1.0005,
                    "low": np.minimum(opened, close) * 0.9995,
                    "close": close,
                    "volume": 1000,
                }
            )

    def fetch_intraday(
        self, symbol: str, start: datetime, end: datetime, timeframe: str = "30Min"
    ) -> pd.DataFrame:
        if symbol in self.failing:
            raise SourceError(f"fake source down for {symbol}")
        frame = self.frames[symbol]
        inside = (frame["ts"] >= pd.Timestamp(start)) & (frame["ts"] <= pd.Timestamp(end))
        return frame.loc[inside].reset_index(drop=True)


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
        group_caps={"us_equity": 0.60},
        history_days=90,
        capital_usd=1000,
    )
    data["forecast"].update(window_days=20, n_paths=300, n_paths_risk=200, drift_shrink=1.0)
    data["decision"].update(horizon=35, p_enter=0.60, p_exit=0.50)
    data["weights"].update(kappa=0.5, max_weight=0.40, min_order_usd=100.0)
    data.update(overrides)
    path = root / "config.paper_mc.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class Rig:
    """A job in a temporary folder with a fake clock, source and alert recorder."""

    def __init__(
        self, root: Path, source: FakeSource | None = None, seconds_per_call: float = 0.0
    ) -> None:
        self.root = root
        self.elapsed = 0.0
        self.seconds_per_call = seconds_per_call
        self.source = source or FakeSource()
        self.recorder = Recorder()
        self.clock = [at(16, 30)]
        deps = Deps(
            now=lambda: self.clock[0],
            source=lambda _: self.source,
            alerter=lambda _: self.recorder,
            git_hash=lambda: "test",
            monotonic=self._monotonic,
        )
        self.job = McJob(_write_config(root), deps)

    def _monotonic(self) -> float:
        self.elapsed += self.seconds_per_call
        return self.elapsed

    def tick(self, when: datetime) -> tuple[int, str]:
        self.clock[0] = when
        lines: list[str] = []
        code = self.job.run(lines.append)
        return code, "\n".join(lines)

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.root / "data" / "paper_mc.db")

    def state(self) -> dict[str, Any]:
        with self.db() as conn:
            row = conn.execute("SELECT value FROM mc_state WHERE key = 'trader'").fetchone()
        loaded: dict[str, Any] = json.loads(row[0])
        return loaded


def test_disabled_instance_does_nothing(tmp_path: Path) -> None:
    path = _write_config(tmp_path, enabled=False)
    out: list[str] = []

    def no_source(_: Path) -> Any:
        raise AssertionError("no data must be read while disabled")

    assert McJob(path, Deps(source=no_source)).run(out.append) == 0
    assert "disabled" in out[0]
    assert not (tmp_path / "data").exists()


def test_the_job_never_runs_outside_paper_mode(tmp_path: Path) -> None:
    path = _write_config(tmp_path, mode="live")
    assert main(["--config", str(path)]) == 2


def test_first_tick_starts_the_account_and_later_ticks_replay_new_slots(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    code, text = rig.tick(at(16, 30))  # the 10:00-11:00 New York slot has just been published
    assert code == 0, text
    assert "1 slot(s) processed" in text
    state = rig.state()
    assert state["counter"] == 0
    assert state["cash"] == 1000.0  # nothing fills before the slot after the next one

    _, text = rig.tick(at(16, 35))
    assert "up to date" in text  # no new slot: no download, no work

    _, text = rig.tick(at(21, 30))  # the close: slots 2 to 6
    assert "5 slot(s) processed" in text
    with rig.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mc_equity").fetchone()[0] == 6
        last = conn.execute("SELECT MAX(slot_end) FROM mc_equity").fetchone()[0]
        statuses = {r[0] for r in conn.execute("SELECT status FROM runs")}
    assert last == "2026-11-10T21:00:00Z"
    assert statuses == {"ok"}
    summaries = [a for a in rig.recorder.alerts if a.kind == "INFO"]
    assert len(summaries) == 1  # one summary, at the close
    assert "equity" in summaries[0].message


def test_a_clear_uptrend_is_bought_with_costs_and_weights_from_the_forecast(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.tick(at(16, 30))
    rig.tick(at(21, 30))
    rig.tick(at(15, 30, day=11))  # the next morning
    with rig.db() as conn:
        trades = conn.execute(
            "SELECT symbol, side, notional, commission, reason FROM mc_trades"
        ).fetchall()
        equity = conn.execute(
            "SELECT equity, cash, exposure FROM mc_equity ORDER BY slot_end DESC"
        ).fetchone()
        forecasts = conn.execute(
            "SELECT COUNT(DISTINCT symbol), COUNT(DISTINCT horizon) FROM mc_forecasts"
        ).fetchone()
        logged = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'MC_TRADE'").fetchone()[0]
    assert trades, "a steady uptrend with these parameters must be bought"
    assert all(t[1] == "buy" and t[4] == "MC_ENTER" for t in trades)
    assert all(t[3] >= 0.35 for t in trades)  # the IBKR minimum per order
    bought = {t[0] for t in trades}
    assert bought <= set(UNIVERSE)
    assert all(t[2] <= 0.40 * 1000 * 1.05 for t in trades)  # the 40% cap per asset
    state = rig.state()
    held = {UNIVERSE[i] for i, shares in enumerate(state["shares"]) if shares > 0}
    assert held == bought
    value, cash, exposure = equity
    assert 0 < exposure <= 1.0
    assert value * (1 - exposure) == pytest.approx(cash)
    assert forecasts == (len(UNIVERSE), 4)
    assert logged == len(trades)


def test_an_outage_is_replayed_to_the_same_account_as_continuous_running(tmp_path: Path) -> None:
    source = FakeSource()
    continuous = Rig(tmp_path / "continuous", source)
    for hour in (16, 17, 18, 19, 20, 21):
        continuous.tick(at(hour, 30))
    continuous.tick(at(16, 30, day=11))

    gappy = Rig(tmp_path / "gappy", source)
    gappy.tick(at(16, 30))
    gappy.tick(at(16, 30, day=11))  # one tick after a day-long gap

    assert gappy.state() == continuous.state()
    last = "SELECT slot_end, equity FROM mc_equity ORDER BY slot_end DESC LIMIT 1"
    with gappy.db() as a, continuous.db() as b:
        assert a.execute(last).fetchone() == b.execute(last).fetchone()


def test_bad_data_stops_the_tick_and_keeps_the_state(tmp_path: Path) -> None:
    spike = pd.Timestamp("2026-11-10T15:30:00Z")  # the second bar of the slot 16:30 processes
    rig = Rig(tmp_path, FakeSource(spike_at=spike))
    code, text = rig.tick(at(16, 30))
    assert code == 2
    assert "bad data" in text
    with rig.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mc_state WHERE key = 'trader'").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "error"
    assert [a.kind for a in rig.recorder.alerts] == ["RUN_FAILED"]


def test_a_failing_source_is_reported_and_retried(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.source.failing = {"GLD"}
    code, text = rig.tick(at(16, 30))
    assert code == 2
    assert "GLD" in text
    assert [a.kind for a in rig.recorder.alerts] == ["DATA_FAILED"]
    rig.source.failing = set()
    code, text = rig.tick(at(16, 35))
    assert code == 0, text
    assert "1 slot(s) processed" in text


def test_a_slot_whose_last_bar_is_not_published_waits_for_the_next_tick(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    cut = pd.Timestamp("2026-11-10T15:30:00Z")  # the closing bar of the 16:00 UTC slot
    full = rig.source.frames
    rig.source.frames = {s: f[f["ts"] < cut] for s, f in full.items()}
    code, text = rig.tick(at(16, 30))
    assert code == 0
    assert "no data" in text
    with rig.db() as conn:
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "no_data"
        assert (
            conn.execute("SELECT COUNT(*) FROM mc_state WHERE key = 'last_end'").fetchone()[0] == 0
        )
    rig.source.frames = full  # the bars arrive
    code, text = rig.tick(at(16, 35))
    assert code == 0
    assert "1 slot(s) processed" in text


def test_a_kill_switch_file_liquidates_and_stops_the_account(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.tick(at(16, 30))
    rig.tick(at(21, 30))
    rig.tick(at(15, 30, day=11))
    assert any(shares > 0 for shares in rig.state()["shares"])
    (tmp_path / "KILL_SWITCH_MC").write_text("stop", encoding="utf-8")
    rig.tick(at(17, 30, day=11))
    state = rig.state()
    assert state["stopped"] is True
    assert all(shares == 0 for shares in state["shares"])
    with rig.db() as conn:
        reasons = {r[0] for r in conn.execute("SELECT reason FROM mc_trades WHERE side = 'sell'")}
        events = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'MC_KILL_FILE'").fetchone()[
            0
        ]
    assert reasons == {"KILL_SWITCH_FILE"}
    assert events == 1
    rig.tick(at(21, 30, day=11))  # stopped for good: no new entries even with the same uptrend
    assert all(shares == 0 for shares in rig.state()["shares"])


def test_the_report_summarizes_the_account(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.job.report(lambda line: None) == 1  # no database yet
    rig.tick(at(21, 30))
    lines: list[str] = []
    assert rig.job.report(lines.append) == 0
    assert "equity" in lines[0]
    assert "Positions" in lines[0]


def test_a_slow_first_download_continues_over_several_ticks(tmp_path: Path) -> None:
    """Each clock reading costs 60 s: the 120 s budget allows only some symbols per tick."""
    rig = Rig(tmp_path, seconds_per_call=60.0)
    code, text = rig.tick(at(16, 30))
    assert code == 0
    assert "still downloading history" in text
    with rig.db() as conn:
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "no_data"
        stored = conn.execute("SELECT COUNT(DISTINCT symbol) FROM intraday_bars").fetchone()[0]
        assert 0 < stored < len(UNIVERSE)
        assert conn.execute("SELECT COUNT(*) FROM mc_state WHERE key = 'trader'").fetchone()[0] == 0
    for minute in (35, 40, 45):
        code, text = rig.tick(at(16, minute))
        if "processed" in text:
            break
    assert "1 slot(s) processed" in text
    assert rig.state()["counter"] == 0


def test_a_crash_in_the_middle_of_a_tick_leaves_nothing_half_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick lands together or not at all: a retry must not duplicate trades or events."""
    source = FakeSource()
    reference = Rig(tmp_path / "reference", source)
    reference.tick(at(16, 30))
    reference.tick(at(21, 30))

    crashing = Rig(tmp_path / "crashing", source)
    crashing.tick(at(16, 30))
    with crashing.db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM mc_equity").fetchone()[0]

    real = real_forecast_at
    calls = [0]

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls[0] += 1
        if calls[0] == 4:
            raise RuntimeError("simulated crash")
        return real(*args, **kwargs)

    monkeypatch.setattr(run_mc_module, "forecast_at", flaky)
    code, text = crashing.tick(at(21, 30))
    assert code == 2
    assert "unexpected error" in text
    with crashing.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mc_equity").fetchone()[0] == before
        assert conn.execute("SELECT value FROM mc_state WHERE key = 'last_end'").fetchone()[0] == (
            "2026-11-10T16:00:00Z"
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE type = 'MC_TRADE'").fetchone()[0] == 0
        )
        assert conn.execute("SELECT status FROM runs ORDER BY id DESC").fetchone()[0] == "error"
    assert [a.kind for a in crashing.recorder.alerts][-1] == "RUN_FAILED"

    monkeypatch.setattr(run_mc_module, "forecast_at", real)
    code, _ = crashing.tick(at(21, 35))
    assert code == 0
    assert crashing.state() == reference.state()
    trades = "SELECT slot_end, symbol, side, notional FROM mc_trades ORDER BY id"
    with crashing.db() as a, reference.db() as b:
        assert a.execute(trades).fetchall() == b.execute(trades).fetchall()
        count = "SELECT COUNT(*) FROM events WHERE type = 'MC_TRADE'"
        assert a.execute(count).fetchone() == b.execute(count).fetchone()

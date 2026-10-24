"""T23: the two paper instances, B (qqq_core_dip) and DIP (crypto_dip)."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from pydantic import SecretStr

from stride import run_daily
from stride.backtest.validation import candidate_config, strategy_candidates
from stride.config import SEARCH_GROUP, StrideConfig, load_config
from stride.data import store
from stride.data.calendar import trading_days
from stride.data.update import UpdateResult
from stride.execution.alpaca_broker import AlpacaBroker
from stride.execution.broker import Broker
from stride.execution.shadow_broker import ShadowBroker
from stride.reporting.alerts import Alert
from stride.settings import CredentialError, MissingCredentialError, Secrets

ROOT = Path(__file__).resolve().parents[1]
BASE = load_config(ROOT / "config.yaml", env={})
NY = ZoneInfo("America/New_York")
PAPER_KEY, PAPER_SECRET = "PKFAKEBKEY", "fake-b-secret"
DIP_KEY, DIP_SECRET = "PKFAKEDIPKEY", "fake-dip-secret"
DAY1, DAY2 = date(2026, 10, 22), date(2026, 10, 23)


def _secrets(**values: str) -> Secrets:
    return Secrets(_env_file=None, **{k: SecretStr(v) for k, v in values.items()})  # type: ignore[call-arg]


def _paper(name: str) -> StrideConfig:
    return load_config(ROOT / f"config.paper_{name}.yaml", env={})


# The files.


def _expected(name: str) -> dict[str, Any]:
    """config.yaml with exactly the differences T23 allows."""
    data = BASE.model_dump()
    data["mode"] = "paper"
    data["data"]["db_path"] = f"data/paper_{name}.db"
    data["experiments"]["enabled"] = ["allocation", "drawdown_scaling"]
    candidate = "qqq_core_dip" if name == "b" else "crypto_dip"
    data["experiments"]["allocation"].update(strategy_candidates(BASE)[candidate])
    data["execution"]["broker"] = "alpaca"
    if name == "b":
        data["universe"] = [*BASE.universe, "QQQ"]
        data["groups"][SEARCH_GROUP] = ["QQQ"]
        data["leveraged_etfs"]["QQQ"] = {2: "QLD", 3: "TQQQ"}
        # QQQ moves out of the search lists, as `with_search_universe` does.
        data["experiments"]["search_universe"] = [
            s for s in BASE.experiments.search_universe if s != "QQQ"
        ]
        data["experiments"]["search_leveraged_etfs"] = {}
        data["experiments"]["dd_scale_window"] = 252
        data["execution"]["alpaca_key_env"] = "ALPACA"
    else:
        data["experiments"]["dd_scale_window"] = 0
        data["execution"]["alpaca_key_env"] = "ALPACA_DIP"
    return data


@pytest.mark.parametrize("name", ["b", "dip"])
def test_paper_config_differs_from_config_yaml_only_where_allowed(name: str) -> None:
    assert _paper(name).model_dump() == _expected(name)


@pytest.mark.parametrize(("name", "candidate"), [("b", "qqq_core_dip"), ("dip", "crypto_dip")])
def test_allocation_is_the_validation_candidate(name: str, candidate: str) -> None:
    paper = _paper(name)
    validated = candidate_config(BASE, candidate)
    assert paper.experiments.allocation == validated.experiments.allocation
    assert paper.experiments.dd_scale_window == validated.experiments.dd_scale_window
    for key, value in strategy_candidates(BASE)[candidate].items():
        assert getattr(paper.experiments.allocation, key) == value, key


def test_config_yaml_defaults_keep_todays_behavior() -> None:
    assert (BASE.execution.broker, BASE.execution.alpaca_key_env) == ("alpaca", "ALPACA")
    raw = BASE.model_dump()
    del raw["execution"]["broker"], raw["execution"]["alpaca_key_env"]
    assert StrideConfig.model_validate(raw).execution == BASE.execution  # same defaults


def test_instances_never_share_files() -> None:
    b = run_daily.load_instance(ROOT / "config.paper_b.yaml")
    dip = run_daily.load_instance(ROOT / "config.paper_dip.yaml")
    assert (b.title, dip.title) == ("STRIDE B", "STRIDE DIP")
    assert b.db != dip.db and b.log_file != dip.log_file
    assert b.kill_switch != dip.kill_switch and b.halt != dip.halt  # one fires, the other runs
    assert (
        ROOT / "KILL_SWITCH" in b.kill_switch_files
        and ROOT / "KILL_SWITCH" in dip.kill_switch_files
    )
    assert b.config.execution.alpaca_key_env != dip.config.execution.alpaca_key_env


# Keys.


def test_alpaca_paper_keys_by_prefix() -> None:
    secrets = _secrets(alpaca_api_key=PAPER_KEY, alpaca_secret_key=PAPER_SECRET,
                       alpaca_dip_api_key=DIP_KEY, alpaca_dip_secret_key=DIP_SECRET)  # fmt: skip
    assert secrets.alpaca_paper_keys() == (PAPER_KEY, PAPER_SECRET)
    assert secrets.alpaca_paper_keys("ALPACA") == (PAPER_KEY, PAPER_SECRET)
    assert secrets.alpaca_paper_keys("ALPACA_DIP") == (DIP_KEY, DIP_SECRET)
    with pytest.raises(CredentialError, match="unknown"):
        secrets.alpaca_paper_keys("ALPACA_LIVE")


def test_dip_keys_are_optional_and_paper_only() -> None:
    with pytest.raises(MissingCredentialError, match="ALPACA_DIP_API_KEY"):
        _secrets().alpaca_paper_keys("ALPACA_DIP")
    live = _secrets(alpaca_dip_api_key="AKFAKELIVE", alpaca_dip_secret_key="x-secret")
    with pytest.raises(CredentialError, match="not a paper-trading key") as info:
        live.alpaca_paper_keys("ALPACA_DIP")
    assert not isinstance(info.value, MissingCredentialError)
    assert "AKFAKELIVE" not in str(info.value)


def test_dip_keys_are_read_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ALPACA_DIP_API_KEY", "ALPACA_DIP_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"ALPACA_DIP_API_KEY={DIP_KEY}\nALPACA_DIP_SECRET_KEY={DIP_SECRET}\n",
                   encoding="utf-8")  # fmt: skip
    from stride.settings import load_secrets  # noqa: PLC0415

    assert load_secrets(env).alpaca_paper_keys("ALPACA_DIP") == (DIP_KEY, DIP_SECRET)


# Brokers.


def _bars(days: pd.DatetimeIndex, seed: int, drift: float, vol: float) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(drift, vol, len(days)))
    out = pd.DataFrame(
        {"open": close * 0.999, "high": close * 1.01, "low": close * 0.99, "close": close},
        index=days,
    )
    out["volume"], out["source"] = 1000, "test"
    out["fetched_at"] = pd.Timestamp("2026-10-01", tz="UTC")
    out.index.name = "date"
    return out


DAYS = trading_days(date(2024, 6, 3), DAY2)
BARS = {
    s: _bars(DAYS, i, 0.0005 if s in {"SPY", "QQQ", "SSO", "QLD"} else 0.001, 0.02)
    for i, s in enumerate(["SPY", "QQQ", "SSO", "QLD", "IBIT", "ETHA"])
}


def _store(conn: sqlite3.Connection, last: date) -> None:
    for symbol, frame in BARS.items():
        rows = frame.loc[: pd.Timestamp(last)].copy()
        rows["symbol"] = symbol
        store.upsert_prices(conn, rows)


def _copy_instances(folder: Path) -> tuple[run_daily.Instance, run_daily.Instance]:
    for name in ("config.paper_b.yaml", "config.paper_dip.yaml"):
        shutil.copy(ROOT / name, folder / name)
    return (run_daily.load_instance(folder / "config.paper_b.yaml"),
            run_daily.load_instance(folder / "config.paper_dip.yaml"))  # fmt: skip


def test_dip_without_its_keys_uses_the_shadow_broker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, dip = _copy_instances(tmp_path)
    conn = store.connect(dip.db)
    _store(conn, DAY1)
    only_b = _secrets(alpaca_api_key=PAPER_KEY, alpaca_secret_key=PAPER_SECRET)
    with caplog.at_level(logging.WARNING, logger="stride.run_daily"):
        broker = run_daily.make_broker(dip.config, only_b, conn)
    assert isinstance(broker, ShadowBroker)  # never B's keys
    assert "shadow" in caplog.text and PAPER_KEY not in caplog.text
    assert broker.get_account().cash == dip.config.cash_flows.initial_deposit_usd


def test_dip_with_its_keys_uses_its_own_alpaca_account(tmp_path: Path) -> None:
    _, dip = _copy_instances(tmp_path)
    both = _secrets(alpaca_api_key=PAPER_KEY, alpaca_secret_key=PAPER_SECRET,
                    alpaca_dip_api_key=DIP_KEY, alpaca_dip_secret_key=DIP_SECRET)  # fmt: skip
    broker = run_daily.make_broker(dip.config, both, store.connect(":memory:"))
    assert isinstance(broker, AlpacaBroker)
    assert broker._client._api_key == DIP_KEY  # type: ignore[attr-defined]


def test_b_without_keys_stops(tmp_path: Path) -> None:
    b, _ = _copy_instances(tmp_path)
    with pytest.raises(MissingCredentialError, match="ALPACA_API_KEY"):
        run_daily.make_broker(b.config, _secrets(), store.connect(":memory:"))


def test_shadow_can_be_forced(tmp_path: Path) -> None:
    b, _ = _copy_instances(tmp_path)
    conn = store.connect(b.db)
    _store(conn, DAY1)
    data = b.config.model_dump()
    data["execution"]["broker"] = "shadow"
    forced = StrideConfig.model_validate(data)
    keys = _secrets(alpaca_api_key=PAPER_KEY, alpaca_secret_key=PAPER_SECRET)
    assert isinstance(run_daily.make_broker(forced, keys, conn), ShadowBroker)


def test_shadow_fills_at_the_open_once_the_bar_is_stored_and_persists(tmp_path: Path) -> None:
    _, dip = _copy_instances(tmp_path)
    conn = store.connect(dip.db)
    _store(conn, DAY1)
    shadow = ShadowBroker(dip.config, conn, ["SPY", "IBIT"])
    order_id = shadow.submit_order("SPY", "buy", 50.0, client_order_id="stride-x-SPY-buy-1")
    assert shadow.cancel_all() == 0  # in flight for the next open
    again = ShadowBroker(dip.config, conn, ["SPY", "IBIT"])  # a new run, same database
    assert again.get_order(order_id).status == "pending"
    _store(conn, DAY2)  # the evening download brings DAY2's bar
    later = ShadowBroker(dip.config, conn, ["SPY", "IBIT"])
    filled = later.get_order(order_id)
    assert (filled.status, filled.filled) == ("filled", DAY2)
    open_ = BARS["SPY"]["open"].to_numpy(dtype=float)[-1]  # the open of DAY2
    assert filled.average_price == pytest.approx(open_ * 1.0005)
    assert later.find_order("stride-x-SPY-buy-1") == filled
    assert "SPY" in later.get_positions()
    assert later.is_fractionable("ETHA")


def test_shadow_refuses_other_modes(tmp_path: Path) -> None:
    _, dip = _copy_instances(tmp_path)
    with pytest.raises(ValueError, match="paper"):
        ShadowBroker(dip.config.model_copy(update={"mode": "backtest"}),
                     store.connect(":memory:"), ["SPY"])  # fmt: skip


# Both instances on the same day.


@dataclass
class Recorder:
    alerts: list[Alert] = field(default_factory=list)
    documents: list[tuple[str, str]] = field(default_factory=list)

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def send_document(self, filename: str, content: bytes, caption: str) -> None:
        self.documents.append((filename, caption))


def test_both_instances_run_the_same_day_without_sharing_anything(tmp_path: Path) -> None:
    b, dip = _copy_instances(tmp_path)
    clock = {"now": datetime(2026, 10, 22, 18, 0, tzinfo=NY)}
    recorder = Recorder()
    used: dict[str, list[str]] = {"B": [], "DIP": []}

    def deps(instance: run_daily.Instance) -> run_daily.Deps:
        def update(conn: sqlite3.Connection, config: StrideConfig, day: date,
                   secrets: Secrets, only_missing: bool) -> list[UpdateResult]:  # fmt: skip
            _store(conn, day)
            return []

        def broker(config: StrideConfig, secrets: Secrets, conn: sqlite3.Connection) -> Broker:
            made = run_daily.make_broker(config, secrets, conn)
            used[instance.name].append(type(made).__name__)
            return made

        return run_daily.Deps(
            now=lambda: clock["now"], broker=broker, update_prices=update,
            risk_free=lambda: pd.Series(0.04, index=pd.date_range("2006-01-01", "2026-12-31")),
            alerter=recorder, git_hash=lambda: "abc1234",
        )  # fmt: skip

    keys = _secrets(alpaca_api_key=PAPER_KEY, alpaca_secret_key=PAPER_SECRET)
    shadow_b = b.config.model_dump()
    shadow_b["execution"]["broker"] = "shadow"  # no network in tests: B on its own shadow too
    b = run_daily.Instance(b.config_path, StrideConfig.model_validate(shadow_b), b.name)
    for instance in (b, dip):
        conn = store.connect(instance.db)
        _store(conn, DAY1 - timedelta(days=1))
        conn.close()
        assert run_daily.run_phase(instance, "auto", deps(instance), secrets=keys) == 0
    assert used == {"B": ["ShadowBroker"], "DIP": ["ShadowBroker"]}
    titles = sorted(caption.split(" [")[0] for _, caption in recorder.documents)
    assert titles == ["STRIDE B", "STRIDE DIP"]
    b_conn, dip_conn = store.connect(b.db), store.connect(dip.db)
    b_orders = set(store.read_orders(b_conn)["symbol"])
    dip_orders = set(store.read_orders(dip_conn)["symbol"])
    assert b_orders and dip_orders
    assert b_orders <= {"QQQ", "QLD", "TQQQ", "IBIT", "ETHA", "BITX", "ETHU"}
    assert dip_orders <= {"SPY", "SSO", "UPRO", "IBIT", "ETHA", "BITX", "ETHU"}
    assert len(store.read_runs(b_conn)) == len(store.read_runs(dip_conn)) == 1
    assert b.db.resolve() != dip.db.resolve()

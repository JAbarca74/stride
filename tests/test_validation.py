"""Validation protocol (SPEC section 9)."""

from __future__ import annotations

import contextlib
import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from stride.backtest import validation as v
from stride.backtest.engine import BacktestSettings, MarketData
from stride.backtest.metrics import Metrics
from stride.config import load_config
from stride.data.calendar import trading_days
from stride.data.store import connect, upsert_prices

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml", env={})
DAYS = trading_days(date(2010, 1, 4), date(2017, 6, 30))
RATE = pd.Series(0.01, index=pd.date_range("2009-12-01", "2017-07-31"))


def _market() -> MarketData:
    bars = {}
    for i, (symbol, vol) in enumerate({"SPY": 0.18, "IEF": 0.07, "DBC": 0.35}.items()):
        rng = np.random.default_rng(i)
        close = 100 * np.cumprod(1 + rng.normal(0.0005, vol / math.sqrt(252), len(DAYS)))
        open_ = close * (1 + rng.normal(0, 0.002, len(DAYS)))
        bars[symbol] = pd.DataFrame(
            {"open": open_, "high": np.maximum(open_, close) * 1.006,
             "low": np.minimum(open_, close) * 0.994, "close": close},
            index=DAYS,
        )  # fmt: skip
    return MarketData(bars, RATE)


SETTINGS = BacktestSettings(date(2010, 1, 4), date(2017, 6, 30), 10_000.0)
ONE = [("strategy", "entry_breakout")]  # a single parameter keeps the tests fast


def test_walk_forward_windows_are_five_years_then_one() -> None:
    windows = v.walk_forward_windows(date(2007, 1, 1), date(2026, 10, 22))
    assert windows[0] == v.Window(date(2007, 1, 1), date(2012, 1, 1), date(2012, 12, 31))
    assert windows[-1] == v.Window(date(2021, 1, 1), date(2026, 1, 1), date(2026, 10, 22))
    assert len(windows) == 15
    # A first year that starts after 1 January is not a full in-sample year.
    assert v.walk_forward_windows(date(2010, 1, 4), date(2017, 6, 30))[0].out_start == date(
        2016, 1, 1
    )
    assert v.walk_forward_windows(date(2010, 1, 4), date(2015, 6, 30)) == []


def test_walk_forward_reports_only_out_of_sample_days() -> None:
    walk = v.walk_forward(_market(), CONFIG, 10_000.0, SETTINGS.start, SETTINGS.end)
    assert walk.returns.index.min() >= pd.Timestamp("2016-01-01")
    assert walk.returns.index.max() <= pd.Timestamp("2017-06-30")
    assert list(walk.per_window["out_of_sample"]) == ["2016", "2017"]
    assert walk.spy is not None
    with pytest.raises(ValueError, match="shorter"):
        v.walk_forward(_market(), CONFIG, 10_000.0, date(2014, 1, 1), SETTINGS.end)


def test_shifted_config_moves_one_parameter_in_memory() -> None:
    down = v.shifted_config(CONFIG, "strategy", "entry_breakout", 0.8)
    up = v.shifted_config(CONFIG, "strategy", "entry_breakout", 1.2)
    assert (down.strategy.entry_breakout, up.strategy.entry_breakout) == (44, 66)
    stop = v.shifted_config(CONFIG, "strategy", "stop_n", 0.8)
    assert stop.strategy.stop_n == pytest.approx(1.6)
    assert CONFIG.strategy.entry_breakout == 55
    assert stop.risk == CONFIG.risk
    valid = 0
    for section, key in v.SENSITIVITY_PARAMETERS:
        for factor in (0.8, 1.2):
            with contextlib.suppress(ValidationError):
                v.shifted_config(CONFIG, section, key, factor)
                valid += 1
    assert valid >= 2 * len(v.SENSITIVITY_PARAMETERS) - 4  # only a few hit a hard limit


def test_sharpe_drop() -> None:
    assert v.sharpe_drop(1.0, 0.6) == pytest.approx(0.4)
    assert v.sharpe_drop(-0.5, -1.0) == pytest.approx(1.0)
    assert v.sharpe_drop(1.0, 1.2) == pytest.approx(-0.2)
    assert math.isnan(v.sharpe_drop(0.0, 1.0))
    assert math.isnan(v.sharpe_drop(1.0, math.nan))


def test_sensitivity_flags_fragile_parameters() -> None:
    sens = v.sensitivity(_market(), CONFIG, SETTINGS, ONE)
    assert list(sens.table["value"]) == [44, 66]
    expected = [bool(not math.isfinite(d) or d > v.FRAGILE_SHARPE_DROP) for d in sens.table["drop"]]
    assert list(sens.table["fragile"]) == expected
    assert sens.fragile == (["strategy.entry_breakout"] if any(expected) else [])


def test_block_bootstrap_brackets_the_true_sharpe_and_is_reproducible() -> None:
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2010-01-01", periods=2_000)
    returns = pd.Series(rng.normal(0.0006, 0.01, len(days)), index=days)
    first = v.block_bootstrap(returns, samples=1_000, seed=3)
    again = v.block_bootstrap(returns, samples=1_000, seed=3)
    assert first == again
    true_sharpe = 0.0006 / 0.01 * math.sqrt(252)
    low, mid, high = first.sharpe
    assert low < true_sharpe < high
    assert low < mid < high
    assert 0 < first.max_drawdown[0] <= first.max_drawdown[2] < 1
    rising = pd.Series(0.001, index=days[:100])
    flat = v.block_bootstrap(rising, samples=50)
    assert flat.max_drawdown == (0.0, 0.0, 0.0)
    assert all(math.isnan(x) for x in flat.sharpe)  # zero volatility: no Sharpe
    with pytest.raises(ValueError, match="two blocks"):
        v.block_bootstrap(rising.iloc[:30])


def test_block_bootstrap_uses_each_days_risk_free() -> None:
    days = pd.bdate_range("2010-01-01", periods=500)
    rng = np.random.default_rng(2)
    returns = pd.Series(rng.normal(0.0004, 0.01, len(days)), index=days)
    rf = pd.Series(0.05, index=days)
    with_rf = v.block_bootstrap(returns, rf, samples=500)
    without = v.block_bootstrap(returns, 0.0, samples=500)
    assert with_rf.sharpe[1] < without.sharpe[1]


def test_leverage_comparison_has_the_dynamic_and_fixed_rows() -> None:
    table = v.leverage_comparison(_market(), CONFIG, SETTINGS, levels=(1.0, 2.0))
    assert list(table["leverage"]) == ["dynamic", "1x", "2x"]
    assert set(table.columns) >= {"cagr", "sharpe", "max_drawdown", "average_exposure"}


def _walk(sharpe: float, spy: float) -> v.WalkForward:
    m = Metrics(0.1, 0.1, sharpe, 1.0, 0.1, 1.0)
    s = Metrics(0.1, 0.1, spy, 1.0, 0.1, 1.0)
    return v.WalkForward([], pd.Series(dtype=float), pd.DataFrame(), m, s)


def _sens(fragile: bool) -> v.Sensitivity:
    table = pd.DataFrame({"parameter": ["strategy.sma_filter"], "fragile": [fragile]})
    return v.Sensitivity(1.0, table)


def test_verdict_applies_the_phase_3_exit_criterion() -> None:
    assert v.verdict(_walk(0.8, 0.5), _sens(False)).passes
    assert not v.verdict(_walk(0.4, 0.5), _sens(False)).passes  # below SPY
    assert not v.verdict(_walk(0.8, 0.5), _sens(True)).passes  # fragile
    alarm = v.verdict(_walk(1.6, 0.5), _sens(False))
    assert alarm.alarm and not alarm.passes  # above 1.5: audit
    assert not v.verdict(_walk(math.nan, 0.5), _sens(False)).passes


def test_validate_strategy_and_outputs(tmp_path: Path) -> None:
    result = v.validate_strategy(
        _market(),
        CONFIG,
        SETTINGS,
        bootstrap_samples=200,
        parameters=ONE,
        levels=(1.0,),
        goal_leverages=(None,),
        rotation=False,
        selection_levels=(),
        search_levels=(),
    )
    text = v.to_markdown(result, {"Data": "synthetic"})
    for heading in ("Phase 3 exit criterion", "Walk-forward", "Sensitivity", "bootstrap",
                    "Leverage comparison", "Improvements", "Owner goal", "Notes"):  # fmt: skip
        assert heading in text
    v.write_outputs(tmp_path / "results", result, {"Data": "synthetic"})
    payload = json.loads((tmp_path / "results" / "validation.json").read_text(encoding="utf-8"))
    assert payload["bootstrap"]["samples"] == 200
    assert payload["verdict"]["passes"] == result.verdict.passes
    assert len(payload["sensitivity"]["runs"]) == 2


def test_main_writes_results_and_returns_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = v.validate_strategy

    def light(*args: Any, **kwargs: Any) -> v.Validation:
        return full(
            *args,
            **kwargs,
            parameters=ONE,
            levels=(2.0,),
            goal_leverages=(None,),
            rotation=False,
            selection_levels=(1.0,),
            search_levels=(),
        )

    monkeypatch.setattr(v, "validate_strategy", light)
    conn = connect(tmp_path / "p.db")
    for symbol, frame in _market().bars.items():
        upsert_prices(
            conn,
            frame.assign(symbol=symbol, volume=1, source="test",
                         fetched_at=pd.Timestamp("2017-07-01", tz="UTC")).rename_axis("date"),
        )  # fmt: skip
    conn.close()
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(text.replace("db_path: data/stride.db", "db_path: p.db"), encoding="utf-8")
    code = v.main(
        ["--config", str(config), "--start", "2010-01-04", "--samples", "100"],
        rate_source=lambda: RATE,
    )
    results = tmp_path / "docs" / "results"
    assert code in {0, 1}
    assert (results / "validation.md").exists()
    assert (results / "backtest.html").exists()
    assert (tmp_path / "data" / "validation.db").exists()
    (tmp_path / "p.db").unlink()
    assert v.main(["--config", str(config)], rate_source=lambda: RATE) == 2


def test_sensitivity_skips_shifts_that_make_the_config_invalid() -> None:
    sens = v.sensitivity(_market(), CONFIG, SETTINGS, [("risk", "kelly_fraction")])
    up = sens.table[sens.table["value"] > 1.0]
    assert list(up["note"]) == [v.SKIPPED] and not up["fragile"].any()
    down = sens.table[sens.table["value"] < 1.0]
    assert list(down["note"]) == [""]


def test_cost_rule_comparison_runs_with_and_without_the_rule() -> None:
    small = BacktestSettings(SETTINGS.start, SETTINGS.end, 100.0)
    table = v.cost_rule_comparison(_market(), CONFIG, small)
    assert list(table["run"]) == ["with rule", "without rule"]
    assert table.loc[1, "resizes_skipped"] == 0
    assert set(table.columns) >= {"sharpe", "costs_per_year", "fills", "final_equity"}


def test_improvement_variants_turn_off_one_improvement_each() -> None:
    variants = v.improvement_variants(CONFIG)
    base = variants["configured (all improvements)"]
    assert base == CONFIG
    assert variants["Fixed pricing instead of Tiered"].costs.pricing == "fixed"
    assert variants["without cost-aware resizing"].execution.max_resize_cost_pct == 0.999
    old = variants["cash earning the full rate (old assumption)"].costs
    assert old.cash_interest_free_usd == 0.0
    for name, cfg in variants.items():  # every other value is untouched
        if name != "Fixed pricing instead of Tiered":
            assert cfg.costs.pricing == CONFIG.costs.pricing


def test_experiment_variants_need_data_for_the_wider_universe() -> None:
    variants = v.experiment_variants(CONFIG, _market())  # no bars for the extra ETFs
    assert list(variants) == [
        "with the multi-horizon ensemble (20/10, 55/20, 100/40)",
        "with drawdown scaling of the leverage",
        "with leverage rotation (3x SPY, refuge IEF)",
    ]
    assert variants[next(iter(variants))].experiments.enabled == ["ensemble"]
    scaled = variants["with drawdown scaling of the leverage"]
    assert scaled.experiments.enabled == ["drawdown_scaling"]
    bars = dict(_market().bars)
    for symbol in CONFIG.experiments.extra_universe:
        bars[symbol] = bars["DBC"]
    wide = v.experiment_variants(CONFIG, MarketData(bars, RATE))
    name = "with the wider universe (7 more ETFs)"
    assert name in wide and len(wide[name].universe) == 17


def test_goal_search_marks_runs_that_meet_30_percent_within_25_drawdown() -> None:
    table = v.goal_search(_market(), CONFIG, SETTINGS, leverages=(None, 2.0), rotations=False)
    assert len(table) == 4
    assert list(table["cagr"]) == sorted(table["cagr"], reverse=True)
    expected = (table["cagr"] >= v.GOAL_CAGR) & (table["max_drawdown"] <= v.GOAL_MAX_DRAWDOWN)
    assert list(table["meets_goal"]) == list(expected)
    assert v.goal_summary(table).startswith(("MET:", "NOT MET:"))


def test_goal_summary_wording() -> None:
    row = {"leverage": "2x", "drawdown_scaling": "on", "sharpe": 1.0, "average_exposure": 1.0,
           "final_equity": 1.0}  # fmt: skip
    met = pd.DataFrame([row | {"cagr": 0.31, "max_drawdown": 0.2, "meets_goal": True}])
    assert v.goal_summary(met).startswith("MET: 2x leverage")
    short = pd.DataFrame([row | {"cagr": 0.12, "max_drawdown": 0.2, "meets_goal": False}])
    assert "18.0% short of 30%" in v.goal_summary(short)
    deep = pd.DataFrame([row | {"cagr": 0.4, "max_drawdown": 0.5, "meets_goal": False}])
    assert v.goal_summary(deep) == "NOT MET: no run keeps the maximum drawdown within 25%"


def _rotation_market() -> MarketData:
    """SPY with a bull run, a crash and a recovery; UPRO and SSO as its 3x and 2x;
    IEF as the refuge."""
    rng = np.random.default_rng(4)
    n = len(DAYS)
    drift = np.where((np.arange(n) > 900) & (np.arange(n) < 1100), -0.004, 0.0007)
    spy = 100 * np.cumprod(1 + drift + rng.normal(0, 0.009, n))
    r = pd.Series(spy).pct_change().fillna(0.0).to_numpy()

    def bars(close: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame(
            {"open": close, "high": close * 1.004, "low": close * 0.996, "close": close},
            index=DAYS,
        )

    out = {
        "SPY": bars(spy),
        "SSO": bars(50 * np.cumprod(1 + 2 * r)),
        "UPRO": bars(30 * np.cumprod(1 + 3 * r)),
        "IEF": bars(100 * np.cumprod(1 + rng.normal(0.0001, 0.003, n))),
    }
    return MarketData(out, RATE, {"SPY": {2: "SSO", 3: "UPRO"}})


def test_rotation_goal_rows_cover_multiples_refuges_and_scaling() -> None:
    rows = v._rotation_goal_rows(_rotation_market(), CONFIG, SETTINGS)
    labels = {(r["leverage"], r["drawdown_scaling"]) for r in rows}
    assert labels == {
        (f"rotation {m}x/{ref}", sc)
        for m in (2, 3) for ref in ("cash", "IEF") for sc in ("off", "on")
    }  # fmt: skip


def test_rotation_report_runs_the_protocol() -> None:
    report = v.rotation_report(_rotation_market(), CONFIG, SETTINGS, bootstrap_samples=100)
    assert report is not None
    assert report.config_label == "3x SPY, SMA 200, band 0.0, refuge IEF"
    assert list(report.sensitivity["change"]) == ["sma 160", "sma 240", "band 0.02", "multiple 2"]
    assert report.walk_forward.returns.index.min() >= pd.Timestamp("2016-01-01")
    text = v._rotation_markdown(report)
    assert any("Phase 3 criterion for the rotation" in line for line in text)
    assert v.rotation_report(MarketData({}, RATE), CONFIG, SETTINGS) is None


def test_window_status_marks_a_system_stopped_before_its_year() -> None:
    w = v.Window(date(2007, 1, 1), date(2012, 1, 1), date(2012, 12, 31))
    assert v._window_status("", w) == "traded"
    assert v._window_status("2009-07-07", w) == "stopped in sample"
    assert v._window_status("2012-05-02", w) == "stopped"


def test_walk_forward_selected_chooses_in_sample_and_reports_out_of_sample() -> None:
    result = v.walk_forward_selected(
        _market(), CONFIG, 10_000.0, SETTINGS.start, SETTINGS.end, levels=(1.0, 2.0)
    )
    table = result.per_window
    assert list(table["out_of_sample"]) == ["2016", "2017"]
    assert set(table["chosen_leverage"]) <= {"1x", "2x"}
    assert set(table["drawdown_scaling"]) <= {"on", "off"}
    assert result.returns.index.min() >= pd.Timestamp("2016-01-01")
    text = v._selected_markdown(result)
    assert any("leverage chosen in sample" in line for line in text)
    assert v._selected_markdown(None) == []


def test_deflated_sharpe_falls_as_more_variants_are_tried() -> None:
    rng = np.random.default_rng(9)
    returns = pd.Series(rng.normal(0.0006, 0.01, 2_500))
    few = v.deflated_sharpe(returns, [0.9])
    many = v.deflated_sharpe(returns, list(np.linspace(-0.5, 1.2, 60)))
    assert 0 <= many < few <= 1
    assert math.isnan(v.deflated_sharpe(returns.iloc[:10], [0.5]))


def test_strategy_search_reports_each_candidate_against_spy() -> None:
    data = _market()
    bars = dict(data.bars) | {"EFA": data.bars["DBC"], "IEF": data.bars["IEF"]}
    market = MarketData(bars, data.risk_free)
    results = v.strategy_search(
        market, CONFIG, SETTINGS, [0.5, 0.6], levels=(1.0,), names=["dual_momentum"]
    )
    assert [r.name for r in results] == ["dual_momentum"]
    r = results[0]
    assert list(r.full["leverage"]) == ["1x", "1x"]
    assert r.selected.returns.index.min() >= pd.Timestamp("2016-01-01")
    assert isinstance(r.passes, bool)
    text = v._search_markdown(results)
    assert any("Pre-registered strategy search" in line for line in text)


def test_strategy_search_leaves_out_candidates_without_data() -> None:
    # The fake market has no QQQ or sector ETFs: round 2 is left out, not an error.
    results = v.strategy_search(
        _market(), CONFIG, SETTINGS, [0.5], levels=(1.0,), names=["sector_momentum"]
    )
    assert results == []


def test_round_two_is_capped_at_one_and_a_half_and_uses_the_search_universe() -> None:
    assert v.candidate_levels("momentum_vol", (1.0, 1.5, 2.0)) == (1.0, 1.5)
    assert v.candidate_levels("vol_managed", (1.0, 1.5, 2.0)) == (1.0, 1.5, 2.0)
    cfg = v.candidate_config(CONFIG, "sector_momentum")
    assert {"QQQ", "XLK", "EEM"} <= set(cfg.universe)
    assert cfg.experiments.allocation.mode == "relative_strength"
    assert "QQQ" in cfg.leveraged_etfs
    assert set(v.candidate_config(CONFIG, "vol_managed").universe) == set(CONFIG.universe)


def test_round_four_uses_the_twelve_month_window_and_two_levels() -> None:
    assert v.candidate_levels("qqq_levered_12m", (1.0,)) == (1.5, 2.0)
    spy = v.candidate_config(CONFIG, "spy_levered_12m")
    assert spy.experiments.dd_scale_window == 252
    assert spy.experiments.allocation.assets == ["SPY"]
    qqq = v.candidate_config(CONFIG, "qqq_levered_12m")
    assert "QQQ" in qqq.universe and qqq.experiments.dd_scale_window == 252
    assert v.candidate_config(CONFIG, "spy_levered").experiments.dd_scale_window == 0


def test_round_five_is_a_twenty_percent_crypto_sleeve_without_leverage() -> None:
    names = [n for n in v.strategy_candidates(CONFIG) if n.startswith("crypto_")]
    assert names == list(v.ROUND_5)
    for name in names:
        assert v.candidate_levels(name, (1.5, 2.0)) == (1.0,)
        alloc = v.candidate_config(CONFIG, name).experiments.allocation
        assert alloc.mode == "sleeve" and alloc.sleeve == 0.20
        assert alloc.core == ["SPY"] and alloc.assets == ["IBIT", "ETHA"]
    trend = v.candidate_config(CONFIG, "crypto_trend").experiments.allocation
    assert trend.kelly_fraction == 0.5 and trend.rebalance == "daily"
    held = v.candidate_config(CONFIG, "crypto_hold").experiments.allocation
    assert held.kelly_fraction == 0.0 and held.rebalance == "monthly"
    rng = v.candidate_config(CONFIG, "crypto_range").experiments.allocation
    assert (rng.entry, rng.exit) == (20, 20)
    dip = v.candidate_config(CONFIG, "crypto_dip").experiments.allocation
    assert (dip.dip_drop, dip.dip_window, dip.take_profit) == (0.25, 90, 0.30)
    assert dip.kelly_fraction == 0.0


def test_round_six_is_a_qqq_core_at_one_and_a_half_with_the_dip_sleeve() -> None:
    assert v.candidate_levels("qqq_core_dip", (1.5, 2.0)) == (1.0,)
    cfg = v.candidate_config(CONFIG, "qqq_core_dip")
    alloc = cfg.experiments.allocation
    assert alloc.core == ["QQQ"] and alloc.core_leverage == 1.5
    assert alloc.signal == "dip" and alloc.sleeve == 0.20
    assert cfg.experiments.dd_scale_window == 252
    assert "QQQ" in cfg.universe and "QQQ" in cfg.leveraged_etfs


def test_round_three_holds_spy_at_one_and_a_half_only() -> None:
    assert v.candidate_levels("spy_levered", (1.0,)) == (1.5,)
    cfg = v.candidate_config(CONFIG, "spy_levered")
    assert cfg.experiments.allocation.mode == "hold"
    assert cfg.experiments.allocation.assets == ["SPY"]

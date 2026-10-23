"""Validation protocol (SPEC section 9). Pure functions over the backtest engine.

1. Walk-forward: 5 years in sample, 1 year out of sample, rolling yearly. Each
   window runs the engine from the start of its in-sample years, so everything the
   engine estimates (Kelly L*, the drawdown peak, the instruments) is learned from
   in-sample data; only the out-of-sample year is reported. Parameters in
   `config.yaml` are frozen and are never fitted.
2. Sensitivity: each parameter moved -20% and +20%; a parameter is fragile when
   the Sharpe drops more than 30% from the base run.
3. Block bootstrap: 5,000 resamples of daily returns in 20-day blocks, for
   confidence intervals on Sharpe and maximum drawdown.
4. Anti-look-ahead: tests/test_lookahead.py and tests/test_engine.py.
5. Leverage comparison: fixed L of 1x, 2x, 3x and 4x against the dynamic L.

Quality alarm: an out-of-sample Sharpe above 1.5 is a sign of a methodological
error and must be audited. Phase 3 exit: out-of-sample Sharpe above SPY's over
the same days, below 1.5, and no fragile parameter. Results are reported as they
come out; parameters are never changed to pass.

The protocol's numbers (window lengths, +-20%, 30%, 5,000, 20 days, 1.5) are
SPEC 9 constants, not strategy parameters.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import ValidationError

from stride.backtest import metrics, run
from stride.backtest.engine import BacktestResult, BacktestSettings, MarketData, run_backtest
from stride.config import StrideConfig, effective_config, load_config, with_extra_universe
from stride.data.leveraged import load_etf_facts, rate_known_at_t_minus_1
from stride.data.sources import FredRateSource
from stride.data.store import connect, last_price_date
from stride.data.update import series_for
from stride.reporting.report import write_report

IN_SAMPLE_YEARS = 5
OUT_OF_SAMPLE_YEARS = 1
SENSITIVITY_STEP = 0.20
FRAGILE_SHARPE_DROP = 0.30
BOOTSTRAP_SAMPLES = 5_000
BOOTSTRAP_BLOCK_DAYS = 20
SHARPE_ALARM = 1.5
FIXED_LEVERAGES = (1.0, 2.0, 3.0, 4.0)
DYNAMIC = "dynamic"
TRADING_DAYS_PER_YEAR = 252
_ZERO = 1e-12  # below this a standard deviation is float noise (as in metrics.py)

# (section, key) moved by the sensitivity test (owner decision on PR #57: as many
# layers as possible): every continuous or window parameter of the strategy, the
# risk and leverage limits, the rebalance band and the switching hysteresis.
# A shift that makes the configuration invalid (e.g. kelly_fraction above 1.0) is
# reported as skipped. Unit caps are integers already at their maximum (4).
SENSITIVITY_PARAMETERS: tuple[tuple[str, str], ...] = (
    ("strategy", "sma_filter"),
    ("strategy", "entry_breakout"),
    ("strategy", "exit_breakout"),
    ("strategy", "atr_period"),
    ("strategy", "stop_n"),
    ("strategy", "pyramid_step_n"),
    ("risk", "risk_per_unit"),
    ("risk", "kelly_fraction"),
    ("risk", "max_portfolio_vol"),
    ("risk", "max_etf_portfolio_leverage"),
    ("risk", "dd_delever_at"),
    ("risk", "kill_switch_dd"),
    ("execution", "rebalance_band"),
    ("execution", "max_resize_cost_pct"),
    ("leverage_switch", "enter_k"),
    ("leverage_switch", "exit_k"),
    ("leverage_switch", "min_hold_days"),
)
SKIPPED = "skipped: invalid configuration"


def daily_risk_free(rate: pd.Series, days: pd.DatetimeIndex) -> pd.Series:
    """Annual risk-free rate known at t-1 on each of `days` (for metrics)."""
    return rate_known_at_t_minus_1(rate, days).fillna(0.0)


def _summary(returns: pd.Series, rate: pd.Series) -> metrics.Metrics:
    return metrics.summary(returns, daily_risk_free(rate, pd.DatetimeIndex(returns.index)))


# 1. Walk-forward.


@dataclass(frozen=True)
class Window:
    in_sample_start: date
    out_start: date
    out_end: date


def walk_forward_windows(first: date, last: date) -> list[Window]:
    """Yearly out-of-sample calendar years, each after 5 in-sample years.

    The first out-of-sample year starts 5 full years after `first`; the last one
    may be partial (it ends at `last`)."""
    windows = []
    year = first.year + IN_SAMPLE_YEARS + (first > date(first.year, 1, 1))
    while date(year, 1, 1) <= last:
        windows.append(
            Window(
                date(year - IN_SAMPLE_YEARS, 1, 1),
                date(year, 1, 1),
                min(date(year + OUT_OF_SAMPLE_YEARS - 1, 12, 31), last),
            )
        )
        year += OUT_OF_SAMPLE_YEARS
    return windows


@dataclass(frozen=True)
class WalkForward:
    windows: list[Window]
    returns: pd.Series  # out-of-sample daily time-weighted returns, all windows
    per_window: pd.DataFrame  # Sharpe, CAGR, max drawdown, kill switch per window
    strategy: metrics.Metrics
    spy: metrics.Metrics | None


def walk_forward(
    data: MarketData, config: StrideConfig, initial_cash: float, first: date, last: date
) -> WalkForward:
    """Run every window and keep only its out-of-sample days."""
    windows = walk_forward_windows(first, last)
    if not windows:
        raise ValueError("the period is shorter than one in-sample window plus a year")
    pieces, rows = [], []
    for w in windows:
        result = run_backtest(
            data, config, BacktestSettings(max(w.in_sample_start, first), w.out_end, initial_cash)
        )
        returns = result.returns
        oos = returns[returns.index >= pd.Timestamp(w.out_start)]
        killed = result.events[result.events["type"] == "KILL_SWITCH"]["at"]
        m = _summary(oos, data.risk_free)
        rows.append(
            {
                "out_of_sample": f"{w.out_start.year}",
                "days": len(oos),
                "sharpe": m.sharpe,
                "cagr": m.cagr,
                "max_drawdown": m.max_drawdown,
                "kill_switch": str(killed.iloc[0]) if not killed.empty else "",
            }
        )
        pieces.append(oos)
    returns = pd.concat(pieces).sort_index()
    spy = None
    if "SPY" in data.bars:
        close = data.bars["SPY"]["close"].sort_index()
        bench = metrics.buy_and_hold_returns(close).reindex(returns.index).dropna()
        spy = _summary(bench, data.risk_free) if len(bench) > 1 else None
    return WalkForward(windows, returns, pd.DataFrame(rows), _summary(returns, data.risk_free), spy)


# 2. Sensitivity.


def shifted_config(config: StrideConfig, section: str, key: str, factor: float) -> StrideConfig:
    """`config` with one parameter multiplied by `factor` (windows rounded to whole
    days). Built in memory and re-validated; `config.yaml` is never touched."""
    block = getattr(config, section)
    value = getattr(block, key)
    moved = round(value * factor) if isinstance(value, int) else value * factor
    data = config.model_dump()
    data[section][key] = moved
    return type(config).model_validate(data)


@dataclass(frozen=True)
class Sensitivity:
    base_sharpe: float
    table: pd.DataFrame  # parameter, value, sharpe, drop, fragile

    @property
    def fragile(self) -> list[str]:
        return sorted(set(self.table.loc[self.table["fragile"], "parameter"]))


def sharpe_drop(base: float, variant: float) -> float:
    """Relative fall of the Sharpe from the base run (0.3 = 30% lower)."""
    if not (math.isfinite(base) and math.isfinite(variant)) or base == 0:
        return math.nan
    return (base - variant) / abs(base)


def sensitivity(
    data: MarketData,
    config: StrideConfig,
    settings: BacktestSettings,
    parameters: Sequence[tuple[str, str]] = SENSITIVITY_PARAMETERS,
    base_returns: pd.Series | None = None,
) -> Sensitivity:
    """Sharpe of the full-period run with each parameter at -20% and +20%.
    `base_returns` reuses a run already made with `config` and `settings`."""
    if base_returns is None:
        base_returns = run_backtest(data, config, settings).returns
    base = _summary(base_returns, data.risk_free).sharpe
    rows = []
    for section, key in parameters:
        for factor in (1 - SENSITIVITY_STEP, 1 + SENSITIVITY_STEP):
            name = f"{section}.{key}"
            try:
                moved = shifted_config(config, section, key, factor)
            except ValidationError:
                value = getattr(getattr(config, section), key) * factor
                rows.append({"parameter": name, "value": value, "sharpe": math.nan,
                             "drop": math.nan, "fragile": False, "note": SKIPPED})  # fmt: skip
                continue
            sharpe = _summary(run_backtest(data, moved, settings).returns, data.risk_free).sharpe
            drop = sharpe_drop(base, sharpe)
            rows.append(
                {
                    "parameter": name,
                    "value": getattr(getattr(moved, section), key),
                    "sharpe": sharpe,
                    "drop": drop,
                    # A Sharpe that cannot be computed is treated as fragile (fail safe).
                    "fragile": bool(not math.isfinite(drop) or drop > FRAGILE_SHARPE_DROP),
                    "note": "",
                }
            )
    return Sensitivity(base, pd.DataFrame(rows))


# 3. Block bootstrap.


@dataclass(frozen=True)
class Bootstrap:
    sharpe: tuple[float, float, float]  # 2.5%, 50%, 97.5% percentiles
    max_drawdown: tuple[float, float, float]
    samples: int
    block: int


def block_bootstrap(
    returns: pd.Series,
    risk_free: pd.Series | float = 0.0,
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    block: int = BOOTSTRAP_BLOCK_DAYS,
    seed: int = 0,
    chunk: int = 250,
) -> Bootstrap:
    """Circular block bootstrap of daily returns: each resample joins random
    `block`-day blocks (wrapping at the end) up to the original length, keeping
    each day's risk-free rate with its return."""
    r = returns.to_numpy(dtype=float)
    n = len(r)
    if n < 2 * block:
        raise ValueError("need at least two blocks of returns")
    if isinstance(risk_free, pd.Series):
        rf = risk_free.reindex(returns.index).fillna(0.0).to_numpy(dtype=float)
    else:
        rf = np.full(n, float(risk_free))
    excess = r - rf / TRADING_DAYS_PER_YEAR
    rng = np.random.default_rng(seed)
    blocks = -(-n // block)
    offsets = np.arange(block)
    sharpes, drawdowns = [], []
    for done in range(0, samples, chunk):
        size = min(chunk, samples - done)
        starts = rng.integers(0, n, size=(size, blocks))
        idx = ((starts[:, :, None] + offsets) % n).reshape(size, -1)[:, :n]
        ex = excess[idx]
        std = ex.std(axis=1, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            sharpes.append(np.where(std > _ZERO, ex.mean(axis=1) / std, np.nan) * math.sqrt(252))
        wealth = np.cumprod(1 + r[idx], axis=1)
        peak = np.maximum(np.maximum.accumulate(wealth, axis=1), 1.0)
        drawdowns.append((1 - wealth / peak).max(axis=1))
    sharpe = np.concatenate(sharpes)
    mdd = np.concatenate(drawdowns)

    def bands(values: np.ndarray) -> tuple[float, float, float]:
        if np.isnan(values).all():
            return math.nan, math.nan, math.nan
        low, mid, high = np.nanpercentile(values, [2.5, 50, 97.5])
        return float(low), float(mid), float(high)

    return Bootstrap(bands(sharpe), bands(mdd), samples, block)


# 5. Leverage comparison.


def leverage_comparison(
    data: MarketData,
    config: StrideConfig,
    settings: BacktestSettings,
    levels: Sequence[float] = FIXED_LEVERAGES,
) -> pd.DataFrame:
    """Section 9 metrics with fixed L at each level and with the dynamic L."""
    rows = []
    for level in (None, *levels):
        run = replace(settings, fixed_leverage=level)
        result = run_backtest(data, config, run)
        m = _summary(result.returns, data.risk_free)
        killed = result.events[result.events["type"] == "KILL_SWITCH"]["at"]
        rows.append(
            {
                "leverage": DYNAMIC if level is None else f"{level:g}x",
                "cagr": m.cagr,
                "volatility": m.volatility,
                "sharpe": m.sharpe,
                "max_drawdown": m.max_drawdown,
                "average_exposure": metrics.average_exposure(
                    result.equity["gross_exposure"], result.equity["equity"]
                ),
                "kill_switch": str(killed.iloc[0]) if not killed.empty else "",
            }
        )
    return pd.DataFrame(rows)


# 6. Improvements (owner request, 2026-10-23): each one turned off in turn.

NO_RESIZE_LIMIT = 0.999  # a cost limit no order reaches: the rule is off


def with_values(config: StrideConfig, section: str, **values: object) -> StrideConfig:
    """`config` with some values of one section replaced (in memory)."""
    block = getattr(config, section).model_copy(update=values)
    return config.model_copy(update={section: block})


def _toggle(config: StrideConfig, experiment: str) -> StrideConfig:
    """`config` with one experiment switched on or off (the universe re-derived)."""
    enabled = set(config.experiments.enabled) ^ {experiment}
    base = with_values(config, "experiments", enabled=sorted(enabled))
    if experiment == "extra_universe" and "extra_universe" not in enabled:
        return base  # already a re-derived universe: switching off is not supported here
    return effective_config(base)


def experiment_variants(config: StrideConfig, data: MarketData) -> dict[str, StrideConfig]:
    """Each strategy experiment switched to the opposite of the configured run.
    The wider universe runs only when every extra symbol has bars."""
    out = {}
    extras = config.experiments.extra_universe
    if extras and all(s in data.bars for s in extras):
        state = "with" if "extra_universe" not in config.experiments.enabled else "without"
        out[f"{state} the wider universe ({len(extras)} more ETFs)"] = _toggle(
            config, "extra_universe"
        )
    if len(config.experiments.breakout_systems) > 1:
        state = "with" if "ensemble" not in config.experiments.enabled else "without"
        systems = ", ".join(f"{e}/{x}" for e, x in config.experiments.breakout_systems)
        out[f"{state} the multi-horizon ensemble ({systems})"] = _toggle(config, "ensemble")
    return out


def improvement_variants(config: StrideConfig) -> dict[str, StrideConfig]:
    """The configured run, and the same run with one improvement removed each time."""
    return {
        "configured (all improvements)": config,
        "without cost-aware resizing": with_values(
            config, "execution", max_resize_cost_pct=NO_RESIZE_LIMIT
        ),
        "Fixed pricing instead of Tiered": with_values(config, "costs", pricing="fixed"),
        "cash earning the full rate (old assumption)": with_values(
            config, "costs", cash_interest_free_usd=0.0, cash_interest_full_rate_nav=1.0
        ),
    }


def compare_variants(
    data: MarketData, settings: BacktestSettings, variants: Mapping[str, StrideConfig]
) -> pd.DataFrame:
    """Full-period metrics and costs of each configuration on the same data."""
    rows = []
    for name, cfg in variants.items():
        result = run_backtest(data, cfg, settings)
        m = _summary(result.returns, data.risk_free)
        fills = result.fills
        costs = (
            (fills["commission"] + fills["spread_cost"]).set_axis(pd.DatetimeIndex(fills["date"]))
            if not fills.empty
            else pd.Series(dtype=float)
        )
        rows.append(
            {
                "run": name,
                "cagr": m.cagr,
                "sharpe": m.sharpe,
                "max_drawdown": m.max_drawdown,
                "costs_per_year": metrics.cost_ratio(costs, result.equity["equity"]),
                "fills": len(fills),
                "resizes_skipped": int((result.events["type"] == "RESIZE_SKIPPED").sum()),
                "final_equity": float(result.equity["equity"].iloc[-1]),
            }
        )
    return pd.DataFrame(rows)


def cost_rule_comparison(
    data: MarketData, config: StrideConfig, settings: BacktestSettings
) -> pd.DataFrame:
    """The run with the cost-aware resize rule and without it."""
    off = with_values(config, "execution", max_resize_cost_pct=NO_RESIZE_LIMIT)
    return compare_variants(data, settings, {"with rule": config, "without rule": off})


# Verdict.


@dataclass(frozen=True)
class Verdict:
    oos_sharpe: float
    spy_sharpe: float
    beats_spy: bool
    below_alarm: bool
    stable: bool
    alarm: bool

    @property
    def passes(self) -> bool:
        """Phase 3 exit criterion (SPEC 12)."""
        return self.beats_spy and self.below_alarm and self.stable


def verdict(walk: WalkForward, sens: Sensitivity) -> Verdict:
    oos = walk.strategy.sharpe
    spy = walk.spy.sharpe if walk.spy is not None else math.nan
    finite = math.isfinite(oos)
    return Verdict(
        oos_sharpe=oos,
        spy_sharpe=spy,
        beats_spy=finite and math.isfinite(spy) and oos > spy,
        below_alarm=finite and oos <= SHARPE_ALARM,
        stable=not sens.fragile,
        alarm=finite and oos > SHARPE_ALARM,
    )


@dataclass(frozen=True)
class Validation:
    walk_forward: WalkForward
    sensitivity: Sensitivity
    bootstrap: Bootstrap  # out-of-sample returns
    bootstrap_full: Bootstrap  # full-period returns (second layer)
    leverage: pd.DataFrame
    experiments: pd.DataFrame  # each improvement turned off in turn
    full: BacktestResult  # the full-period run with the frozen parameters
    verdict: Verdict
    period: tuple[date, date]


def validate_strategy(
    data: MarketData,
    config: StrideConfig,
    settings: BacktestSettings,
    *,
    bootstrap_samples: int = BOOTSTRAP_SAMPLES,
    parameters: Sequence[tuple[str, str]] = SENSITIVITY_PARAMETERS,
    levels: Sequence[float] = FIXED_LEVERAGES,
) -> Validation:
    """The whole SPEC 9 protocol on one data set (`parameters` and `levels` are
    only narrowed by tests)."""
    walk = walk_forward(data, config, settings.initial_cash, settings.start, settings.end)
    full = run_backtest(data, config, settings)
    sens = sensitivity(data, config, settings, parameters, base_returns=full.returns)

    def boot(returns: pd.Series) -> Bootstrap:
        rf = daily_risk_free(data.risk_free, pd.DatetimeIndex(returns.index))
        return block_bootstrap(returns, rf, samples=bootstrap_samples)

    lev = leverage_comparison(data, config, settings, levels)
    rule = compare_variants(
        data, settings, improvement_variants(config) | experiment_variants(config, data)
    )
    return Validation(
        walk, sens, boot(walk.returns), boot(full.returns), lev, rule, full, verdict(walk, sens),
        (settings.start, settings.end),
    )  # fmt: skip


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return "n/a" if math.isnan(value) else f"{value:.3f}"
    return str(value)


def _table(frame: pd.DataFrame) -> list[str]:
    head = "| " + " | ".join(frame.columns) + " |"
    rule = "| " + " | ".join("---" for _ in frame.columns) + " |"
    body = ["| " + " | ".join(_fmt(v) for v in row) + " |" for row in frame.itertuples(index=False)]
    return [head, rule, *body]


def to_markdown(v: Validation, notes: Mapping[str, str] | None = None) -> str:
    """Human-readable summary for docs/results/."""
    w, s, b, verdict_ = v.walk_forward, v.sensitivity, v.bootstrap, v.verdict
    spy = w.spy
    stable = "yes" if verdict_.stable else "NO, fragile: " + ", ".join(s.fragile)
    lines = [
        "# STRIDE validation (SPEC 9)",
        "",
        f"Period {v.period[0]} to {v.period[1]}."
        " Generated by `python -m stride.backtest.validation`.",
        "",
        "## Phase 3 exit criterion",
        "",
        f"- Out-of-sample Sharpe: **{_fmt(verdict_.oos_sharpe)}**; SPY over the same days:"
        f" {_fmt(verdict_.spy_sharpe)} -> {'above' if verdict_.beats_spy else 'NOT above'} SPY",
        f"- Below the 1.5 alarm: {'yes' if verdict_.below_alarm else 'NO'}"
        + (" -- **AUDIT REQUIRED** (SPEC 9 quality alarm)" if verdict_.alarm else ""),
        f"- Sensitivity stable: {stable}",
        f"- **Verdict: {'PASS' if verdict_.passes else 'FAIL'}**",
        "",
        "## 1. Walk-forward (out-of-sample only)",
        "",
        f"CAGR {_fmt(w.strategy.cagr)}, volatility {_fmt(w.strategy.volatility)}, Sharpe"
        f" {_fmt(w.strategy.sharpe)}, max drawdown {_fmt(w.strategy.max_drawdown)}.",
    ]
    if spy is not None:
        lines.append(
            f"SPY on the same days: CAGR {_fmt(spy.cagr)}, Sharpe {_fmt(spy.sharpe)},"
            f" max drawdown {_fmt(spy.max_drawdown)}."
        )
    lines += ["", *_table(w.per_window), "", "## 2. Sensitivity (+-20%)", ""]
    lines += [f"Base Sharpe (full period): {_fmt(s.base_sharpe)}.", "", *_table(s.table)]
    lines += [
        "",
        f"## 3. Block bootstrap ({b.samples:,} resamples, {b.block}-day blocks, out-of-sample)",
        "",
        "| Metric | 2.5% | median | 97.5% |",
        "| --- | --- | --- | --- |",
        "| Sharpe | " + " | ".join(_fmt(x) for x in b.sharpe) + " |",
        "| Max drawdown | " + " | ".join(_fmt(x) for x in b.max_drawdown) + " |",
        "| Sharpe (full period) | " + " | ".join(_fmt(x) for x in v.bootstrap_full.sharpe) + " |",
        "| Max drawdown (full period) | "
        + " | ".join(_fmt(x) for x in v.bootstrap_full.max_drawdown)
        + " |",
        "",
        "## 5. Leverage comparison (full period)",
        "",
        *_table(v.leverage),
        "",
        "## 6. Improvements, each one turned off in turn (full period)",
        "",
        *_table(v.experiments),
    ]
    if notes:
        lines += ["", "## Notes", ""] + [f"- **{k}:** {text}" for k, text in notes.items()]
    return "\n".join(lines) + "\n"


# Command line: run everything on the stored prices.


def to_json(v: Validation) -> dict[str, object]:
    def clean(value: object) -> object:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    def records(frame: pd.DataFrame) -> list[dict[str, object]]:
        return [{str(k): clean(x) for k, x in row.items()} for row in frame.to_dict("records")]

    w = v.walk_forward
    return {
        "period": [v.period[0].isoformat(), v.period[1].isoformat()],
        "verdict": {k: clean(x) for k, x in vars(v.verdict).items()} | {"passes": v.verdict.passes},
        "walk_forward": {
            "strategy": {k: clean(x) for k, x in vars(w.strategy).items()},
            "spy": {k: clean(x) for k, x in vars(w.spy).items()} if w.spy else None,
            "windows": records(w.per_window),
        },
        "sensitivity": {
            "base_sharpe": clean(v.sensitivity.base_sharpe),
            "fragile": v.sensitivity.fragile,
            "runs": records(v.sensitivity.table),
        },
        "bootstrap": {
            "samples": v.bootstrap.samples,
            "block_days": v.bootstrap.block,
            "sharpe": list(v.bootstrap.sharpe),
            "max_drawdown": list(v.bootstrap.max_drawdown),
            "full_period": {
                "sharpe": list(v.bootstrap_full.sharpe),
                "max_drawdown": list(v.bootstrap_full.max_drawdown),
            },
        },
        "leverage": records(v.leverage),
        "experiments": records(v.experiments),
    }


def _notes(
    full: BacktestResult, data: MarketData, rate: pd.Series, calibrated: Sequence[run.Calibrated]
) -> dict[str, str]:
    etfs = ", ".join(
        f"{c.etf} spread {c.spread:.2%}, tracking {c.tracking_diff_per_year:.2%}/yr"
        for c in calibrated
    )
    return {
        "Leveraged ETFs": etfs or "none eligible (filters or missing bars): every asset at 1x",
        "Full-period run": "; ".join(run.format_summary(run.summarize(full, data.bars, rate))),
    }


def write_outputs(
    folder: Path,
    validation: Validation,
    notes: Mapping[str, str],
) -> None:
    """validation.md and validation.json in `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "validation.md").write_text(to_markdown(validation, notes), encoding="utf-8")
    (folder / "validation.json").write_text(
        json.dumps(to_json(validation), indent=2) + "\n", encoding="utf-8"
    )


def main(
    argv: Sequence[str] | None = None,
    rate_source: Callable[[], pd.Series] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m stride.backtest.validation")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--start", type=date.fromisoformat, help="default: data.history_start")
    parser.add_argument("--end", type=date.fromisoformat, help="default: last stored SPY bar")
    parser.add_argument(
        "--initial-cash", type=float, help="default: cash_flows.initial_deposit_usd"
    )
    parser.add_argument("--results", default="docs/results", help="output folder")
    parser.add_argument("--samples", type=int, default=BOOTSTRAP_SAMPLES)
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    root = config_path.parent
    config = effective_config(load_config(config_path))
    prices_db = (root / config.data.db_path).resolve()
    if not prices_db.exists():
        sys.stdout.write(f"error: no price database at {prices_db}\n")
        return 2
    rate = (rate_source or FredRateSource().fetch_rate)()
    source = connect(prices_db)
    try:
        last = last_price_date(source, "SPY")
        # Bars for the extra universe too, so its experiment can run on the same data.
        data, calibrated = run.load_market(
            source, with_extra_universe(config), rate, load_etf_facts()
        )
        prices = {s: series_for(source, s) for s in config.universe}
    finally:
        source.close()
    if last is None:
        sys.stdout.write("error: no SPY prices stored\n")
        return 2
    settings = BacktestSettings(
        args.start or config.data.history_start,
        args.end or last,
        args.initial_cash or config.cash_flows.initial_deposit_usd,
    )
    validation = validate_strategy(data, config, settings, bootstrap_samples=args.samples)
    full = validation.full
    results = (root / args.results).resolve()
    write_outputs(results, validation, _notes(full, data, rate, calibrated))
    out = connect(root / "data" / "validation.db")  # never the price database
    try:
        run.write_results(out, full, {s: f for s, f in prices.items() if not f.empty})
        rf = float(rate.iloc[-1]) if len(rate) else 0.0
        write_report(out, config, settings.start, settings.end, results / "backtest.html",
                     risk_free=rf)  # fmt: skip
    finally:
        out.close()
    for line in to_markdown(validation).splitlines()[4:10]:
        sys.stdout.write(line + "\n")
    sys.stdout.write(f"results written to {results}\n")
    return 0 if validation.verdict.passes else 1


if __name__ == "__main__":
    raise SystemExit(main())

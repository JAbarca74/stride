"""Walk-forward evaluation of the Monte Carlo forecast (docs/design/mc_forecast.md, section 8).

Reads the 30-minute history in `data/mc_research.db` (see `mc_data.py`), builds the slot
panel and runs, without look-ahead:

  A. forecast quality: for 15 forecasters (3 windows x null, drift 0.5, drift 1.0,
     analog 0.5, analog 1.0) the probabilities said at weekly evaluations against what
     happened. The forecaster is chosen on the selection period (2017-2022) only;
     the go/no-go gate is read once on the holdout period (2023 to the last bar).
  B. trading: 6 rules per decision horizon, hourly evaluations, costs and the fill rule
     of the design. The rule is chosen on the selection period, then read on the holdout.

Everything runs offline against the local database (no network). Usage:

    python scripts/research/mc_evaluate.py [--out reports/mc_eval] [--workers 11]
        [--doc docs/design/mc_backtest_results.md] [--skip-trading]
"""

# ruff: noqa: PLR0917, PLR0915

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from stride.backtest.costs import commission, spread_bps
from stride.config import load_config
from stride.data.intraday import slot_inputs
from stride.forecast.calibration import (
    brier_skill,
    brier_skill_interval,
    coverage,
    reliability_table,
)
from stride.forecast.params import DecisionParams, ForecastParams, WeightParams
from stride.forecast.returns import ReturnPanel, build_panel
from stride.forecast.summary import Forecast
from stride.forecast.walkforward import (
    CostModel,
    Rule,
    forecast_at,
    forecast_table,
    run_trading,
    trading_metrics,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIVERSE = [
    "QQQ",
    "IWM",
    "XLE",
    "XLV",
    "XLU",
    "EFA",
    "FXI",
    "INDA",
    "EWZ",
    "TLT",
    "GLD",
    "SLV",
    "CPER",
    "USO",
    "UNG",
    "DBA",
    "VNQ",
    "UUP",
    "IBIT",
    "ETHA",
]
GROUPS = {
    "crypto": ("IBIT", "ETHA"),
    "metals": ("GLD", "SLV", "CPER"),
    "us_equity": ("QQQ", "IWM"),
}
GROUP_CAPS = {"crypto": 0.30, "metals": 0.35, "us_equity": 0.40}
SELECTION_START = pd.Timestamp("2017-01-01")  # the first year is the warm-up of the windows
SELECTION_END = pd.Timestamp("2022-12-31")
HOLDOUT_START = pd.Timestamp("2023-01-01")
SEED = 20261112
ORDER_USD = 250.0  # typical order (25% of 1,000 USD): sets the cost the forecast subtracts
INITIAL_EQUITY = 1000.0
KILL_SWITCH_DD = 0.20
EVAL_EVERY_SESSIONS = 5
EVAL_SLOTS = (2, 6)  # 11:00-12:00 and the close
CHUNK = 40
MIN_BIN_CASES = 200
MAX_BIN_GAP = 0.05
COVERAGE_RANGE = (0.87, 0.93)
DECISION_HORIZONS = (35, 140)
WINDOWS = (63, 126, 252)
FORECASTERS = (("null", "drift", 0.0), ("drift05", "drift", 0.5), ("drift10", "drift", 1.0))
FORECASTERS += (("analog05", "analog", 0.5), ("analog10", "analog", 1.0))

_PANEL: ReturnPanel | None = None
_COSTS: CostModel | None = None


def build_costs() -> CostModel:
    """The cost model of `config.yaml` for the universe."""
    cfg = load_config(REPO_ROOT / "config.yaml")
    spreads = np.array([spread_bps(symbol, cfg) for symbol in UNIVERSE])
    return CostModel(spreads, lambda shares, price: commission(shares, price, cfg))


def _ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Nanoseconds since the epoch (UTC for tz-aware indexes), whatever the index unit."""
    naive = index.tz_convert("UTC").tz_localize(None) if index.tz is not None else index
    return naive.to_numpy(dtype="datetime64[ns]").astype("int64")


def save_panel(panel: ReturnPanel, path: Path) -> None:
    """Save the slot panel as an .npz so the workers can load it."""
    np.savez(
        path,
        symbols=np.array(panel.symbols),
        end=_ns(panel.end),
        day=_ns(panel.day),
        slot=panel.slot,
        open=panel.open,
        close=panel.close,
        ret=panel.ret,
    )


def load_panel(path: Path) -> ReturnPanel:
    """Load a panel saved by `save_panel`."""
    with np.load(path) as data:
        return ReturnPanel(
            tuple(str(s) for s in data["symbols"]),
            pd.DatetimeIndex(data["end"].astype("datetime64[ns]"), tz="UTC"),
            pd.DatetimeIndex(data["day"].astype("datetime64[ns]")),
            data["slot"],
            data["open"],
            data["close"],
            data["ret"],
        )


def init_worker(panel_path: str) -> None:
    """Load the panel and the costs once in each worker process."""
    global _PANEL, _COSTS  # noqa: PLW0603
    _PANEL = load_panel(Path(panel_path))
    _COSTS = build_costs()


def params_for(window: int, generator: str, shrink: float, paths: int = 2000) -> ForecastParams:
    """Simulation parameters for a window, generator and drift shrink."""
    return ForecastParams(
        window_days=window,
        generator="analog" if generator == "analog" else "drift",
        drift_shrink=shrink,
        n_paths=paths,
        n_paths_risk=1000,
        horizons=(1, 7, 35, 140),
    )


def _quality_task(task: tuple[str, ForecastParams, list[int]]) -> pd.DataFrame:
    name, params, rows = task
    assert _PANEL is not None and _COSTS is not None
    table = forecast_table(_PANEL, rows, _COSTS, order_usd=ORDER_USD, params=params, seed=SEED)
    table.insert(0, "forecaster", name)
    return table


def _forecast_task(task: tuple[ForecastParams, int, int]) -> Forecast | None:
    """One forecast, slimmed to what the decision and the weights read."""
    params, row, keep_horizon = task
    assert _PANEL is not None and _COSTS is not None
    forecast = forecast_at(_PANEL, row, _COSTS, order_usd=ORDER_USD, params=params, seed=SEED)
    if forecast is None:
        return None
    wanted = sorted({7, 35, keep_horizon})
    return Forecast(
        tuple(wanted),
        {h: forecast.by_horizon[h] for h in wanted},
        forecast.valid,
        {35: forecast.joint[35][:1000]},
    )


def split_period(panel: ReturnPanel) -> tuple[np.ndarray, np.ndarray]:
    """Boolean row masks: selection period (to 2022) and holdout (from 2023)."""
    days = panel.day
    return (
        np.asarray((days >= SELECTION_START) & (days <= SELECTION_END)),
        np.asarray(days >= HOLDOUT_START),
    )


def quality_rows(panel: ReturnPanel, every: int) -> list[int]:
    """Panel rows for stage A: every `every`-th session at the evaluation slots."""
    codes = pd.factorize(panel.day)[0]
    keep = (codes % every == 0) & np.isin(panel.slot, EVAL_SLOTS)
    return [int(r) for r in np.flatnonzero(keep)]


def run_quality(executor: ProcessPoolExecutor, panel: ReturnPanel, every: int) -> pd.DataFrame:
    """Stage A: every forecaster at every evaluation row, in parallel."""
    rows = quality_rows(panel, every)
    chunks = [rows[i : i + CHUNK] for i in range(0, len(rows), CHUNK)]
    tasks = []
    for window in WINDOWS:
        for key, generator, shrink in FORECASTERS:
            params = params_for(window, generator, shrink)
            tasks += [(f"{key}_{window}", params, chunk) for chunk in chunks]
    tables = list(executor.map(_quality_task, tasks))
    table = pd.concat([t for t in tables if not t.empty], ignore_index=True)
    table["day"] = panel.day[table["row"].to_numpy()]
    table["period"] = np.where(table["day"] <= SELECTION_END, "selection", "holdout")
    return table


def quality_metrics(table: pd.DataFrame) -> pd.DataFrame:
    """Per forecaster, period and horizon: skill against the null model and calibration."""
    rng = np.random.default_rng(0)
    out = []
    keys = ["row", "symbol", "horizon"]
    for window in WINDOWS:
        null = table[table["forecaster"] == f"null_{window}"].set_index(keys)
        for key, _, _ in FORECASTERS[1:]:
            name = f"{key}_{window}"
            mine = table[table["forecaster"] == name].set_index(keys)
            joined = mine.join(null[["p_up"]], rsuffix="_null", how="inner")
            for (period, horizon), group in joined.groupby(["period", "horizon"]):
                happened = (group["realized"] > 0).astype(float).to_numpy()
                predicted = group["p_up"].to_numpy()
                reference = group["p_up_null"].to_numpy()
                base = group.assign(h=happened).groupby("symbol")["h"].transform("mean").to_numpy()
                months = group["day"].dt.strftime("%Y-%m").to_numpy()
                skill = brier_skill_interval(predicted, happened, reference, months, rng)
                bins = reliability_table(predicted, happened)
                big = bins[bins["count"] >= MIN_BIN_CASES]
                net_happened = (group["realized"] > group["cost"]).astype(float).to_numpy()
                net_bins = reliability_table(group["p_net"].to_numpy(), net_happened)
                net_big = net_bins[net_bins["count"] >= MIN_BIN_CASES]
                out.append(
                    {
                        "forecaster": name,
                        "period": period,
                        "horizon": int(horizon),
                        "cases": len(group),
                        "skill_vs_null": skill.skill,
                        "skill_low": skill.low,
                        "skill_high": skill.high,
                        "skill_vs_base_rate": brier_skill(predicted, happened, base),
                        "max_bin_gap": float(big["gap"].abs().max()) if len(big) else np.nan,
                        "bins_checked": len(big),
                        "max_bin_gap_net": float(net_big["gap"].abs().max())
                        if len(net_big)
                        else np.nan,
                        "coverage90": coverage(
                            group["q05"].to_numpy(),
                            group["q95"].to_numpy(),
                            group["realized"].to_numpy(),
                        ),
                        "mean_p_up": float(predicted.mean()),
                        "freq_up": float(happened.mean()),
                    }
                )
    return pd.DataFrame(out)


def gate_row(row: pd.Series) -> dict[str, Any]:
    """The three gate tests for one forecaster and horizon on the holdout."""
    gap_ok = bool(row["bins_checked"] > 0 and row["max_bin_gap"] <= MAX_BIN_GAP)
    cov_ok = bool(COVERAGE_RANGE[0] <= row["coverage90"] <= COVERAGE_RANGE[1])
    skill_ok = bool(row["skill_low"] > 0)
    return {
        "forecaster": row["forecaster"],
        "horizon": int(row["horizon"]),
        "reliability_ok": gap_ok,
        "coverage_ok": cov_ok,
        "skill_ok": skill_ok,
        "passes": gap_ok and cov_ok and skill_ok,
        "max_bin_gap": float(row["max_bin_gap"]),
        "coverage90": float(row["coverage90"]),
        "skill_vs_null": float(row["skill_vs_null"]),
        "skill_low": float(row["skill_low"]),
        "skill_high": float(row["skill_high"]),
        "cases": int(row["cases"]),
    }


def choose_forecasters(metrics: pd.DataFrame) -> dict[int, str]:
    """For each decision horizon, the forecaster with the best skill on the selection period."""
    chosen = {}
    sel = metrics[metrics["period"] == "selection"]
    for horizon in DECISION_HORIZONS:
        part = sel[sel["horizon"] == horizon]
        chosen[horizon] = str(part.sort_values("skill_vs_null").iloc[-1]["forecaster"])
    return chosen


def parse_forecaster(name: str) -> ForecastParams:
    """Simulation parameters from a name such as `drift05_252`."""
    key, window = name.rsplit("_", 1)
    _, generator, shrink = next(f for f in FORECASTERS if f[0] == key)
    return params_for(int(window), generator, shrink)


def make_rules(horizon: int) -> list[Rule]:
    """The six trading rules of one decision horizon."""
    rules = []
    for p_enter in (0.55, 0.60, 0.65):
        for kappa in (0.25, 0.5):
            decision = DecisionParams(
                horizon=horizon, confirm_horizon=7, p_enter=p_enter, p_exit=round(p_enter - 0.10, 2)
            )
            weights = WeightParams(
                kappa=kappa,
                max_weight=0.25,
                groups=GROUPS,
                group_caps=GROUP_CAPS,
                risk_horizon=35,
                max_portfolio_loss=0.08,
                min_order_usd=150.0,
            )
            rules.append(Rule(f"H{horizon}_p{int(p_enter * 100)}_k{kappa}", decision, weights))
    return rules


def trade_period(
    executor: ProcessPoolExecutor,
    panel: ReturnPanel,
    mask: np.ndarray,
    params: ForecastParams,
    horizon: int,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Stage B for one period: all rules over the same stream of forecasts."""
    rows = np.flatnonzero(mask)
    start, stop = int(rows[0]), int(rows[-1]) + 1
    if limit is not None:
        stop = min(stop, start + limit)
    sliced = panel.window(start, stop)
    stream = executor.map(
        _forecast_task, [(params, r, horizon) for r in range(start, stop)], chunksize=8
    )
    forecasts = iter(stream)
    current: dict[str, Any] = {"row": -1, "value": None}

    def lookup(row: int) -> Forecast | None:
        """The next forecast of the stream (rows are asked for in order)."""
        while current["row"] < row:
            current["row"] += 1
            current["value"] = next(forecasts)
        return current["value"]  # type: ignore[no-any-return]

    rules = make_rules(horizon)
    results = run_trading(
        sliced,
        list(range(len(sliced))),
        lookup,
        rules,
        build_costs(),
        initial_equity=INITIAL_EQUITY,
        kill_switch_dd=KILL_SWITCH_DD,
    )
    out = []
    for result in results:
        metrics = trading_metrics(result, INITIAL_EQUITY)
        metrics["rule"] = result.rule.name
        metrics["calmar"] = metrics["cagr"] / max(metrics["max_drawdown"], 0.05)
        out.append(metrics)
    return out


def fmt(frame: pd.DataFrame, digits: int = 3) -> str:
    """A GitHub markdown table without extra dependencies."""
    columns = list(frame.columns)
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    for _, row in frame.iterrows():
        cells = [f"{v:.{digits}f}" if isinstance(v, float) else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    """Run stages A and B and write the outputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "mc_research.db")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "mc_eval")
    parser.add_argument("--workers", type=int, default=11)
    parser.add_argument("--skip-trading", action="store_true")
    parser.add_argument("--smoke", action="store_true", help="tiny run to test the pipeline")
    args = parser.parse_args()
    if args.smoke:
        args.out = args.out.with_name(args.out.name + "_smoke")
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()

    asof = datetime.now(UTC)
    conn = sqlite3.connect(args.db)
    grid, slots = slot_inputs(conn, UNIVERSE, asof)
    panel = build_panel(slots, grid)
    panel_path = args.out / "panel.npz"
    save_panel(panel, panel_path)
    selection, holdout = split_period(panel)
    print(f"panel: {len(panel)} slots, {panel.day[0].date()} to {panel.day[-1].date()}")
    summary: dict[str, Any] = {
        "run_utc": asof.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "panel_slots": len(panel),
        "first_day": str(panel.day[0].date()),
        "last_day": str(panel.day[-1].date()),
        "selection_end": str(SELECTION_END.date()),
        "holdout_start": str(HOLDOUT_START.date()),
    }

    with ProcessPoolExecutor(
        max_workers=args.workers, initializer=init_worker, initargs=(str(panel_path),)
    ) as executor:
        table = run_quality(executor, panel, 80 if args.smoke else EVAL_EVERY_SESSIONS)
        metrics = quality_metrics(table)
        metrics.to_csv(args.out / "quality_metrics.csv", index=False)
        print(f"stage A done in {time.time() - started:.0f} s, {len(table)} forecast lines")
        chosen = choose_forecasters(metrics)
        summary["chosen_forecasters"] = {str(k): v for k, v in chosen.items()}
        gates = []
        for horizon, name in chosen.items():
            row = metrics[
                (metrics["forecaster"] == name)
                & (metrics["horizon"] == horizon)
                & (metrics["period"] == "holdout")
            ].iloc[0]
            gates.append(gate_row(row))
        summary["gate_holdout"] = gates
        reliability = {}
        for horizon, name in chosen.items():
            part = table[
                (table["forecaster"] == name)
                & (table["horizon"] == horizon)
                & (table["period"] == "holdout")
            ]
            happened = (part["realized"] > 0).astype(float).to_numpy()
            bins = reliability_table(part["p_up"].to_numpy(), happened)
            reliability[str(horizon)] = bins.to_dict(orient="records")
        summary["reliability_holdout"] = reliability

        if not args.skip_trading:
            trading = []
            for horizon, name in chosen.items():
                params = replace(parse_forecaster(name), horizons=(1, 7, 35, 140))
                for label, mask in (("selection", selection), ("holdout", holdout)):
                    for entry in trade_period(
                        executor, panel, mask, params, horizon, 700 if args.smoke else None
                    ):
                        entry.update({"period": label, "forecaster": name, "horizon": horizon})
                        trading.append(entry)
                    print(f"trading H{horizon} {label} done at {time.time() - started:.0f} s")
            frame = pd.DataFrame(trading)
            frame.to_csv(args.out / "trading_all.csv", index=False)
            summary["trading_selection_best"] = {}
            for horizon in DECISION_HORIZONS:
                sel = frame[(frame["horizon"] == horizon) & (frame["period"] == "selection")]
                sel = sel[sel["killed"] == 0.0]
                best = sel.sort_values("calmar").iloc[-1] if len(sel) else None
                if best is not None:
                    summary["trading_selection_best"][str(horizon)] = str(best["rule"])

    summary["elapsed_s"] = round(time.time() - started)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps({k: v for k, v in summary.items() if k != "reliability_holdout"}, indent=2))


if __name__ == "__main__":
    main()

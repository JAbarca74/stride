"""The test of docs/design/dip_costs.md, section 4: DIP at 100 USD plus 20 USD a month.

Four runs of the backtest engine on the stored prices of the DIP instance:

- V0: as before 2026-11-16 (every order above 60% of equity is split; a missed
  entry labelled HOLD is skipped for cost like a resize);
- V1: a buy of a symbol not held is never skipped as a resize;
- V2: a buy without margin is limited by the equity, not split at 60% (option A of
  the design and the guard change of option C have the same effect on DIP, whose
  account never borrows);
- V3: V1 and V2 together (the code on main after this change).

The old behaviour is reproduced by replacing the two new rules for the duration of
a run, and checked against the golden curve that was recorded with the old engine.

python scripts/research/dip_costs.py --prices <copy of data/paper_dip.db>
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from stride.backtest import engine  # noqa: E402
from stride.backtest.engine import BacktestSettings, run_backtest  # noqa: E402
from stride.backtest.run import load_market  # noqa: E402
from stride.config import StrideConfig, effective_config, load_config  # noqa: E402
from stride.data.leveraged import load_etf_facts  # noqa: E402
from stride.data.sources import FredRateSource  # noqa: E402
from stride.data.store import last_price_date  # noqa: E402
from stride.risk import guard  # noqa: E402

START = date(2017, 1, 3)
OLD_GOLDEN_FINAL = 18226.690771041012  # tests/data/allocation_equity_golden.json before the change
RUNS = {"V0": (False, False), "V1": (True, False), "V2": (False, True), "V3": (True, True)}

_new_limit = guard.max_order_usd
_new_skip = engine.skip_costly_resizes


def _old_limit(side: guard.Side, equity: float, config: StrideConfig) -> float:
    return config.execution.max_order_pct_equity * equity


def _old_skip(orders, prices, config, targets=None):  # type: ignore[no-untyped-def]
    plain = [dataclasses.replace(o, opens=False) for o in orders]
    return _new_skip(plain, prices, config, targets)


@contextlib.contextmanager
def rules(entry_fix: bool, no_split: bool) -> Iterator[None]:
    """Run with or without each of the two rules of 2026-11-16."""
    engine.skip_costly_resizes = _new_skip if entry_fix else _old_skip  # type: ignore[assignment]
    guard.max_order_usd = _new_limit if no_split else _old_limit  # type: ignore[assignment]
    try:
        yield
    finally:
        engine.skip_costly_resizes = _new_skip  # type: ignore[assignment]
        guard.max_order_usd = _new_limit  # type: ignore[assignment]


def check_old_behaviour() -> float:
    """V0 must reproduce the golden curve recorded with the engine before the change."""
    from golden_backtest import golden_run  # noqa: PLC0415 - test helper

    with rules(entry_fix=False, no_split=False):
        final = float(golden_run().equity["equity"].iloc[-1])
    if final != OLD_GOLDEN_FINAL:
        raise SystemExit(f"V0 does not reproduce the old engine: {final} != {OLD_GOLDEN_FINAL}")
    return final


def measure(result: engine.BacktestResult) -> dict[str, float | int | bool]:
    equity = result.equity
    twr = (1.0 + result.returns.fillna(0.0)).cumprod()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    fills = result.fills
    commission = float(fills["commission"].sum()) if "commission" in fills else 0.0
    spread = float(fills["spread_cost"].sum()) if "spread_cost" in fills else 0.0
    deposits = float(result.cash_flows.clip(lower=0).sum())
    events = result.events
    kinds = events["type"] if "type" in events else pd.Series(dtype=str)
    return {
        "final_equity": float(equity["equity"].iloc[-1]),
        "deposits_usd": deposits,
        "twr_total": float(twr.iloc[-1] - 1.0),
        "twr_cagr": float(twr.iloc[-1] ** (1 / years) - 1.0),
        "max_drawdown": float((1.0 - twr / twr.cummax()).max()),
        "costs_usd": commission + spread,
        "commission_usd": commission,
        "costs_pct_of_deposits_per_year": (commission + spread) / deposits / years,
        "avg_cash_share": float((equity["cash"] / equity["equity"]).mean()),
        "orders": len(result.orders),
        "fills": len(fills),
        "resizes_skipped": int((kinds == "RESIZE_SKIPPED").sum()),
        "killed": bool((kinds == "KILL_SWITCH").any()),
        "years": years,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, required=True, help="a copy of paper_dip.db")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.paper_dip.yaml")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "results" / "dip_costs")
    args = parser.parse_args()

    golden = check_old_behaviour()
    config = effective_config(load_config(args.config))
    conn = sqlite3.connect(f"file:{args.prices.as_posix()}?mode=ro", uri=True)
    try:
        rate = FredRateSource().fetch_rate()
        data, _ = load_market(conn, config, rate, load_etf_facts())
        end = last_price_date(conn, "SPY")
    finally:
        conn.close()
    assert end is not None
    settings = BacktestSettings(START, end, config.cash_flows.initial_deposit_usd)
    table = {}
    for name, (entry_fix, no_split) in RUNS.items():
        with rules(entry_fix, no_split):
            table[name] = measure(run_backtest(data, config, settings))
    frame = pd.DataFrame(table).T
    args.out.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out / "dip_costs_runs.csv")
    summary = {"start": START.isoformat(), "end": end.isoformat(), "golden_v0": golden}
    (args.out / "dip_costs_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(frame.T)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

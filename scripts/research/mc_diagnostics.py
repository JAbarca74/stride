"""Context for reading the Monte Carlo results: what the assets did on their own.

Reads the slot panel saved by `mc_evaluate.py` (`reports/mc_eval/panel.npz`) and writes
`asset_stats.csv` (per asset and period: return, volatility, drawdown, how often it was up
after 5 and 20 sessions) and `universe_hold.csv` (an equal-weight buy-and-hold of the
universe, with the same costs, as a diagnostic of how much of a rule's result is simply
being invested; it is NOT a benchmark or a goal). No network. Usage:

    python scripts/research/mc_diagnostics.py [--panel PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from stride.backtest.costs import commission, spread_bps
from stride.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTION = ("selection", "2017-01-01", "2022-12-31")
HOLDOUT = ("holdout", "2023-01-01", "2099-12-31")
START_EQUITY = 1000.0
TRADING_DAYS = 252
MIN_SESSIONS = 60


def load_closes(path: Path) -> pd.DataFrame:
    """Close of each session (the last slot of the day), one column per asset."""
    with np.load(path) as data:
        symbols = [str(s) for s in data["symbols"]]
        day = pd.DatetimeIndex(data["day"].astype("datetime64[ns]"))
        close = pd.DataFrame(data["close"], columns=symbols)
    close["day"] = day
    return close.groupby("day").last()


def max_drawdown(curve: pd.Series) -> float:
    """Largest fall from a running peak, as a fraction."""
    return float((1.0 - curve / curve.cummax()).max())


def asset_stats(closes: pd.DataFrame) -> pd.DataFrame:
    """Return, volatility, drawdown and up-frequency per asset and period."""
    rows = []
    for period, start, end in (SELECTION, HOLDOUT):
        part = closes.loc[start:end]
        for symbol in closes.columns:
            series = part[symbol].dropna()
            if len(series) < MIN_SESSIONS:
                continue
            log = np.log(series).diff().dropna()
            ann_return = float(log.mean() * TRADING_DAYS)
            ann_vol = float(log.std() * np.sqrt(TRADING_DAYS))
            rows.append(
                {
                    "period": period,
                    "symbol": symbol,
                    "sessions": len(series),
                    "ann_log_return": ann_return,
                    "ann_vol": ann_vol,
                    "return_over_vol": ann_return / ann_vol if ann_vol > 0 else 0.0,
                    "max_drawdown": max_drawdown(series / series.iloc[0]),
                    "up_5d": float((series.shift(-5) > series).mean()),
                    "up_20d": float((series.shift(-20) > series).mean()),
                }
            )
    return pd.DataFrame(rows)


def universe_hold(closes: pd.DataFrame, root: Path) -> pd.DataFrame:
    """Equal weight over the assets listed at the start; an asset listed later stays out."""
    base = load_config(root / "config.yaml", env={})
    rows = []
    for period, start, end in (SELECTION, HOLDOUT):
        part = closes.loc[start:end]
        listed = [s for s in closes.columns if part[s].notna().iloc[0]]
        budget = START_EQUITY / len(listed)
        spent = 0.0
        value = pd.Series(0.0, index=part.index)
        for symbol in listed:
            price = float(part[symbol].iloc[0])
            fill = price * (1 + spread_bps(symbol, base) / 10_000.0)
            shares = budget / fill
            spent += commission(shares, fill, base)
            value += shares * part[symbol]
        curve = value - spent
        years = len(curve) / TRADING_DAYS
        rows.append(
            {
                "period": period,
                "assets": len(listed),
                "final_equity": float(curve.iloc[-1]),
                "total_return": float(curve.iloc[-1] / START_EQUITY - 1.0),
                "cagr": float((curve.iloc[-1] / START_EQUITY) ** (1 / years) - 1.0),
                "max_drawdown": max_drawdown(curve),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    """Write the per-asset statistics and the equal-weight hold."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--panel", type=Path, default=REPO_ROOT / "reports" / "mc_eval" / "panel.npz"
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "mc_eval")
    args = parser.parse_args()
    closes = load_closes(args.panel)
    args.out.mkdir(parents=True, exist_ok=True)
    stats = asset_stats(closes)
    hold = universe_hold(closes, REPO_ROOT)
    stats.round(4).to_csv(args.out / "asset_stats.csv", index=False)
    hold.round(4).to_csv(args.out / "universe_hold.csv", index=False)
    print(hold.round(3).to_string(index=False))


if __name__ == "__main__":
    main()

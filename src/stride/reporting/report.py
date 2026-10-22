"""Monthly HTML report (SPEC section 10, task T15).

Reads the database and writes one self-contained HTML file: equity curve against
SPY and 60/40 SPY/IEF (time-weighted, so deposits never look like gains), the
section 9 metrics table, total deposited against the balance, and the 3-year
correlation matrix with pairs above 0.7 flagged.
"""

from __future__ import annotations

import base64
import io
import math
import sqlite3
from dataclasses import asdict
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no display: render to PNG only

import matplotlib.pyplot as plt
import pandas as pd
from jinja2 import Environment, select_autoescape

from stride.backtest.metrics import (
    average_exposure,
    buy_and_hold_returns,
    deposits_vs_balance,
    sixty_forty_returns,
    summary,
    time_weighted_returns,
)
from stride.config import StrideConfig
from stride.data.store import net_cash_flows, read_equity, read_prices
from stride.reporting.correlations import correlation_matrix, flag_pairs

STOCK, BOND = "SPY", "IEF"

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STRIDE report {{ start }} to {{ end }}</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 16px;
       color: #1d1d1f; background: #fff; }
table { border-collapse: collapse; margin: 1rem 0; }
th, td { border: 1px solid #ccc; padding: 4px 10px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
img { max-width: 100%; }
.flag { color: #b00020; }
</style></head><body>
<h1>STRIDE report</h1>
<p>Period {{ start }} to {{ end }} ({{ days }} trading days). Mode: {{ mode }}.</p>

<h2>Equity curve</h2>
<img alt="Growth of $1, time-weighted" src="data:image/png;base64,{{ chart }}">

<h2>Metrics</h2>
<table>
<tr><th>Series</th>{% for name in metric_names %}<th>{{ name }}</th>{% endfor %}</tr>
{% for row in metrics %}<tr><td>{{ row.series }}</td>
{% for name in metric_keys %}<td>{{ row[name] }}</td>{% endfor %}</tr>
{% endfor %}</table>
<p>Average exposure (leverage used): {{ exposure }}</p>

<h2>Deposits and balance</h2>
<p>Total deposited: {{ deposited }}. Balance: {{ balance }}. Dollar gain: {{ gain }}.</p>

<h2>Correlation of daily returns (3 years)</h2>
{% if correlation is none %}<p>Not enough price history.</p>{% else %}
<table><tr><th></th>{% for s in correlation.columns %}<th>{{ s }}</th>{% endfor %}</tr>
{% for s, row in correlation.iterrows() %}<tr><td>{{ s }}</td>
{% for v in row %}<td>{{ "%.2f"|format(v) }}</td>{% endfor %}</tr>{% endfor %}</table>
{% if flagged %}<p class="flag">Pairs above 0.7:
{% for p in flagged %}{{ p.a }}/{{ p.b }} ({{ "%.2f"|format(p.correlation) }})
{%- if not loop.last %},
{% endif %}{% endfor %}</p>{% else %}<p>No pair above 0.7.</p>{% endif %}{% endif %}
</body></html>
"""

METRIC_KEYS = ["cagr", "volatility", "sharpe", "sortino", "max_drawdown", "calmar"]
METRIC_NAMES = ["CAGR", "Volatility", "Sharpe", "Sortino", "Max drawdown", "Calmar"]
_PERCENT = {"cagr", "volatility", "max_drawdown"}


def _fmt(key: str, value: float) -> str:
    if math.isnan(value):
        return "n/a"
    return f"{value:.2%}" if key in _PERCENT else f"{value:.2f}"


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _close(conn: sqlite3.Connection, symbol: str, start: date, end: date) -> pd.Series:
    prices = read_prices(conn, symbol, start, end)
    return prices["close"] if not prices.empty else pd.Series(dtype=float)


def _chart(curves: dict[str, pd.Series]) -> str:
    fig, ax = plt.subplots(figsize=(9, 4))
    for name, returns in curves.items():
        if not returns.empty:
            ax.plot((1 + returns).cumprod(), label=name)
    ax.set_ylabel("Growth of $1")
    ax.grid(alpha=0.3)
    ax.legend()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def build_report(
    conn: sqlite3.Connection,
    config: StrideConfig,
    start: date,
    end: date,
    risk_free: float = 0.0,
) -> str:
    """HTML for the period `start`..`end` (inclusive)."""
    equity = read_equity(conn, start, end)
    if len(equity) < 2:  # noqa: PLR2004
        raise ValueError("the report needs at least two days of equity")
    flows = net_cash_flows(conn)
    flows = flows[(flows.index >= pd.Timestamp(start)) & (flows.index <= pd.Timestamp(end))]

    strategy = time_weighted_returns(equity["equity"], flows)
    stock = _close(conn, STOCK, start, end)
    bond = _close(conn, BOND, start, end)
    curves = {
        "STRIDE": strategy,
        STOCK: buy_and_hold_returns(stock) if len(stock) > 1 else pd.Series(dtype=float),
        "60/40": sixty_forty_returns(stock, bond)
        if len(stock) > 1 and len(bond) > 1
        else pd.Series(dtype=float),
    }
    metrics = []
    for name, returns in curves.items():
        if returns.empty:
            continue
        values = asdict(summary(returns, risk_free))
        metrics.append({"series": name} | {k: _fmt(k, values[k]) for k in METRIC_KEYS})

    universe = {s: _close(conn, s, date(1900, 1, 1), end) for s in config.universe}
    universe = {s: c for s, c in universe.items() if len(c) > 1}
    correlation = correlation_matrix(universe, end) if len(universe) > 1 else None

    table = deposits_vs_balance(equity["equity"], flows)
    first_balance = float(equity["equity"].iloc[0])
    deposited = first_balance + float(table["deposited"].iloc[-1])
    balance = float(equity["equity"].iloc[-1])
    exposure = average_exposure(equity["gross_exposure"], equity["equity"])

    environment = Environment(autoescape=select_autoescape(default=True))
    return environment.from_string(_TEMPLATE).render(
        start=start,
        end=end,
        days=len(equity),
        mode=config.mode,
        chart=_chart(curves),
        metric_names=METRIC_NAMES,
        metric_keys=METRIC_KEYS,
        metrics=metrics,
        exposure="n/a" if math.isnan(exposure) else f"{exposure:.2f}x",
        deposited=_usd(deposited),
        balance=_usd(balance),
        gain=_usd(balance - deposited),
        correlation=correlation,
        flagged=flag_pairs(correlation) if correlation is not None else [],
    )


def write_report(
    conn: sqlite3.Connection,
    config: StrideConfig,
    start: date,
    end: date,
    path: str | Path,
    *,
    risk_free: float = 0.0,
) -> Path:
    """Write the HTML report to `path` and return it."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(conn, config, start, end, risk_free), encoding="utf-8")
    return out

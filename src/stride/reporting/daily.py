"""Daily report (SPEC section 10): one short HTML page after the 18:00 run.

What was done and what was not, for one trading day, read from the database:

- Status: OK, ATTENTION (something was not done), HALTED or KILL SWITCH, and
  NO RUN when the day has no equity row.
- Account: equity, the day's time-weighted return (deposits excluded), drawdown,
  leverage and the limit that bound it.
- Done: fills at today's open, orders sent for the next open, deposits, unit
  changes with the rule that fired.
- Not done: orders vetoed by the guard (with the reasons), orders rejected or
  cancelled by the broker, unit adds cut by the caps, symbols without data, and
  error events.
- Positions: units per asset after today's signals.

It is sent as a document through the Telegram bot already used for alerts.

    python -m stride.reporting.daily --date 2026-10-22            # write the HTML
    python -m stride.reporting.daily --date 2026-10-22 --send     # and send it
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, select_autoescape

from stride.config import load_config
from stride.data.store import connect, net_cash_flows, read_equity, read_events, read_table
from stride.reporting.alerts import AlertError, LogAlerter, alerter_from_env

OK = "OK"
ATTENTION = "ATTENTION"
HALTED = "HALTED"
KILLED = "KILL SWITCH"
NO_RUN = "NO RUN"

# Events that mean something did not happen as planned.
_PROBLEM_EVENTS = {"ORDER_REJECTED", "ORDER_CANCELLED", "ERROR", "DATA_FAILED", "RUN_FAILED"}
_STATE_EVENTS = {"KILL_SWITCH", "KILL_SWITCH_STILL_ACTIVE", "HALT", "HALT_STILL_ACTIVE"}


@dataclass(frozen=True)
class DailySummary:
    day: date
    mode: str
    status: str
    equity: float | None = None
    day_return: float | None = None
    drawdown: float | None = None
    leverage: float | None = None
    binding_limit: str | None = None
    deposited: float = 0.0
    done: list[str] = field(default_factory=list)
    not_done: list[str] = field(default_factory=list)
    positions: list[tuple[str, int, str]] = field(default_factory=list)


def _rows(conn: sqlite3.Connection, table: str, day: date) -> list[dict[str, Any]]:
    """Rows of `table` dated `day`, as plain dicts."""
    frame = read_table(conn, table)
    if frame.empty:
        return []
    today = frame[pd.DatetimeIndex(frame["date"]).date == day]
    return [{str(k): v for k, v in row.items()} for row in today.to_dict("records")]


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _done(day: date, conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    done, not_done = [], []
    for f in _rows(conn, "fills", day):
        done.append(
            f"Filled: {f['side']} {float(f['quantity']):.4f} {f['symbol']} at"
            f" {_usd(float(f['price']))} (commission {_usd(float(f['commission']))})"
        )
    for o in _rows(conn, "orders", day):
        switch = ", switch" if o["switch"] else ""
        what = f"{o['side']} {_usd(float(o['notional']))} {o['symbol']} ({o['rule']}{switch})"
        if o["status"] == "vetoed":
            not_done.append(f"Vetoed: {what} - {str(o['reasons']).replace('+', ', ')}")
        else:  # its fate shows in the next day's report (fill, rejection, cancel)
            done.append(f"Order sent for the next open: {what}")
    return done, not_done


def _signals(
    day: date, conn: sqlite3.Connection
) -> tuple[list[str], list[str], list[tuple[str, int, str]]]:
    done, not_done, positions = [], [], []
    for s in _rows(conn, "signals", day):
        symbol, rule = str(s["symbol"]), str(s["rule"])
        current, target = int(s["current_units"]), int(s["target_units"])
        if target != current:
            done.append(f"{symbol}: {current} -> {target} units ({rule})")
        if s["capped"]:
            not_done.append(f"{symbol}: unit add cut by the exposure caps ({rule})")
        if target > 0:
            positions.append((symbol, target, rule))
    return done, not_done, positions


def _events(day: date, conn: sqlite3.Connection) -> tuple[list[str], set[str]]:
    problems, states = [], set()
    for at, kind, detail in read_events(conn):
        if not at.startswith(day.isoformat()):
            continue
        if kind in _STATE_EVENTS:
            states.add(kind)
            problems.append(f"{kind}: {detail}")
        elif kind in _PROBLEM_EVENTS:
            problems.append(f"{kind.replace('_', ' ').capitalize()}: {detail}")
    return problems, states


def build_daily(conn: sqlite3.Connection, day: date, mode: str) -> DailySummary:
    """Everything the daily report shows for `day`."""
    equity = read_equity(conn, end=day)
    problems, states = _events(day, conn)
    if equity.empty or pd.Timestamp(day) != equity.index[-1]:
        return DailySummary(day, mode, NO_RUN, not_done=["No run recorded for this day", *problems])

    flows = net_cash_flows(conn)
    deposited = float(flows.get(pd.Timestamp(day), 0.0))
    values = equity["equity"].to_numpy(dtype=float)
    today = float(values[-1])
    day_return = None
    if len(values) > 1 and values[-2] > 0:
        day_return = (today - deposited) / float(values[-2]) - 1

    drawdown = leverage = None
    binding = None
    vetoed_in_risk: list[str] = []
    for row in _rows(conn, "risk_decisions", day)[-1:]:
        drawdown, leverage = float(row["drawdown"]), float(row["leverage"])
        binding = str(row["binding_limit"])
        vetoed_in_risk = json.loads(str(row["vetoes"]))

    done, not_done = _done(day, conn)
    signal_done, signal_not_done, positions = _signals(day, conn)
    if not any(line.startswith("Vetoed") for line in not_done):  # orders table not filled yet
        not_done += [f"Vetoed: {v.replace(':', ' ', 2).replace('+', ', ')}" for v in vetoed_in_risk]
    if deposited:
        done.append(f"Deposit received: {_usd(deposited)}")
    done = signal_done + done
    not_done = not_done + signal_not_done + problems

    if states & {"KILL_SWITCH", "KILL_SWITCH_STILL_ACTIVE"} or binding == "KILL_SWITCH":
        status = KILLED
    elif states & {"HALT", "HALT_STILL_ACTIVE"}:
        status = HALTED
    else:
        status = ATTENTION if not_done else OK
    return DailySummary(
        day, mode, status, today, day_return, drawdown, leverage, binding,
        deposited, done, not_done, positions,
    )  # fmt: skip


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STRIDE daily {{ s.day }}</title>
<style>
:root { --fg: #1d1d1f; --bg: #ffffff; --muted: #6e6e73; --ok: #1a7f37; --warn: #9a6700;
        --bad: #b00020; --line: #d0d7de; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #f0f0f0; --bg: #161b22; --muted: #9da7b3; --ok: #3fb950; --warn: #d29922;
          --bad: #f85149; --line: #30363d; }
}
body { font-family: system-ui, sans-serif; max-width: 640px; margin: 1.5rem auto;
       padding: 0 16px; color: var(--fg); background: var(--bg); line-height: 1.45; }
h1 { font-size: 1.25rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 1.25rem 0 .4rem; }
.sub { color: var(--muted); margin: 0; }
.status { display: inline-block; padding: 2px 10px; border-radius: 999px; font-weight: 600;
          border: 1px solid currentColor; margin-top: .5rem; }
.OK { color: var(--ok); } .ATTENTION { color: var(--warn); }
.HALTED, .KILL, .NO { color: var(--bad); }
table { border-collapse: collapse; width: 100%; }
td { padding: 3px 0; border-bottom: 1px solid var(--line); }
td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
ul { margin: 0; padding-left: 1.1rem; }
.none { color: var(--muted); }
</style></head><body>
<h1>STRIDE daily report</h1>
<p class="sub">{{ s.day }} &middot; mode {{ s.mode }}</p>
<span class="status {{ s.status.split()[0] }}">{{ s.status }}</span>

{% if s.equity is not none %}
<h2>Account</h2>
<table>
<tr><td>Equity</td><td>{{ usd(s.equity) }}</td></tr>
<tr><td>Day return (time-weighted)</td><td>{{ pct(s.day_return) }}</td></tr>
<tr><td>Drawdown from peak</td><td>{{ pct(s.drawdown) }}</td></tr>
<tr><td>Leverage (binding limit)</td>
<td>{{ num(s.leverage) }}x{% if s.binding_limit %} ({{ s.binding_limit }}){% endif %}</td></tr>
</table>
{% endif %}

<h2>Done</h2>
{% if s.done %}<ul>{% for line in s.done %}<li>{{ line }}</li>{% endfor %}</ul>
{% else %}<p class="none">Nothing to do today: no signal changed.</p>{% endif %}

<h2>Not done</h2>
{% if s.not_done %}<ul>{% for line in s.not_done %}<li>{{ line }}</li>{% endfor %}</ul>
{% else %}<p class="none">Everything planned went through.</p>{% endif %}

<h2>Positions (units)</h2>
{% if s.positions %}<table>{% for symbol, units, rule in s.positions %}
<tr><td>{{ symbol }} <span class="sub">{{ rule }}</span></td><td>{{ units }}</td></tr>
{% endfor %}</table>{% else %}<p class="none">No open positions.</p>{% endif %}
</body></html>
"""


def _pct(value: float | None) -> str:
    return "n/a" if value is None or math.isnan(value) else f"{value:+.2%}"


def _num(value: float | None) -> str:
    return "n/a" if value is None or math.isnan(value) else f"{value:.2f}"


def render_daily(summary: DailySummary) -> str:
    environment = Environment(autoescape=select_autoescape(default=True))
    return environment.from_string(_TEMPLATE).render(s=summary, usd=_usd, pct=_pct, num=_num)


def caption(summary: DailySummary) -> str:
    """One line for the Telegram message that carries the file."""
    parts = [f"STRIDE [{summary.mode}] {summary.day}: {summary.status}"]
    if summary.equity is not None:
        parts.append(f"equity {_usd(summary.equity)} ({_pct(summary.day_return)})")
    parts.append(f"{len(summary.done)} done, {len(summary.not_done)} not done")
    return "; ".join(parts)


def write_daily(conn: sqlite3.Connection, day: date, mode: str, path: str | Path) -> DailySummary:
    summary = build_daily(conn, day, mode)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_daily(summary), encoding="utf-8")
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stride.reporting.daily")
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", help="default: data.db_path next to the config")
    parser.add_argument("--out", help="default: reports/daily/<date>.html next to the config")
    parser.add_argument("--send", action="store_true", help="send it with the Telegram bot")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    root = config_path.parent
    config = load_config(config_path)
    db = Path(args.db) if args.db else root / config.data.db_path
    if not db.exists():
        sys.stdout.write(f"error: no database at {db}\n")
        return 2
    out = Path(args.out) if args.out else root / "reports" / "daily" / f"{args.date}.html"
    conn = connect(db)
    try:
        summary = write_daily(conn, args.date, config.mode, out)
    finally:
        conn.close()
    sys.stdout.write(f"{caption(summary)}\nwritten to {out}\n")
    if not args.send:
        return 0
    alerter = alerter_from_env(args.env_file)
    if isinstance(alerter, LogAlerter):
        sys.stdout.write("Telegram is not configured in .env; not sent\n")
        return 2
    try:
        alerter.send_document(out.name, out.read_bytes(), caption(summary))
    except AlertError as exc:
        sys.stdout.write(f"error: {exc}\n")
        return 1
    sys.stdout.write("sent\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

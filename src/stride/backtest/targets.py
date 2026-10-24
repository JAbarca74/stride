"""Allocation targets for one day, shared by the backtest and the paper run.

Pure: no network, no files, no clock. `Engine._allocate` and the daily job
(`run_daily.py`) both call `allocation_targets`, so the paper account holds what
the backtest would hold on the same day with the same inputs:

1. the pre-registered strategy (`strategy/allocation.target_weights`) sets
   weights from the closes up to and including day t and the risk-free rate the
   caller passes (the backtest passes the value known at t-1);
2. the weights times the leverage (`fixed_leverage` when given, else
   `experiments.allocation.leverage`), times the drawdown scaling when
   `drawdown_scaling` is enabled, become dollar exposures;
3. the leveraged-ETF route (`risk/leverage.choose_instruments`, with its
   hysteresis and minimum hold) maps each asset to one instrument and its cash.

Also here: `update_instruments` (held instruments and their hold days, and the
switches of the day) and the state the paper run rebuilds from its database
between days (`PaperState`, `rebuild_state`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import pandas as pd

from stride.config import StrideConfig
from stride.risk.kill_switch import TwrState, advance
from stride.risk.leverage import HeldInstrument, InstrumentChoice, choose_instruments
from stride.strategy.allocation import target_weights
from stride.strategy.rules import HOLD

ALLOCATE_IN = "ALLOCATE_IN"  # allocation: the asset enters the portfolio this period
ALLOCATE_OUT = "ALLOCATE_OUT"  # allocation: the asset leaves it


@dataclass(frozen=True)
class AllocationTargets:
    weights: dict[str, float]  # unlevered target weights per asset
    rules: dict[str, str]  # ALLOCATE_IN, ALLOCATE_OUT or HOLD per asset
    choices: dict[str, InstrumentChoice]  # one instrument per asset
    targets: dict[str, float]  # cash to hold per instrument symbol
    scale: float  # the drawdown scaling applied (1 when off)
    gross: float  # exposure / equity


def drawdown_scale(drawdown: float, config: StrideConfig) -> float:
    """Experiment (owner request, 2026-10-23: keep large drawdowns out). The
    leverage is multiplied by 1 until the drawdown of the time-weighted index
    reaches `dd_scale_start`, then falls linearly to `dd_scale_floor` at
    `dd_scale_full`, and stays there until the index recovers. The engine passes
    the drawdown from the peak of the last `dd_scale_window` days (all-time when
    0); see `Engine._scaling_drawdown`."""
    e = config.experiments
    if drawdown <= e.dd_scale_start:
        return 1.0
    if drawdown >= e.dd_scale_full:
        return e.dd_scale_floor
    share = (drawdown - e.dd_scale_start) / (e.dd_scale_full - e.dd_scale_start)
    return 1.0 - share * (1.0 - e.dd_scale_floor)


def allocation_rules(weights: Mapping[str, float], previous: Mapping[str, float]) -> dict[str, str]:
    """ALLOCATE_IN when an asset enters, ALLOCATE_OUT when it leaves, else HOLD."""
    rules = {}
    for asset in set(weights) | set(previous):
        was, now = previous.get(asset, 0.0) > 0, weights.get(asset, 0.0) > 0
        rules[asset] = ALLOCATE_IN if now and not was else ALLOCATE_OUT if was and not now else HOLD
    return rules


def allocation_targets(
    config: StrideConfig,
    closes: pd.DataFrame,
    risk_free: float,
    equity: float,
    *,
    scaling_drawdown: float,
    held: Mapping[str, HeldInstrument],
    eligible: Mapping[str, Mapping[int, str]],
    previous_weights: Mapping[str, float],
    fixed_leverage: float | None = None,
) -> AllocationTargets:
    """Pure. The allocation experiment's targets for the last day of `closes`.

    `closes` must stop at day t (the caller slices it: nothing after t is read
    here); `scaling_drawdown` is the drawdown the scaling uses (the peak of the
    last `dd_scale_window` days, or all-time); `held` the instruments held with
    their hold days; `eligible` the leveraged versions that pass the filters;
    `previous_weights` the weights of the last allocation, for the rules."""
    settings = config.experiments.allocation
    weights = target_weights(settings, closes, risk_free)
    level = fixed_leverage or settings.leverage
    scale = 1.0
    if "drawdown_scaling" in config.experiments.enabled:
        scale = drawdown_scale(scaling_drawdown, config)
    exposures = {a: w * equity * level * scale for a, w in weights.items() if w > 0}
    choices = choose_instruments(
        exposures,
        equity,
        eligible=eligible,
        current=held,
        switch=config.leverage_switch,
        max_etf_portfolio_leverage=config.risk.max_etf_portfolio_leverage,
        borrow=1.0,
    )
    gross = sum(c.exposure_usd for c in choices.values()) / equity if equity > 0 else 0.0
    return AllocationTargets(
        weights=weights,
        rules=allocation_rules(weights, previous_weights),
        choices=dict(choices),
        targets={c.symbol: c.cash_usd for c in choices.values()},
        scale=scale,
        gross=gross,
    )


def update_instruments(
    held: Mapping[str, HeldInstrument],
    chosen: Mapping[str, str],
    choices: Mapping[str, InstrumentChoice],
) -> tuple[dict[str, HeldInstrument], dict[str, str], list[tuple[str, str, str]]]:
    """Pure. The instruments held after today's choices, with their hold days
    (one more day when the multiple is unchanged, 0 after a switch), the symbol
    per asset, and the switches of the day as (asset, from, to)."""
    after: dict[str, HeldInstrument] = {}
    switches = []
    for asset, choice in choices.items():
        before = held.get(asset)
        if before is not None and before.multiple == choice.multiple:
            after[asset] = HeldInstrument(choice.multiple, before.days_held + 1)
            continue
        after[asset] = HeldInstrument(choice.multiple, 0)
        previous = chosen.get(asset)
        if previous is not None and previous != choice.symbol:
            switches.append((asset, previous, choice.symbol))
    return after, {asset: c.symbol for asset, c in choices.items()}, switches


# State the paper run keeps between days, rebuilt from its own database.


@dataclass(frozen=True)
class PaperState:
    twr: TwrState = field(default_factory=TwrState)
    twr_history: tuple[float, ...] = ()  # the index after each stored day, oldest first
    held: dict[str, HeldInstrument] = field(default_factory=dict)
    chosen: dict[str, str] = field(default_factory=dict)
    last_period: tuple[int, ...] | None = None  # the last allocation period traded
    weights: dict[str, float] = field(default_factory=dict)  # of that allocation


def twr_history(equity: pd.Series, flows: pd.Series) -> tuple[TwrState, tuple[float, ...]]:
    """Pure. The time-weighted index after each day of `equity` (deposits in
    `flows`, by date, never count as gains), through `risk/kill_switch.advance`.
    The first day starts the index at 1."""
    state = TwrState()
    history: list[float] = []
    previous: float | None = None
    for day, value in equity.sort_index().items():
        if previous is not None and previous > 0:
            state = advance(state, previous, float(value), float(flows.get(day, 0.0)))
        history.append(state.index)
        previous = float(value)
    return state, tuple(history)


def scaling_drawdown(state: TwrState, history: Sequence[float], window: int) -> float:
    """Pure. The drawdown the scaling uses, as `Engine._scaling_drawdown`: from
    the all-time peak when `window` is 0, else from the peak of the last `window`
    index values (today included)."""
    if not window or not history:
        return min(max(state.drawdown, 0.0), 1.0)
    peak = max(history[-window:])
    return min(max(1.0 - state.index / peak, 0.0), 1.0) if peak > 0 else 0.0


def allocation_period(day: pd.Timestamp, rebalance: str) -> tuple[int, ...]:
    """The period an allocation belongs to: (year, month), or the day itself
    with `rebalance: daily` (as `Engine._allocate`)."""
    if rebalance == "daily":
        return (day.year, day.month, day.day)
    return (day.year, day.month)


def held_from_history(
    rows: Iterable[tuple[pd.Timestamp, str, str, int]],
) -> tuple[dict[str, HeldInstrument], dict[str, str]]:
    """Pure. The held instruments and their hold days from the stored daily
    choices (date, asset, symbol, multiple), oldest first: replaying
    `update_instruments` day by day gives the same hold days as the backtest."""
    held: dict[str, HeldInstrument] = {}
    chosen: dict[str, str] = {}
    by_day: dict[pd.Timestamp, dict[str, InstrumentChoice]] = {}
    for day, asset, symbol, multiple in rows:
        by_day.setdefault(pd.Timestamp(day), {})[asset] = InstrumentChoice(
            asset, symbol, int(multiple), 0.0, 0.0, "stored"
        )
    for day in sorted(by_day):
        held, chosen, _ = update_instruments(held, chosen, by_day[day])
    return held, chosen


def rebuild_state(
    equity: pd.Series,
    flows: pd.Series,
    choices: Iterable[tuple[pd.Timestamp, str, str, int]],
    last_allocation: tuple[pd.Timestamp, Mapping[str, float]] | None,
    rebalance: str,
) -> PaperState:
    """Pure. The paper run's state from its tables: the TWR index history (from
    `equity` and `cash_flows`), the held instruments with their hold days (from
    the stored daily choices) and the last allocation period with its weights."""
    twr, history = twr_history(equity, flows)
    held, chosen = held_from_history(choices)
    period, weights = None, {}
    if last_allocation is not None:
        day, stored = last_allocation
        period = allocation_period(pd.Timestamp(day), rebalance)
        weights = {a: float(w) for a, w in stored.items() if math.isfinite(float(w))}
    return PaperState(twr, history, held, chosen, period, weights)

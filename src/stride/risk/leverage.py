"""Leverage: Kelly estimate, effective leverage and the leveraged-ETF route.

SPEC section 7 (steps 1, 2 and 2b) as decided in SPEC 1.3. HUMAN-REVIEWED MODULE:
read the whole file before merging. Pure functions, no I/O, no clock.

Step 1   kelly_estimate      L* = (mu - r_f) / sigma^2 from the unlevered strategy.
Step 2   effective_leverage  L = min(Kelly, vol target, max, capacity),
                             then x 0.5 while the drawdown deleveraging is active.
Step 2b  choose_instruments  reach the exposure with 2x/3x ETFs when there is no
                             margin, with hysteresis on switching.

Invalid inputs raise ValueError: the caller must not trade on them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stride.config import LeverageSwitchConfig, RiskConfig

TRADING_DAYS_PER_YEAR = 252
KELLY_WINDOW_DAYS = 3 * TRADING_DAYS_PER_YEAR  # SPEC 7, step 1: 3-year window
VOL_SPAN = 60  # SPEC 1.3: the same EWMA as vol60

KELLY = "KELLY"
VOL_TARGET = "VOL_TARGET"
MAX_LEVERAGE = "MAX_LEVERAGE"
CAPACITY = "CAPACITY"

_CASH_TOLERANCE = 1e-9  # relative slack when checking that cash fits in equity
_ZERO_VOL = 1e-12  # below this an annual volatility is float noise, i.e. zero
_MIN_EXPOSURE_USD = 1e-6  # smaller target exposures are treated as no position


# Step 1: Kelly estimate.


@dataclass(frozen=True)
class KellyEstimate:
    """L* and its inputs. `l_star` is None when there is no usable estimate."""

    l_star: float | None
    mu: float
    sigma: float
    days: int


def kelly_estimate(daily_returns: pd.Series, risk_free: float, min_days: int) -> KellyEstimate:
    """L* = (mu - r_f) / sigma^2 over the last 3 years of unlevered daily returns.

    mu and sigma are annualized. No estimate (None) with fewer than `min_days`
    returns, zero volatility, or L* <= 0: the Kelly term then becomes 1.0.
    """
    returns = daily_returns.dropna().iloc[-KELLY_WINDOW_DAYS:]
    days = len(returns)
    if days < 2:  # noqa: PLR2004
        return KellyEstimate(None, math.nan, math.nan, days)
    mu = float(returns.mean()) * TRADING_DAYS_PER_YEAR
    sigma = float(returns.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
    if days < min_days or not sigma > _ZERO_VOL:
        return KellyEstimate(None, mu, sigma, days)
    l_star = (mu - risk_free) / sigma**2
    return KellyEstimate(l_star if l_star > 0 else None, mu, sigma, days)


# Step 2: forward-looking volatility (SPEC 1.3).


def ewma_covariance(returns: pd.DataFrame, span: int = VOL_SPAN) -> pd.DataFrame:
    """Zero-mean EWMA covariance of daily returns at the last date (daily units).

    Weights (1 - a)^k with a = 2 / (span + 1), normalized; the same weighting as
    `indicators.ewma_volatility`, so a single asset gives the same volatility.
    Needs at least `span` complete rows.
    """
    clean = returns.dropna()
    if len(clean) < span:
        raise ValueError(f"need at least {span} days of returns for every symbol")
    alpha = 2 / (span + 1)
    weights = (1 - alpha) ** np.arange(len(clean) - 1, -1, -1)
    weights /= weights.sum()
    values = clean.to_numpy(dtype=float)
    cov = (values * weights[:, None]).T @ values
    return pd.DataFrame(cov, index=clean.columns, columns=clean.columns)


def forward_volatility(
    weights: Mapping[str, float], returns: pd.DataFrame, span: int = VOL_SPAN
) -> float:
    """Annualized sigma = sqrt(w' S w * 252) for today's unlevered target weights.

    With no positions the volatility is 0. Every symbol with a weight needs a
    returns column with at least `span` days.
    """
    held = {symbol: w for symbol, w in weights.items() if w != 0}
    if not held:
        return 0.0
    missing = [symbol for symbol in held if symbol not in returns.columns]
    if missing:
        raise ValueError(f"no returns for {missing}")
    symbols = list(held)
    cov = ewma_covariance(returns[symbols], span).to_numpy()
    w = np.array([held[s] for s in symbols], dtype=float)
    variance = float(w @ cov @ w)
    return math.sqrt(max(variance, 0.0) * TRADING_DAYS_PER_YEAR)


# Step 2: effective leverage.


@dataclass(frozen=True)
class LeverageDecision:
    leverage: float
    binding_limit: str
    limits: dict[str, float]  # every applicable limit, before deleveraging
    delever_active: bool
    borrow: float  # L_borrow: cash usable per dollar of equity (1.0 = no margin)


def effective_leverage(
    kelly: KellyEstimate,
    *,
    sigma: float,
    broker_leverage: float,
    equity: float,
    drawdown: float,
    delever_active: bool,
    risk: RiskConfig,
) -> LeverageDecision:
    """L = min of the applicable limits, then x 0.5 while deleveraging (SPEC 1.3/1.4).

    - Kelly term: kelly_fraction * L*, or 1.0 when there is no estimate.
    - Vol target: max_portfolio_vol / sigma; not applicable when sigma is 0.
    - Absolute cap: max_leverage.
    - Capacity (SPEC 1.4): L_borrow x max_etf_portfolio_leverage. L_borrow is the
      broker's margin leverage only when `allow_margin` is on and equity is at
      least margin_min_equity; otherwise 1.0 (no loan). The real account runs
      with allow_margin off, so its capacity is 2.5x whatever the balance and
      the maximum loss is what was deposited.
    - `drawdown` is a positive magnitude of the time-weighted index. Deleveraging
      starts when it exceeds dd_delever_at and lasts until a new high (drawdown 0).
    """
    for name, value in {"sigma": sigma, "broker_leverage": broker_leverage,
                        "equity": equity, "drawdown": drawdown}.items():  # fmt: skip
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number")
    if drawdown > 1:
        raise ValueError("drawdown is a fraction between 0 and 1")

    margin = risk.allow_margin and equity >= risk.margin_min_equity
    if margin and broker_leverage <= 0:
        raise ValueError("broker_leverage must be positive with a margin account")
    borrow = broker_leverage if margin else 1.0

    # Fail safe: an L* that is not a positive finite number counts as no estimate.
    l_star = kelly.l_star
    usable = l_star is not None and math.isfinite(l_star) and l_star > 0
    limits: dict[str, float] = {
        KELLY: risk.kelly_fraction * l_star if usable and l_star is not None else 1.0,
        MAX_LEVERAGE: risk.max_leverage,
        CAPACITY: borrow * risk.max_etf_portfolio_leverage,
    }
    if sigma > 0:
        limits[VOL_TARGET] = risk.max_portfolio_vol / sigma

    binding = min(limits, key=lambda name: limits[name])
    leverage = limits[binding]

    # A new high (drawdown 0) ends the deleveraging; otherwise it starts beyond
    # dd_delever_at and stays on.
    active = False if drawdown == 0 else delever_active or drawdown > risk.dd_delever_at
    if active:
        leverage *= 0.5
    return LeverageDecision(leverage, binding, limits, active, borrow)


# Step 2b: leveraged-ETF route (no margin).


@dataclass(frozen=True)
class HeldInstrument:
    """The instrument an asset is held in today and for how many trading days."""

    multiple: int
    days_held: int


@dataclass(frozen=True)
class InstrumentChoice:
    asset: str
    symbol: str
    multiple: int
    exposure_usd: float  # underlying exposure reached
    cash_usd: float  # dollars invested in `symbol`: exposure / multiple
    reason: str


def _largest_multiple(eligible: Mapping[int, str], cap: int) -> int:
    allowed = [m for m in eligible if m <= cap]
    return max(allowed, default=1)


def _symbol(asset: str, multiple: int, eligible: Mapping[int, str]) -> str:
    return asset if multiple == 1 else eligible[multiple]


def _route(k: float, switch: LeverageSwitchConfig) -> str:
    if k >= switch.enter_k:
        return "leveraged"
    if k <= switch.exit_k:
        return "normal"
    return "keep"


def _usable(held: HeldInstrument | None, eligible: Mapping[int, str]) -> bool:
    return held is not None and held.multiple in {1, *eligible}


def _route_multiples(
    route: str,
    active: Mapping[str, float],
    cash_available: float,
    eligible: Mapping[str, Mapping[int, str]],
    current: Mapping[str, HeldInstrument],
) -> tuple[dict[str, int], dict[str, str]]:
    if route == "normal":
        return dict.fromkeys(active, 1), dict.fromkeys(active, "NORMAL")
    if route == "keep":
        kept = {
            asset: current[asset].multiple if _usable(current.get(asset), eligible.get(asset, {}))
            else 1
            for asset in active
        }  # fmt: skip
        return kept, dict.fromkeys(active, "HYSTERESIS_KEEP")
    multiples: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for m in (2, 3):
        multiples = {a: _largest_multiple(eligible.get(a, {}), m) for a in active}
        reasons = dict.fromkeys(active, f"LEVERAGED_{m}X_ROUTE")
        if sum(active[a] / multiples[a] for a in active) <= cash_available * (1 + _CASH_TOLERANCE):
            break
    return multiples, reasons


def choose_instruments(
    exposures: Mapping[str, float],
    equity: float,
    *,
    eligible: Mapping[str, Mapping[int, str]],
    current: Mapping[str, HeldInstrument],
    switch: LeverageSwitchConfig,
    max_etf_portfolio_leverage: float,
    borrow: float = 1.0,
) -> dict[str, InstrumentChoice]:
    """Pick one instrument per asset so the exposure fits in the equity (SPEC 7, 2b).

    `exposures` are the target dollar exposures to each underlying (already
    multiplied by L). `eligible` maps an asset to the leveraged versions that pass
    the filters, e.g. {"SPY": {2: "SSO", 3: "UPRO"}, "TLT": {3: "TMF"}}.

    1. k = total exposure / equity. Use leveraged versions only if k >= enter_k,
       go back to normal ETFs only if k <= exit_k; in between keep each asset's
       current instrument.
    2. On the leveraged route, exposure is capped at max_etf_portfolio_leverage x
       equity, and the smallest common multiple m (2, then 3) whose total cash
       fits in the equity is used; each asset takes its largest eligible multiple
       <= m (1x if it has none).
    3. An asset keeps its current instrument for min_hold_days unless the
       position is closed.
    4. If the cash still does not fit, every position is scaled down.

    `borrow` is L_borrow from `effective_leverage` (1.0 without margin): the cash
    available is borrow x equity, and the exposure cap is max_etf_portfolio_leverage
    x that cash (SPEC 1.4).
    """
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("equity must be positive")
    if not math.isfinite(borrow) or borrow < 1:
        raise ValueError("borrow must be at least 1.0")
    cash_available = borrow * equity
    if any(not math.isfinite(e) or e < 0 for e in exposures.values()):
        raise ValueError("exposures must be finite and non-negative (long only)")
    active = {asset: float(e) for asset, e in exposures.items() if e > _MIN_EXPOSURE_USD}
    route = _route(sum(active.values()) / cash_available, switch)

    total = sum(active.values())
    if route == "leveraged" and total > max_etf_portfolio_leverage * cash_available:
        scale = max_etf_portfolio_leverage * cash_available / total
        active = {asset: e * scale for asset, e in active.items()}

    multiples, reasons = _route_multiples(route, active, cash_available, eligible, current)

    for asset in active:
        held = current.get(asset)
        if (
            held is not None
            and held.multiple != multiples[asset]
            and held.days_held < switch.min_hold_days
            and _usable(held, eligible.get(asset, {}))
        ):
            multiples[asset], reasons[asset] = held.multiple, "MIN_HOLD"

    cash = sum(active[a] / multiples[a] for a in active)
    if cash > cash_available * (1 + _CASH_TOLERANCE):
        scale = cash_available / cash
        active = {asset: e * scale for asset, e in active.items()}
        reasons = {asset: reason + "+SCALED" for asset, reason in reasons.items()}
    active = {asset: e for asset, e in active.items() if e > 0}  # nothing underflows to a 0 lot

    return {
        asset: InstrumentChoice(
            asset=asset,
            symbol=_symbol(asset, multiples[asset], eligible.get(asset, {})),
            multiple=multiples[asset],
            exposure_usd=active[asset],
            cash_usd=active[asset] / multiples[asset],
            reason=reasons[asset],
        )
        for asset in active
    }

"""Risk-based weights and brakes (docs/design/mc_risk_allocation.md, sections 3 and 4).

Three allocators that use no forecast of direction, the limits of the current
instance, and two brakes. Pure: no network, clock or files.

An allocator returns weights that sum to 1 over the assets it is given. Everything
after it (`apply_limits`, the brakes, the loss cap) can only lower a weight; what is
taken away stays in cash.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from stride.forecast.params import WeightParams
from stride.forecast.returns import FloatArray
from stride.forecast.weights import portfolio_loss

Allocator = Literal["EW", "IV", "ERC"]
Brake = Literal["none", "trend", "dd"]
ALLOCATORS: tuple[Allocator, ...] = ("EW", "IV", "ERC")
BRAKES: tuple[Brake, ...] = ("none", "trend", "dd")

BoolArray = np.ndarray[tuple[int], np.dtype[np.bool_]]

SIGMA_FLOOR = 1e-12
ERC_TOLERANCE = 1e-10
ERC_MAX_SWEEPS = 500


@dataclass(frozen=True)
class RiskRule:
    """One variant: how to weigh, which brake, and the limits."""

    name: str
    allocator: Allocator
    brake: Brake
    limits: WeightParams = field(default_factory=WeightParams)
    trend_days: int = 200  # an asset is held only above its average of this many closes
    dd_floor: float = 0.15  # the drawdown brake reaches zero exposure at this drawdown

    def __post_init__(self) -> None:
        if self.allocator not in ALLOCATORS:
            raise ValueError(f"unknown allocator: {self.allocator}")
        if self.brake not in BRAKES:
            raise ValueError(f"unknown brake: {self.brake}")
        if self.trend_days < 2:  # noqa: PLR2004
            raise ValueError("trend_days must be at least 2")
        if not 0.0 < self.dd_floor <= 1.0:
            raise ValueError("dd_floor must be in (0, 1]")


def equal_weights(count: int) -> FloatArray:
    """`EW`: the null allocation."""
    if count < 1:
        return np.zeros(0)
    return np.full(count, 1.0 / count)


def inverse_vol_weights(sigma: FloatArray) -> FloatArray:
    """`IV`: weights proportional to 1 / `sigma` [n], summing to 1."""
    if len(sigma) == 0:
        return np.zeros(0)
    if not np.isfinite(sigma).all() or (sigma < 0).any():
        raise ValueError("sigma must be finite and not negative")
    inverse = 1.0 / np.maximum(sigma, SIGMA_FLOOR)
    out: FloatArray = inverse / inverse.sum()
    return out


def erc_weights(covariance: FloatArray) -> FloatArray:
    """`ERC`: weights whose risk contributions `w_i (C w)_i` are all equal, summing to 1.

    Cyclical coordinate descent on `0.5 w'Cw - (1/n) sum(log w_i)`, whose minimum has
    equal risk contributions; the result is then rescaled to sum to 1 (contributions
    stay equal under a rescale). `covariance` [n, n] must be symmetric with a positive
    diagonal.
    """
    size = covariance.shape[0]
    if covariance.shape != (size, size):
        raise ValueError("covariance must be square")
    if size == 0:
        return np.zeros(0)
    if not np.isfinite(covariance).all():
        raise ValueError("covariance must be finite")
    diagonal = np.maximum(np.diag(covariance), SIGMA_FLOOR**2)
    budget = 1.0 / size
    weights = 1.0 / np.sqrt(diagonal)
    weights /= weights.sum()
    for _ in range(ERC_MAX_SWEEPS):
        previous = weights.copy()
        for i in range(size):
            cross = float(covariance[i] @ weights - covariance[i, i] * weights[i])
            weights[i] = (-cross + np.sqrt(cross * cross + 4.0 * diagonal[i] * budget)) / (
                2.0 * diagonal[i]
            )
        if np.abs(weights - previous).max() <= ERC_TOLERANCE * weights.max():
            break
    out: FloatArray = weights / weights.sum()
    return out


def allocate(allocator: Allocator, joint: FloatArray) -> FloatArray:
    """Weights summing to 1 from the joint simulated simple returns `joint` [P, n]."""
    count = joint.shape[1]
    if allocator == "EW":
        return equal_weights(count)
    if allocator == "IV":
        return inverse_vol_weights(joint.std(axis=0))
    if allocator == "ERC":
        if count == 1:
            return np.ones(1)
        return erc_weights(np.cov(joint, rowvar=False))
    raise ValueError(f"unknown allocator: {allocator}")


def risk_contributions(weights: FloatArray, covariance: FloatArray) -> FloatArray:
    """Share of the portfolio variance that comes from each asset (sums to 1)."""
    marginal = covariance @ weights
    total = float(weights @ marginal)
    if total <= 0:
        return np.zeros(len(weights))
    out: FloatArray = weights * marginal / total
    return out


def trend_ok(closes: FloatArray, days: int) -> BoolArray:
    """Assets whose last close is above the average of their last `days` closes.

    `closes` [T, n] ends at the evaluation; an asset with fewer than `days` finite
    closes is not in a measurable trend and is not held.
    """
    if closes.shape[0] < days:
        return np.zeros(closes.shape[1], dtype=np.bool_)
    window = closes[-days:]
    known = np.isfinite(window).all(axis=0)
    average = np.where(known, np.where(np.isfinite(window), window, 0.0).mean(axis=0), np.inf)
    out: BoolArray = known & (window[-1] > average)
    return out


def drawdown_scale(drawdown: float, floor: float) -> float:
    """The `dd` brake: 1 with no drawdown, falling linearly to 0 at `floor`."""
    if floor <= 0:
        raise ValueError("floor must be positive")
    return float(min(1.0, max(0.0, 1.0 - max(drawdown, 0.0) / floor)))


def loss_scale(weights: FloatArray, joint: FloatArray, quantile: float, max_loss: float) -> float:
    """Factor in (0, 1] that brings the simulated portfolio loss down to `max_loss`.

    The loss exceeded in `quantile` of the joint paths is linear in the size of the
    portfolio, so scaling every weight by the result is enough.
    """
    loss = portfolio_loss(weights, joint, quantile)
    if loss <= max_loss:
        return 1.0
    return float(max_loss / loss)


def apply_limits(
    weights: FloatArray,
    symbols: Sequence[str],
    *,
    equity: float,
    params: WeightParams,
) -> FloatArray:
    """Per-asset cap, group caps, total cap and minimum order. Only lowers weights.

    A target below `min_order_usd` is dropped to zero, and what it held stays in
    cash: it is not handed to the other assets.
    """
    if len(weights) != len(symbols):
        raise ValueError("one weight per symbol is needed")
    if not np.isfinite(weights).all() or (weights < 0).any():
        raise ValueError("weights must be finite and not negative")
    if equity <= 0:
        return np.zeros(len(weights))
    out = np.minimum(weights, params.max_weight)
    for group, cap in params.group_caps.items():
        members = np.array([symbol in params.groups[group] for symbol in symbols], dtype=np.bool_)
        total = float(out[members].sum())
        if total > cap:
            out[members] *= cap / total
    total = float(out.sum())
    if total > params.max_total:
        out *= params.max_total / total
    limited: FloatArray = np.where(out * equity < params.min_order_usd, 0.0, out)
    return limited

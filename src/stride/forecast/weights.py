"""Dynamic weights and risk limits (design section 6).

Weights come from the simulation: a fraction of Kelly, edge over variance, then
caps per asset and per group, a cap on total exposure, a cap on the simulated
portfolio loss, and a minimum order size. Every step can only lower a weight.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from stride.forecast.params import WeightParams
from stride.forecast.returns import FloatArray
from stride.forecast.summary import Forecast

BoolArray = np.ndarray[tuple[int], np.dtype[np.bool_]]


def portfolio_loss(weights: FloatArray, joint: FloatArray, quantile: float) -> float:
    """Loss (positive fraction of equity) exceeded in `quantile` of the joint paths."""
    outcome = joint @ weights
    return float(max(0.0, -np.quantile(outcome, quantile)))


def target_weights(
    forecast: Forecast,
    long: BoolArray,
    symbols: Sequence[str],
    *,
    equity: float,
    horizon: int,
    params: WeightParams,
) -> FloatArray:
    """Target weight per asset (fraction of equity), 0 for assets that are not long.

    `long` marks the assets held after the decision; `horizon` is the decision
    horizon whose edge and sigma size the positions.
    """
    main = forecast.by_horizon[horizon]
    size = len(symbols)
    if equity <= 0:
        return np.zeros(size)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = params.kappa * main.edge / main.sigma**2
    active = long & np.isfinite(raw) & (raw > 0)
    joint = forecast.joint[params.risk_horizon]
    members = {
        group: np.array([symbol in params.groups[group] for symbol in symbols])
        for group in params.group_caps
    }
    while True:
        weights = np.zeros(size)
        weights[active] = np.minimum(raw[active], params.max_weight)
        for group, cap in params.group_caps.items():
            total = weights[members[group]].sum()
            if total > cap:
                weights[members[group]] *= cap / total
        total = weights.sum()
        if total > params.max_total:
            weights *= params.max_total / total
        loss = portfolio_loss(weights, joint, params.risk_quantile)
        if loss > params.max_portfolio_loss:
            weights *= params.max_portfolio_loss / loss
        small = active & (weights * equity < params.min_order_usd)
        if not small.any():
            return weights
        # Too many or too weak signals for the minimum order: drop the weakest
        # one and size the rest again, so capital concentrates in the best.
        weakest = np.flatnonzero(active)[np.argmin(raw[active])]
        active = active.copy()
        active[weakest] = False

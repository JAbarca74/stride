"""Entry and exit rule with hysteresis (design section 5). Long or cash, per asset."""

from __future__ import annotations

import numpy as np

from stride.forecast.params import DecisionParams
from stride.forecast.summary import Forecast

MC_ENTER = "MC_ENTER"
MC_EXIT = "MC_EXIT"
MC_HOLD = "MC_HOLD"

BoolArray = np.ndarray[tuple[int], np.dtype[np.bool_]]


def decide(forecast: Forecast, held: BoolArray, params: DecisionParams) -> list[str]:
    """One action per asset.

    Flat: enter when the probability of a gain above costs at the decision horizon
    reaches `p_enter`, the net expected gain is at least `k_cost` times the cost,
    and the 1-day horizon does not point down. Long: exit when the probability of
    any gain falls to `p_exit` or the net expected gain is gone. An asset without a
    forecast (NaN) is never entered, and a held one is exited: without a forecast
    there is no reason to hold.
    """
    main = forecast.by_horizon[params.horizon]
    confirm = forecast.by_horizon[params.confirm_horizon]
    cost = main.mean - main.edge
    actions: list[str] = []
    for asset in range(len(held)):
        known = bool(forecast.valid[asset]) and np.isfinite(main.p_net[asset])
        if held[asset]:
            leave = not known or main.p_up[asset] <= params.p_exit or main.edge[asset] <= 0.0
            actions.append(MC_EXIT if leave else MC_HOLD)
            continue
        enter = (
            known
            and main.p_net[asset] >= params.p_enter
            and main.edge[asset] >= params.k_cost * cost[asset]
            and main.edge[asset] > 0.0
            and confirm.p_up[asset] >= params.p_confirm
        )
        actions.append(MC_ENTER if enter else MC_HOLD)
    return actions


def long_after(actions: list[str], held: BoolArray) -> BoolArray:
    """Which assets are long once `actions` are applied."""
    out = held.copy()
    for asset, action in enumerate(actions):
        if action == MC_ENTER:
            out[asset] = True
        elif action == MC_EXIT:
            out[asset] = False
    return out

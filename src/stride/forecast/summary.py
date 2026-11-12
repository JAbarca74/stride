"""From a window of returns to the numbers the decision reads (design section 4)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from stride.forecast.params import SLOTS_PER_DAY, ForecastParams
from stride.forecast.returns import Filtered, FloatArray, IntArray, standardize
from stride.forecast.simulate import analog_weights, simulate

TAIL = 0.05


@dataclass(frozen=True)
class Summary:
    """Simulated simple-return statistics per asset for one horizon. NaN = no forecast."""

    mean: FloatArray
    sigma: FloatArray
    es5: FloatArray  # mean of the worst 5% of the paths
    p_up: FloatArray  # share of paths above 0
    p_net: FloatArray  # share of paths above the round-trip cost
    edge: FloatArray  # mean minus the round-trip cost
    q05: FloatArray  # 5% and 95% quantiles: the simulated 90% interval
    q95: FloatArray


@dataclass(frozen=True)
class Forecast:
    """All horizons for one evaluation."""

    horizons: tuple[int, ...]
    by_horizon: dict[int, Summary]
    valid: np.ndarray[tuple[int], np.dtype[np.bool_]]  # assets with a full window
    # Joint simple returns [paths, N] per horizon, same history in each path for
    # every asset: the input of the portfolio risk cap. Invalid assets are 0.
    joint: dict[int, FloatArray]


def summarize(paths: FloatArray, cost: FloatArray) -> Summary:
    """Statistics of cumulative log returns `paths` [P, N] against `cost` [N]."""
    simple = np.expm1(paths)
    ordered = np.sort(simple, axis=0)
    worst = max(1, round(TAIL * simple.shape[0]))
    mean = simple.mean(axis=0)
    return Summary(
        mean=mean,
        sigma=simple.std(axis=0),
        es5=ordered[:worst].mean(axis=0),
        p_up=(simple > 0).mean(axis=0),
        p_net=(simple > cost).mean(axis=0),
        edge=mean - cost,
        q05=np.quantile(simple, TAIL, axis=0),
        q95=np.quantile(simple, 1.0 - TAIL, axis=0),
    )


def _spread(values: Summary, valid: np.ndarray, size: int) -> Summary:
    """Place per-valid-asset statistics into arrays of `size`, NaN elsewhere."""

    def place(array: FloatArray) -> FloatArray:
        """An array of `size` with the values of the valid assets, NaN elsewhere."""
        out = np.full(size, np.nan)
        out[valid] = array
        return out

    return Summary(
        place(values.mean),
        place(values.sigma),
        place(values.es5),
        place(values.p_up),
        place(values.p_net),
        place(values.edge),
        place(values.q05),
        place(values.q95),
    )


def run_forecast(
    ret: FloatArray,
    slot: IntArray,
    cost: FloatArray,
    params: ForecastParams,
    seed: int,
) -> Forecast:
    """Simulate from the last `window_steps` rows of `ret` [T, N] and summarize.

    `ret` and `slot` end at the slot that just closed; nothing later may be passed.
    `cost` [N] is each asset's round-trip cost as a fraction. Assets with a missing
    return inside the window (not listed yet) get NaN statistics. The same inputs
    and seed always give the same output.
    """
    window = params.window_steps
    size = ret.shape[1]
    if ret.shape[0] < window:
        raise ValueError("not enough history for the window")
    recent, recent_slot = ret[-window:], slot[-window:]
    valid = np.isfinite(recent).all(axis=0)
    empty = Summary(*(np.full(size, np.nan) for _ in range(8)))
    if not valid.any():
        return Forecast(
            params.horizons,
            dict.fromkeys(params.horizons, empty),
            valid,
            {h: np.zeros((1, size)) for h in params.horizons},
        )
    filtered = standardize(recent[:, valid], recent_slot, params.vol_span_steps)
    start_slot = (int(recent_slot[-1]) + 1) % SLOTS_PER_DAY
    root = np.random.SeedSequence(seed)
    joint_seed, *asset_seeds = root.spawn(1 + int(valid.sum()))
    analog = params.generator == "analog"

    def paths_for(
        data: Filtered,
        n_paths: int,
        drift_shrink: float,
        seed_sequence: np.random.SeedSequence,
        start_weights: FloatArray | None = None,
    ) -> FloatArray:
        """Simulate `n_paths` futures for `data` with the shared settings."""
        return simulate(
            data,
            recent_slot,
            start_slot=start_slot,
            horizons=params.horizons,
            n_paths=n_paths,
            mean_block=params.mean_block_steps,
            drift_shrink=drift_shrink,
            vol_span=params.vol_span_steps,
            rng=np.random.default_rng(seed_sequence),
            start_weights=start_weights,
        )

    joint_paths = paths_for(
        filtered,
        params.n_paths_risk if analog else params.n_paths,
        0.0 if analog else params.drift_shrink,
        joint_seed,
    )
    decision_paths = joint_paths
    if analog:
        # Each asset is conditioned on its own state, so it gets its own paths.
        decision_paths = np.empty((len(params.horizons), params.n_paths, int(valid.sum())))
        for column, asset_seed in enumerate(asset_seeds):
            single = standardize(recent[:, valid][:, [column]], recent_slot, params.vol_span_steps)
            weights = analog_weights(
                single, recent_slot, 0, share=params.analog_share, minimum=params.analog_min
            )
            own = paths_for(single, params.n_paths, params.drift_shrink, asset_seed, weights)
            decision_paths[:, :, column] = own[:, :, 0]
    by_horizon: dict[int, Summary] = {}
    joint: dict[int, FloatArray] = {}
    for k, horizon in enumerate(params.horizons):
        by_horizon[horizon] = _spread(summarize(decision_paths[k], cost[valid]), valid, size)
        full = np.zeros((joint_paths.shape[1], size))
        full[:, valid] = np.expm1(joint_paths[k])
        joint[horizon] = full
    return Forecast(params.horizons, by_horizon, valid, joint)

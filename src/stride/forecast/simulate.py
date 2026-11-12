"""Path simulation: filtered historical simulation with a slot-aligned stationary
bootstrap (design section 3).

Paths are built by copying runs of consecutive standardized shocks from the window.
A run always starts at a historical point whose slot of the day is the one being
simulated, so an overnight shock is never placed at midday. Shocks are rescaled by
a volatility that starts at today's level and is updated along each path.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from stride.forecast.params import SLOTS_PER_DAY
from stride.forecast.returns import Filtered, FloatArray, IntArray

# Keeps the simulated variance within a sane multiple of its starting level.
VARIANCE_BAND = 16.0
MIN_HISTORY = 35  # steps needed before a moment can be used as an analog (5 days)
SHORT_LOOKBACK = 7


def _positions_by_slot(slot: IntArray, n_slots: int) -> list[IntArray]:
    everything = np.arange(len(slot), dtype=np.int64)
    out: list[IntArray] = []
    for index in range(n_slots):
        rows = np.flatnonzero(slot == index).astype(np.int64)
        out.append(rows if len(rows) else everything)
    return out


def simulate(
    filtered: Filtered,
    slot: IntArray,
    *,
    start_slot: int,
    horizons: Sequence[int],
    n_paths: int,
    mean_block: float,
    drift_shrink: float,
    vol_span: int,
    rng: np.random.Generator,
    start_weights: FloatArray | None = None,
    n_slots: int = SLOTS_PER_DAY,
) -> FloatArray:
    """Cumulative log returns at each horizon: array [len(horizons), n_paths, N].

    `start_slot` is the slot of the first simulated step. All assets share the same
    historical positions in a path, which preserves their correlations.
    `drift_shrink` keeps that share of each asset's mean shock (0 = no direction).
    `start_weights` [W], if given, are the probabilities of each window position as
    the start of a path (the analog generator); later runs start uniformly.
    """
    shocks = filtered.shocks
    steps_in_window, n_assets = shocks.shape
    if steps_in_window < n_slots:
        raise ValueError("the window is shorter than one session")
    if mean_block < 1:
        raise ValueError("mean_block must be at least 1 step")
    centered = shocks - (1.0 - drift_shrink) * shocks.mean(axis=0)
    positions = _positions_by_slot(slot, n_slots)
    alpha = 2.0 / (vol_span + 1.0)
    start_variance = filtered.next_variance
    low, high = start_variance / VARIANCE_BAND, start_variance * VARIANCE_BAND
    variance = np.broadcast_to(start_variance, (n_paths, n_assets)).copy()
    cumulative = np.zeros((n_paths, n_assets))
    wanted = {int(h): k for k, h in enumerate(horizons)}
    out = np.empty((len(wanted), n_paths, n_assets))
    index = np.zeros(n_paths, dtype=np.int64)
    jump_probability = 1.0 / mean_block
    for step in range(max(wanted)):
        current_slot = (start_slot + step) % n_slots
        pool = positions[current_slot]
        if step == 0:
            if start_weights is None:
                index = rng.choice(pool, size=n_paths)
            else:
                index = rng.choice(steps_in_window, size=n_paths, p=start_weights)
        else:
            following = np.minimum(index + 1, steps_in_window - 1)
            keep = (
                (index + 1 < steps_in_window)
                & (slot[following] == current_slot)
                & (rng.random(n_paths) >= jump_probability)
            )
            index = np.where(keep, following, rng.choice(pool, size=n_paths))
        drawn = centered[index]
        cumulative += drawn * np.sqrt(variance) * filtered.factors[current_slot]
        variance = np.clip(variance * (1.0 - alpha + alpha * drawn**2), low, high)
        if step + 1 in wanted:
            out[wanted[step + 1]] = cumulative
    return out


def analog_weights(
    filtered: Filtered,
    slot: IntArray,
    asset: int,
    *,
    share: float,
    minimum: int,
) -> FloatArray | None:
    """Start probabilities that favour moments resembling the present, for one asset.

    The state of a moment is its 1-day and 5-day return in volatility units and its
    volatility against the window average. The nearest `share` of the past moments
    that closed the same slot as now (at least `minimum`) get equal probability on
    the position that followed them. Returns None when history is too short, in
    which case the caller falls back to uniform starts.
    """
    scaled = filtered.scaled[:, asset]
    variance = filtered.variance[:, asset]
    steps = len(scaled)
    average = float(variance.mean())
    if steps <= MIN_HISTORY + 1 or average <= 0:
        return None
    total = np.concatenate(([0.0], np.cumsum(scaled)))

    def trailing(length: int) -> FloatArray:
        # Sum of the `length` returns ending at each position t >= length - 1.
        sums = np.full(steps, np.nan)
        sums[length - 1 :] = total[length:] - total[:-length]
        scaled_sums: FloatArray = sums / np.sqrt(length * average)
        return scaled_sums

    with np.errstate(divide="ignore", invalid="ignore"):
        level = np.log(np.maximum(variance[1:], 1e-300) / average)
    state = np.column_stack([trailing(SHORT_LOOKBACK), trailing(MIN_HISTORY), level])
    now = state[-1]
    # A candidate t must have a successor t + 1 inside the window.
    candidates = np.flatnonzero(
        (slot[:-1] == slot[-1]) & np.isfinite(state[:-1]).all(axis=1)
    ).astype(np.int64)
    if len(candidates) < minimum or not np.isfinite(now).all():
        return None
    spread = state[candidates].std(axis=0)
    spread = np.where(spread > 0, spread, 1.0)
    distance = (((state[candidates] - now) / spread) ** 2).sum(axis=1)
    count = min(len(candidates), max(minimum, round(share * len(candidates))))
    nearest = candidates[np.argsort(distance, kind="stable")[:count]]
    weights = np.zeros(steps)
    weights[nearest + 1] = 1.0 / count
    return weights

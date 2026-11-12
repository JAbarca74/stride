"""Calibration measures (design section 8): does 60% happen about 60% of the time?"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from stride.forecast.returns import FloatArray

BINS = 10


def reliability_table(
    predicted: FloatArray, happened: FloatArray, bins: int = BINS
) -> pd.DataFrame:
    """Predicted probability against observed frequency in `bins` equal-width bins.

    Columns: `low`, `high`, `count`, `predicted` (mean forecast in the bin),
    `observed` (share of cases that happened), `gap` (observed - predicted).
    Empty bins are left out.
    """
    edges = np.linspace(0.0, 1.0, bins + 1)
    which = np.clip(np.digitize(predicted, edges[1:-1]), 0, bins - 1)
    rows = []
    for index in range(bins):
        inside = which == index
        count = int(inside.sum())
        if count == 0:
            continue
        mean_predicted = float(predicted[inside].mean())
        observed = float(happened[inside].mean())
        rows.append(
            {
                "low": edges[index],
                "high": edges[index + 1],
                "count": count,
                "predicted": mean_predicted,
                "observed": observed,
                "gap": observed - mean_predicted,
            }
        )
    return pd.DataFrame(rows, columns=["low", "high", "count", "predicted", "observed", "gap"])


def brier_score(predicted: FloatArray, happened: FloatArray) -> float:
    """Mean squared error of a probability forecast (lower is better)."""
    return float(np.mean((predicted - happened) ** 2))


def brier_skill(predicted: FloatArray, happened: FloatArray, reference: FloatArray) -> float:
    """1 - Brier(model) / Brier(reference): above 0 means better than the reference."""
    base = brier_score(reference, happened)
    if base == 0:
        return 0.0
    return 1.0 - brier_score(predicted, happened) / base


def coverage(low: FloatArray, high: FloatArray, outcome: FloatArray) -> float:
    """Share of outcomes inside the simulated interval [low, high]."""
    return float(np.mean((outcome >= low) & (outcome <= high)))


@dataclass(frozen=True)
class SkillInterval:
    """A Brier skill with the 95% interval of its bootstrap."""

    skill: float
    low: float
    high: float


def brier_skill_interval(
    predicted: FloatArray,
    happened: FloatArray,
    reference: FloatArray,
    groups: np.ndarray,
    rng: np.random.Generator,
    *,
    resamples: int = 1000,
    level: float = 0.95,
) -> SkillInterval:
    """Brier skill with a bootstrap interval that resamples whole `groups`.

    Forecasts made close together overlap in time and are not independent, so the
    bootstrap draws whole groups (e.g. calendar months) with replacement.
    """
    labels, inverse = np.unique(groups, return_inverse=True)
    model_error = (predicted - happened) ** 2
    base_error = (reference - happened) ** 2
    model_sum = np.bincount(inverse, weights=model_error, minlength=len(labels))
    base_sum = np.bincount(inverse, weights=base_error, minlength=len(labels))
    draws = rng.integers(0, len(labels), size=(resamples, len(labels)))
    numerator = model_sum[draws].sum(axis=1)
    denominator = base_sum[draws].sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        skills = np.where(denominator > 0, 1.0 - numerator / denominator, 0.0)
    tail = (1.0 - level) / 2.0
    return SkillInterval(
        brier_skill(predicted, happened, reference),
        float(np.quantile(skills, tail)),
        float(np.quantile(skills, 1.0 - tail)),
    )

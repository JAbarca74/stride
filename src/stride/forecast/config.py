"""Configuration of the Monte Carlo paper instance (`config.paper_mc.yaml`).

A small standalone file, not a copy of `config.yaml`: the instance has nothing to do
with the daily strategy. Costs (commission, spread) are taken from `config.yaml`, so
the instance pays what B and DIP pay. Every number here is a parameter; none is
hardcoded in the job (CLAUDE.md, rule 5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stride.forecast.params import DecisionParams, ForecastParams, WeightParams
from stride.forecast.walkforward import Rule

Ticker = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")]
Fraction = Annotated[float, Field(gt=0.0, lt=1.0)]


class McConfigError(Exception):
    """The file is missing, unreadable or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ForecastSection(_Section):
    """How the futures are simulated (`ForecastParams`)."""

    window_days: Annotated[int, Field(ge=20)]
    generator: Literal["drift", "analog"]
    drift_shrink: Annotated[float, Field(ge=0.0, le=1.0)]
    n_paths: Annotated[int, Field(ge=100)]
    n_paths_risk: Annotated[int, Field(ge=100)]
    order_usd: Annotated[float, Field(gt=0)]  # typical order: sets the cost the forecast subtracts
    seed: int


class DecisionSection(_Section):
    """Entry and exit rule (`DecisionParams`)."""

    horizon: Annotated[int, Field(ge=1)]  # decision horizon, in slots (35 = 5 days)
    confirm_horizon: Annotated[int, Field(ge=1)]
    p_enter: Fraction
    p_exit: Fraction
    k_cost: Annotated[float, Field(ge=0)]
    p_confirm: Fraction


class WeightsSection(_Section):
    """Dynamic weights and risk limits (`WeightParams`)."""

    kappa: Annotated[float, Field(gt=0)]
    max_weight: Annotated[float, Field(gt=0, le=1.0)]
    max_total: Annotated[float, Field(gt=0, le=1.0)]
    risk_horizon: Annotated[int, Field(ge=1)]
    risk_quantile: Fraction
    max_portfolio_loss: Annotated[float, Field(gt=0)]
    min_order_usd: Annotated[float, Field(ge=0)]


class McConfig(_Section):
    """`enabled: false` makes every tick of the job a no-op (the owner flips it)."""

    enabled: bool
    mode: Literal["paper"]  # this instance never trades real money
    capital_usd: Annotated[float, Field(gt=0)]
    kill_switch_dd: Fraction
    db_path: str
    universe: Annotated[list[Ticker], Field(min_length=2)]
    groups: dict[str, list[Ticker]]
    group_caps: dict[str, Annotated[float, Field(gt=0, le=1.0)]]
    forecast: ForecastSection
    decision: DecisionSection
    weights: WeightsSection
    history_days: Annotated[int, Field(ge=30)]  # calendar days of 30-minute bars kept
    catchup_slots: Annotated[int, Field(ge=1)]  # most recent missed slots that are still decided
    download_budget_s: Annotated[int, Field(ge=10)]  # seconds a tick may spend downloading bars

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        universe = set(self.universe)
        if len(universe) != len(self.universe):
            raise ValueError("universe contains duplicate symbols")
        seen: set[str] = set()
        for name, members in self.groups.items():
            for symbol in members:
                if symbol not in universe:
                    raise ValueError(f"group {name!r}: {symbol} is not in the universe")
                if symbol in seen:
                    raise ValueError(f"{symbol} belongs to more than one group")
                seen.add(symbol)
        if set(self.group_caps) - set(self.groups):
            raise ValueError("group_caps names a group that does not exist")
        if self.decision.p_exit >= self.decision.p_enter:
            raise ValueError("decision.p_exit must be below p_enter (hysteresis)")
        if self.decision.horizon not in (1, 7, 35, 140):
            raise ValueError("decision.horizon must be one of the simulated horizons")
        if self.weights.risk_horizon not in (1, 7, 35, 140):
            raise ValueError("weights.risk_horizon must be one of the simulated horizons")
        if self.history_days < self.forecast.window_days * 365 // 252 + 14:
            raise ValueError("history_days must cover the forecast window")
        return self

    def forecast_params(self) -> ForecastParams:
        """The simulation parameters (horizons 1 hour, 1, 5 and 20 days)."""
        section = self.forecast
        return ForecastParams(
            window_days=section.window_days,
            generator=section.generator,
            drift_shrink=section.drift_shrink,
            n_paths=section.n_paths,
            n_paths_risk=section.n_paths_risk,
            horizons=(1, 7, 35, 140),
        )

    def rule(self) -> Rule:
        """The trading rule: decision parameters and weight limits."""
        decision = DecisionParams(**self.decision.model_dump())
        weights = WeightParams(
            **self.weights.model_dump(),
            groups={name: tuple(members) for name, members in self.groups.items()},
            group_caps=dict(self.group_caps),
        )
        return Rule("mc", decision, weights)


def load_mc_config(path: str | Path) -> McConfig:
    """Read and validate the instance file."""
    target = Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        return McConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise McConfigError(f"{target}: {exc}") from None

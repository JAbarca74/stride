"""`config.paper_news.yaml`, validated (docs/design/news_agent.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stride.config import ConfigError

EVENTS = (
    "INSIDER_BUY",
    "STAKE_UP",
    "UPGRADE",
    "GUIDANCE_UP",
    "BEAT",
    "BUYBACK",
    "TARGET_UP",
    "TARGET_DOWN",
    "DOWNGRADE",
    "DOWNGRADE_SELL",
    "STAKE_DOWN",
    "MISS",
    "GUIDANCE_DOWN",
    "REGULATOR",
)
Fraction = Annotated[float, Field(gt=0, lt=1)]
NonNegative = Annotated[float, Field(ge=0)]


class NewsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool
    db_path: str
    capital_usd: Annotated[float, Field(gt=0)]
    score_window_sessions: Annotated[int, Field(ge=1)]
    buy_score: Annotated[float, Field(gt=0)]
    sell_score: Annotated[float, Field(lt=0)]
    max_positions: Annotated[int, Field(ge=1)]
    position_weight: Fraction
    max_hold_sessions: Annotated[int, Field(ge=1)]
    stop_loss: Fraction
    kill_switch_dd: Fraction
    brake_symbol: str
    brake_sma: Annotated[int, Field(ge=2)]
    decision_cutoff: Annotated[str, Field(pattern=r"^\d{2}:\d{2}$")]
    spread_bps: NonNegative
    tiered_per_share: NonNegative
    tiered_min_per_order: NonNegative
    tiered_max_pct_of_value: NonNegative
    tiered_third_party_per_share: NonNegative
    alert_points: Annotated[float, Field(gt=0)]
    points: dict[str, float]
    universe: dict[str, list[str]]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if set(self.points) != set(EVENTS):
            raise ValueError(f"points must list exactly the events {EVENTS}")
        if self.max_positions * self.position_weight > 1 + 1e-9:
            raise ValueError("max_positions x position_weight must not exceed 1 (no leverage)")
        if not self.universe:
            raise ValueError("the universe is empty")
        return self

    @property
    def symbols(self) -> list[str]:
        return sorted(self.universe)


def load_news_config(path: str | Path) -> NewsConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return NewsConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid news config {path}: {exc}") from exc

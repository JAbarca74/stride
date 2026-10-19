from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml

from stride.config import ConfigError, StrideConfig, load_config

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "config.yaml"


def _reference() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(REFERENCE.read_text(encoding="utf-8"))
    return data


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _with(section: str | None, key: str, value: Any) -> dict[str, Any]:
    data = _reference()
    target = data if section is None else data[section]
    target[key] = value
    return data


def test_reference_file_is_accepted() -> None:
    config = load_config(REFERENCE, env={})
    assert isinstance(config, StrideConfig)
    assert config.mode == "paper"
    assert len(config.universe) == 10
    assert config.risk.max_leverage == 4.0
    assert config.leveraged_etfs["SPY"] == {2: "SSO", 3: "UPRO"}
    assert config.data.history_start.isoformat() == "2007-01-01"


def test_max_leverage_10_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, _with("risk", "max_leverage", 10))
    with pytest.raises(ConfigError, match="max_leverage"):
        load_config(path, env={})


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("strategy", "sma_filter", 1),
        ("strategy", "entry_breakout", 0),
        ("strategy", "atr_period", -5),
        ("strategy", "stop_n", 0),
        ("strategy", "pyramid_step_n", 6),
        ("strategy", "max_units_per_asset", 5),
        ("strategy", "max_units_per_asset", 0),
        ("risk", "risk_per_unit", 0.1),
        ("risk", "risk_per_unit", 0),
        ("risk", "max_units_total", 0),
        ("risk", "kelly_fraction", 1.5),
        ("risk", "kelly_fraction", 0),
        ("risk", "max_leverage", 0),
        ("risk", "max_etf_portfolio_leverage", 0.5),
        ("risk", "max_etf_portfolio_leverage", 3.5),
        ("risk", "max_portfolio_vol", 1.0),
        ("risk", "dd_delever_at", -0.2),
        ("risk", "kill_switch_dd", 1.0),
        ("risk", "margin_min_equity", -1),
        ("risk", "margin_buffer", 0),
        ("risk", "min_history_days_for_kelly", 0),
        ("execution", "rebalance_band", 1.2),
        ("execution", "min_order_usd", -1),
        ("execution", "limit_offset", 1),
        ("execution", "max_orders_per_day", 0),
        ("execution", "max_order_pct_equity", 0),
        ("cash_flows", "monthly_deposit_usd", -20),
        ("cash_flows", "deposit_day", "last_trading_day"),
        ("costs", "spread_bps", -1),
        ("costs", "commission_max_pct_of_value", -0.01),
        ("data", "db_path", ""),
        ("data", "history_start", "not-a-date"),
        (None, "mode", "production"),
        (None, "universe", []),
    ],
)
def test_out_of_range_values_are_rejected(
    tmp_path: Path, section: str | None, key: str, value: Any
) -> None:
    path = _write(tmp_path, _with(section, key, value))
    with pytest.raises(ConfigError, match=key):
        load_config(path, env={})


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("strategy", "exit_breakout", 55, "exit_breakout must be shorter"),
        ("risk", "max_etf_portfolio_leverage", 3.0, "must not exceed max_leverage"),
        ("risk", "dd_delever_at", 0.4, "dd_delever_at must be smaller"),
        ("risk", "max_units_group", 13, "max_units_group must not exceed"),
        ("risk", "max_units_crypto", 13, "max_units_crypto must not exceed"),
        ("data", "crypto_history_start", "2006-12-31", "crypto_history_start"),
        (None, "universe", ["SPY", "SPY"], "duplicate"),
        (None, "universe", ["spy"], "universe"),
    ],
)
def test_inconsistent_values_are_rejected(
    tmp_path: Path, section: str | None, key: str, value: Any, message: str
) -> None:
    data = _with(section, key, value)
    if key == "max_etf_portfolio_leverage":
        data["risk"]["max_leverage"] = 2.0
    if key == "universe":
        data["groups"] = {}
        data["leveraged_etfs"] = {}
    path = _write(tmp_path, data)
    with pytest.raises(ConfigError, match=message):
        load_config(path, env={})


def test_max_units_per_asset_above_group_cap_is_rejected(tmp_path: Path) -> None:
    data = _reference()
    data["strategy"]["max_units_per_asset"] = 4
    data["risk"]["max_units_group"] = 3
    data["risk"]["max_units_crypto"] = 3
    path = _write(tmp_path, data)
    with pytest.raises(ConfigError, match="max_units_per_asset must not exceed"):
        load_config(path, env={})


def test_group_member_outside_universe_is_rejected(tmp_path: Path) -> None:
    data = _reference()
    data["groups"]["equities"].append("QQQ")
    with pytest.raises(ConfigError, match="QQQ is not in the universe"):
        load_config(_write(tmp_path, data), env={})


def test_symbol_in_two_groups_is_rejected(tmp_path: Path) -> None:
    data = _reference()
    data["groups"]["bonds"].append("SPY")
    with pytest.raises(ConfigError, match="more than one group"):
        load_config(_write(tmp_path, data), env={})


def test_leveraged_etf_for_unknown_symbol_is_rejected(tmp_path: Path) -> None:
    data = _reference()
    data["leveraged_etfs"]["QQQ"] = {2: "QLD"}
    with pytest.raises(ConfigError, match="leveraged_etfs: QQQ"):
        load_config(_write(tmp_path, data), env={})


def test_leveraged_multiple_other_than_2_or_3_is_rejected(tmp_path: Path) -> None:
    data = _reference()
    data["leveraged_etfs"]["SPY"] = {4: "XXX"}
    with pytest.raises(ConfigError, match="leveraged_etfs"):
        load_config(_write(tmp_path, data), env={})


@pytest.mark.parametrize("section", [None, "risk"])
def test_unknown_key_is_rejected(tmp_path: Path, section: str | None) -> None:
    path = _write(tmp_path, _with(section, "max_levrage", 2.0))
    with pytest.raises(ConfigError, match="max_levrage"):
        load_config(path, env={})


def test_missing_key_is_rejected(tmp_path: Path) -> None:
    data = _reference()
    del data["risk"]["kill_switch_dd"]
    with pytest.raises(ConfigError, match="kill_switch_dd"):
        load_config(_write(tmp_path, data), env={})


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml", env={})


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("risk: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path, env={})


def test_non_mapping_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(path, env={})


def test_loaded_config_is_frozen() -> None:
    config = load_config(REFERENCE, env={})
    with pytest.raises(pydantic.ValidationError):
        config.risk.max_leverage = 10


@pytest.mark.parametrize("env", [{}, {"LIVE_CONFIRM": ""}, {"LIVE_CONFIRM": "yes"}])
def test_live_without_confirmation_falls_back_to_paper(
    tmp_path: Path, env: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path, _with(None, "mode", "live"))
    with caplog.at_level(logging.WARNING, logger="stride.config"):
        config = load_config(path, env=env)
    assert config.mode == "paper"
    assert "starting in paper mode" in caplog.text


@pytest.mark.parametrize("mode", ["backtest", "paper"])
def test_non_live_modes_are_kept(tmp_path: Path, mode: str) -> None:
    path = _write(tmp_path, _with(None, "mode", mode))
    assert load_config(path, env={}).mode == mode


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("risk", "max_leverage", True),
        ("costs", "commission_min_per_order", False),
        ("risk", "max_leverage", "2.0"),
        ("strategy", "sma_filter", 200.0),
        ("strategy", "entry_breakout", "55"),
        ("risk", "min_history_days_for_kelly", True),
    ],
)
def test_wrong_scalar_types_are_rejected_not_coerced(
    tmp_path: Path, section: str, key: str, value: Any
) -> None:
    path = _write(tmp_path, _with(section, key, value))
    with pytest.raises(ConfigError, match=key):
        load_config(path, env={})


def test_integer_is_accepted_for_float_field(tmp_path: Path) -> None:
    path = _write(tmp_path, _with("risk", "max_leverage", 3))
    assert load_config(path, env={}).risk.max_leverage == 3.0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("missing_days_lookback", 0),
        ("max_daily_change", 0),
        ("max_daily_change", 1.5),
        ("source_close_tolerance", 0),
        ("latest_bar_max_diff", 1),
    ],
)
def test_out_of_range_quality_thresholds_are_rejected(tmp_path: Path, key: str, value: Any) -> None:
    data = _reference()
    data["data"]["quality"][key] = value
    with pytest.raises(ConfigError, match=key):
        load_config(_write(tmp_path, data), env={})


def test_latest_bar_limit_below_sample_tolerance_is_rejected(tmp_path: Path) -> None:
    data = _reference()
    data["data"]["quality"]["latest_bar_max_diff"] = 0.001
    with pytest.raises(ConfigError, match="latest_bar_max_diff must not be below"):
        load_config(_write(tmp_path, data), env={})


def test_quality_section_is_required(tmp_path: Path) -> None:
    data = _reference()
    del data["data"]["quality"]
    with pytest.raises(ConfigError, match="quality"):
        load_config(_write(tmp_path, data), env={})

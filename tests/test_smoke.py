from __future__ import annotations

from pathlib import Path

import yaml

import stride

ROOT = Path(__file__).resolve().parents[1]


def test_package_imports() -> None:
    assert stride.__version__ == "0.1.0"


def test_reference_config_parses() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert config["mode"] in {"backtest", "paper", "live"}
    assert len(config["universe"]) == 10
    assert config["risk"]["max_leverage"] == 4.0


def test_secrets_are_git_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", "data/", "KILL_SWITCH"):
        assert entry in ignored

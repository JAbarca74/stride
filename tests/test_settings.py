from __future__ import annotations

from pathlib import Path

import pytest

from stride.settings import CredentialError, load_secrets

FAKE_DATA = "fake-data-key-not-real"
FAKE_PAPER = "PKFAKENOTREAL"
FAKE_LIVE = "AKFAKENOTREAL"
FAKE_SECRET = "fake-secret-not-real"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATA_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


def _env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_keys_are_read_from_env_file(tmp_path: Path) -> None:
    secrets = load_secrets(
        _env(
            tmp_path,
            f"DATA_API_KEY={FAKE_DATA}\nALPACA_API_KEY={FAKE_PAPER}\n"
            f"ALPACA_SECRET_KEY={FAKE_SECRET}\nLIVE_CONFIRM=\n",
        )
    )
    assert secrets.tiingo_key() == FAKE_DATA
    assert secrets.alpaca_paper_keys() == (FAKE_PAPER, FAKE_SECRET)


def test_keys_never_appear_in_repr(tmp_path: Path) -> None:
    secrets = load_secrets(_env(tmp_path, f"DATA_API_KEY={FAKE_DATA}\n"))
    assert FAKE_DATA not in repr(secrets)
    assert FAKE_DATA not in str(secrets)


def test_empty_values_count_as_missing(tmp_path: Path) -> None:
    secrets = load_secrets(_env(tmp_path, "DATA_API_KEY=\nALPACA_API_KEY=\n"))
    with pytest.raises(CredentialError, match="DATA_API_KEY is not set"):
        secrets.tiingo_key()
    with pytest.raises(CredentialError, match="not set"):
        secrets.alpaca_paper_keys()


def test_missing_env_file_means_no_keys(tmp_path: Path) -> None:
    with pytest.raises(CredentialError):
        load_secrets(tmp_path / "missing.env").tiingo_key()


def test_live_alpaca_key_is_refused(tmp_path: Path) -> None:
    secrets = load_secrets(
        _env(tmp_path, f"ALPACA_API_KEY={FAKE_LIVE}\nALPACA_SECRET_KEY={FAKE_SECRET}\n")
    )
    with pytest.raises(CredentialError, match="not a paper-trading key") as info:
        secrets.alpaca_paper_keys()
    assert FAKE_LIVE not in str(info.value)

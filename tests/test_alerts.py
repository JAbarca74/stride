from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

import pytest
import requests

from stride.reporting.alerts import (
    MAX_LENGTH,
    Alert,
    AlertError,
    LogAlerter,
    TelegramAlerter,
    alerter_from_env,
    main,
)

FAKE_TOKEN = "123456:fake-token-not-real"
FAKE_CHAT = "-100987"


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeSession:
    def __init__(self, outcome: int | Exception = 200) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def post(self, url: str, *, json: Mapping[str, str], timeout: float) -> FakeResponse:
        self.calls.append((url, json))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return FakeResponse(self.outcome)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ALERT_TELEGRAM_TOKEN", "ALERT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)


def test_sends_a_test_message_to_the_bot() -> None:
    session = FakeSession()
    TelegramAlerter(FAKE_TOKEN, FAKE_CHAT, session).send(Alert("TEST", "hello"))
    [(url, payload)] = session.calls
    assert url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert payload == {"chat_id": FAKE_CHAT, "text": "STRIDE [paper] TEST: hello"}


@pytest.mark.parametrize("outcome", [401, requests.ConnectionError(f"boom {FAKE_TOKEN}")])
def test_errors_never_leak_the_token(outcome: int | Exception) -> None:
    alerter = TelegramAlerter(FAKE_TOKEN, FAKE_CHAT, FakeSession(outcome))
    with pytest.raises(AlertError) as info:
        alerter.send(Alert("KILL_SWITCH", "drawdown 40%"))
    assert FAKE_TOKEN not in str(info.value)


def test_long_messages_are_truncated() -> None:
    text = Alert("DATA_FAILED", "x" * 10_000).text()
    assert len(text) == MAX_LENGTH
    assert text.endswith("...")


def test_missing_token_is_rejected() -> None:
    with pytest.raises(AlertError):
        TelegramAlerter("", FAKE_CHAT)


def test_log_alerter_writes_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="stride.reporting.alerts"):
        LogAlerter().send(Alert("ORDER_VETOED", "SPY too large", mode="backtest"))
    assert "STRIDE [backtest] ORDER_VETOED: SPY too large" in caplog.text


def test_alerter_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("ALERT_TELEGRAM_TOKEN=\nALERT_TELEGRAM_CHAT_ID=\n", encoding="utf-8")
    assert isinstance(alerter_from_env(str(env)), LogAlerter)
    env.write_text(f"ALERT_TELEGRAM_TOKEN={FAKE_TOKEN}\nALERT_TELEGRAM_CHAT_ID={FAKE_CHAT}\n",
                   encoding="utf-8")  # fmt: skip
    assert isinstance(alerter_from_env(str(env)), TelegramAlerter)


def test_cli_without_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    assert main(["test"]) == 2
    assert "not set" in capsys.readouterr().out
    assert main([]) == 2


def test_cli_sends_with_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"ALERT_TELEGRAM_TOKEN={FAKE_TOKEN}\nALERT_TELEGRAM_CHAT_ID={FAKE_CHAT}\n", encoding="utf-8"
    )
    session = FakeSession()
    monkeypatch.setattr("stride.reporting.alerts.requests.Session", lambda: session)
    assert main(["test"]) == 0
    assert "sent" in capsys.readouterr().out
    assert session.calls[0][1]["text"].endswith("test message, alerts are working")
    session.outcome = 500
    assert main(["test"]) == 1

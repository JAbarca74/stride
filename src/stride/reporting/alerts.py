"""Telegram alerts (SPEC section 10, task T16).

Alerts go out when a daily run fails or does not run, data fails validation, the
risk manager vetoes an order, reconciliation differs, drawdown crosses -20% or the
kill switch fires. The bot token travels in the request URL (Telegram's API), so
neither the URL nor the token ever appears in a log line or an error message.

Without `ALERT_TELEGRAM_TOKEN` and `ALERT_TELEGRAM_CHAT_ID` in `.env`, alerts are
written to the log only.

    python -m stride.reporting.alerts test   # send a test message
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import requests

from stride.settings import load_secrets

logger = logging.getLogger(__name__)

AlertKind = Literal[
    "RUN_FAILED",
    "RUN_MISSED",
    "DATA_FAILED",
    "ORDER_VETOED",
    "RECONCILIATION",
    "DRAWDOWN",
    "KILL_SWITCH",
    "TEST",
]
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 15
MAX_LENGTH = 4096  # Telegram's limit per message
HTTP_OK = 200


class AlertError(Exception):
    """An alert could not be delivered."""


class PostResponse(Protocol):
    status_code: int


class PostSession(Protocol):
    def post(self, url: str, *, json: Mapping[str, str], timeout: float) -> PostResponse: ...


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    message: str
    mode: str = "paper"

    def text(self) -> str:
        body = f"STRIDE [{self.mode}] {self.kind}: {self.message}"
        return body if len(body) <= MAX_LENGTH else body[: MAX_LENGTH - 3] + "..."


class Alerter(Protocol):
    def send(self, alert: Alert) -> None: ...


class LogAlerter:
    """Fallback when Telegram is not configured: alerts go to the log."""

    def send(self, alert: Alert) -> None:
        logger.warning("%s", alert.text())


class TelegramAlerter:
    def __init__(self, token: str, chat_id: str, session: PostSession | None = None) -> None:
        if not token or not chat_id:
            raise AlertError("Telegram token and chat id are required")
        self._token = token
        self._chat_id = chat_id
        self._session: PostSession = session or requests.Session()

    def send(self, alert: Alert) -> None:
        try:
            response = self._session.post(
                TELEGRAM_URL.format(token=self._token),
                json={"chat_id": self._chat_id, "text": alert.text()},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise AlertError(f"telegram request failed: {type(exc).__name__}") from None
        if response.status_code != HTTP_OK:
            raise AlertError(f"telegram returned HTTP {response.status_code}")


def alerter_from_env(env_file: str = ".env") -> Alerter:
    """Telegram if both values are in `.env`, otherwise the log."""
    telegram = load_secrets(env_file).telegram()
    if telegram is None:
        logger.warning("Telegram alerts not configured; alerts go to the log only")
        return LogAlerter()
    token, chat_id = telegram
    return TelegramAlerter(token, chat_id)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["test"]:
        sys.stdout.write("usage: python -m stride.reporting.alerts test\n")
        return 2
    alerter = alerter_from_env()
    if isinstance(alerter, LogAlerter):
        sys.stdout.write("ALERT_TELEGRAM_TOKEN and ALERT_TELEGRAM_CHAT_ID are not set in .env\n")
        return 2
    try:
        alerter.send(Alert("TEST", "test message, alerts are working"))
    except AlertError as exc:
        sys.stdout.write(f"error: {exc}\n")
        return 1
    sys.stdout.write("sent\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

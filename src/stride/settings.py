"""Credentials from `.env` (SPEC section 11). Never print, log or commit them.

Only data-only keys (Tiingo) and paper-trading keys (Alpaca paper) are allowed for
development. Values are `SecretStr`, so they never show up in a repr or a log line.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Alpaca issues paper-trading key IDs starting with "PK" and live ones with "AK".
ALPACA_PAPER_KEY_PREFIX = "PK"


class CredentialError(Exception):
    """A required key is missing, or a key is not of an allowed kind."""


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_api_key: SecretStr | None = None
    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alert_telegram_token: SecretStr | None = None
    alert_telegram_chat_id: SecretStr | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_missing(cls, value: object) -> object:
        return None if value == "" else value

    def tiingo_key(self) -> str:
        if self.data_api_key is None:
            raise CredentialError("DATA_API_KEY is not set in .env")
        return self.data_api_key.get_secret_value()

    def telegram(self) -> tuple[str, str] | None:
        """Bot token and chat id, or None when alerts are not configured."""
        if self.alert_telegram_token is None or self.alert_telegram_chat_id is None:
            return None
        return (
            self.alert_telegram_token.get_secret_value(),
            self.alert_telegram_chat_id.get_secret_value(),
        )

    def alpaca_paper_keys(self) -> tuple[str, str]:
        """Alpaca keys, refusing anything that is not a paper-trading key."""
        if self.alpaca_api_key is None or self.alpaca_secret_key is None:
            raise CredentialError("ALPACA_API_KEY and ALPACA_SECRET_KEY are not set in .env")
        key = self.alpaca_api_key.get_secret_value()
        if not key.startswith(ALPACA_PAPER_KEY_PREFIX):
            raise CredentialError("ALPACA_API_KEY is not a paper-trading key; refusing to use it")
        return key, self.alpaca_secret_key.get_secret_value()


def load_secrets(env_file: str | Path = ".env") -> Secrets:
    """Read keys from the process environment and `env_file` (environment wins)."""
    return Secrets(_env_file=env_file)  # type: ignore[call-arg]

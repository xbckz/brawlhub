"""Validated runtime configuration for the ticket-only bot."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"
    discord_application_id: int | None = None
    discord_bot_token: SecretStr | None = None
    discord_development_guild_id: int | None = None
    discord_members_intent_enabled: bool = False
    turso_database_url: str | None = None
    turso_auth_token: SecretStr | None = None
    ticket_admin_user_ids: str = "606751416261935123"
    ticket_admin_role_ids: str = ""

    @field_validator("discord_application_id", "discord_development_guild_id", mode="before")
    @classmethod
    def empty_snowflake_is_unset(cls, value: int | str | None) -> int | str | None:
        return None if value == "" else value

    @field_validator("discord_application_id", "discord_development_guild_id")
    @classmethod
    def validate_snowflake(cls, value: int | None) -> int | None:
        if value is not None and not 10**16 <= value < 10**20:
            raise ValueError("must be a valid Discord snowflake")
        return value

    @field_validator("discord_bot_token", "turso_auth_token", mode="before")
    @classmethod
    def empty_secret_is_unset(cls, value: str | None) -> str | None:
        return None if value == "" else value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @staticmethod
    def _parse_ids(raw: str) -> frozenset[int]:
        values: set[int] = set()
        for item in raw.split(","):
            value = item.strip()
            if not value:
                continue
            if not value.isdigit() or not 10**16 <= int(value) < 10**20:
                raise ValueError(f"Invalid Discord ID in admin configuration: {value}")
            values.add(int(value))
        return frozenset(values)

    def ticket_admin_ids(self) -> frozenset[int]:
        return self._parse_ids(self.ticket_admin_user_ids)

    def ticket_admin_role_id_set(self) -> frozenset[int]:
        return self._parse_ids(self.ticket_admin_role_ids)

    def validate_bot_runtime(self) -> None:
        errors: list[str] = []
        if self.discord_bot_token is None:
            errors.append("DISCORD_BOT_TOKEN is required")
        if not self.turso_database_url:
            errors.append("TURSO_DATABASE_URL is required")
        elif not self.turso_database_url.startswith("file:") and self.turso_auth_token is None:
            errors.append("TURSO_AUTH_TOKEN is required for a remote Turso database")
        if not self.ticket_admin_ids() and not self.ticket_admin_role_id_set():
            errors.append(
                "TICKET_ADMIN_USER_IDS or TICKET_ADMIN_ROLE_IDS must contain at least one ID"
            )
        if self.environment == "production" and self.discord_development_guild_id is not None:
            errors.append("DISCORD_DEVELOPMENT_GUILD_ID must be unset in production")
        if errors:
            raise RuntimeError("Invalid bot runtime configuration: " + "; ".join(errors))

    def require_bot_token(self) -> str:
        if self.discord_bot_token is None:
            raise RuntimeError("DISCORD_BOT_TOKEN is required to run bot.py")
        return self.discord_bot_token.get_secret_value()

    def require_database_url(self) -> str:
        if not self.turso_database_url:
            raise RuntimeError("TURSO_DATABASE_URL is required for this process")
        return self.turso_database_url

    def require_database_auth_token(self) -> str:
        return self.turso_auth_token.get_secret_value() if self.turso_auth_token else ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

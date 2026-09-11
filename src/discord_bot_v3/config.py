"""Application configuration, loaded once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


def _parse_timezone(raw: str | None) -> str:
    """Validate the configured zone now, rather than when a reminder is set."""
    name = (raw or "Asia/Kuala_Lumpur").strip() or "Asia/Kuala_Lumpur"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TIMEZONE is not a known zone: {name!r}") from exc
    return name


def _parse_mysql() -> MysqlConfig | None:
    """Build the MySQL settings, or None when the bot should run without it.

    A user is the one thing that cannot be guessed, so its absence is what
    marks MySQL as unconfigured; everything else has a sensible default.
    """
    user = os.getenv("MYSQL_USER", "").strip()
    if not user:
        return None

    raw_port = os.getenv("MYSQL_PORT", "3306").strip() or "3306"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError(f"MYSQL_PORT must be a number, got {raw_port!r}") from exc

    return MysqlConfig(
        host=os.getenv("MYSQL_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=port,
        user=user,
        password=os.getenv("MYSQL_PASSWORD", ""),
        name=os.getenv("MYSQL_DB", "discord_v3").strip() or "discord_v3",
    )


def _parse_guild_ids(raw: str | None) -> list[int]:
    """Parse a comma-separated guild id list, ignoring blanks."""
    if not raw:
        return []
    try:
        return [int(part) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise ConfigError(f"GUILD_IDS must be comma-separated integers, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class MysqlConfig:
    """Connection settings for the bot's own MySQL database."""

    host: str
    port: int
    user: str
    password: str
    name: str


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime settings for the bot."""

    token: str
    guild_ids: list[int] = field(default_factory=list)
    log_level: str = "INFO"
    # Zone that bare wall-clock input is read in, e.g. "remind me at 14:00".
    # Everything is stored in UTC regardless.
    timezone: str = "Asia/Kuala_Lumpur"
    # Optional: /gif is not registered without it. Key from https://partner.klipy.com
    klipy_api_key: str | None = None
    # Optional: the MyAnimeList fallback for /anime and /manga is skipped
    # without it. Client id from https://myanimelist.net/apiconfig
    mal_client_id: str | None = None
    # Optional: features that need persistence are skipped without it.
    mysql: MysqlConfig | None = None
    # Optional: caching only. Absent means every lookup goes to its API.
    redis_url: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        """Build a Config from environment variables, loading .env if present."""
        load_dotenv()

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
            )

        return cls(
            token=token,
            guild_ids=_parse_guild_ids(os.getenv("GUILD_IDS")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            timezone=_parse_timezone(os.getenv("TIMEZONE")),
            klipy_api_key=os.getenv("KLIPY_API_KEY", "").strip() or None,
            mal_client_id=os.getenv("MAL_CLIENT_ID", "").strip() or None,
            mysql=_parse_mysql(),
            redis_url=os.getenv("REDIS_URL", "").strip() or None,
        )

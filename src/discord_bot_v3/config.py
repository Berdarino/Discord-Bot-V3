"""Application configuration, loaded once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


# V2 greeted every join with this, and the joke is the point, so it stays the
# default rather than becoming a required setting. WELCOME_MESSAGE overrides it;
# an empty WELCOME_MESSAGE turns greetings off entirely.
DEFAULT_WELCOME_MESSAGE = "Who simply add people in again... smh"

# The local model the chat feature talks to. 8B at Q4 is about 5GB resident,
# which is the practical floor for holding a character over several turns.
DEFAULT_OLLAMA_MODEL = "qwen3:8b"


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


def _parse_welcome_message(raw: str | None) -> str:
    """Read the join greeting, distinguishing "unset" from "deliberately off".

    An absent variable means the caller never thought about it and gets the
    default; one present but blank is an explicit "post nothing".
    """
    if raw is None:
        return DEFAULT_WELCOME_MESSAGE
    return raw.strip()


def _parse_guild_ids(raw: str | None) -> list[int]:
    """Parse a comma-separated guild id list, ignoring blanks."""
    if not raw:
        return []
    try:
        return [int(part) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise ConfigError(f"GUILD_IDS must be comma-separated integers, got {raw!r}") from exc


def _parse_snowflake(raw: str | None, name: str) -> int | None:
    """Parse one optional Discord id without accepting zero or negatives."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a Discord id, got {text!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive Discord id, got {text!r}")
    return value


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
    # Optional: channel where the daily birthday task posts. Birthdays can
    # still be saved without it, but no public messages are sent.
    birthday_channel_id: int | None = None
    # Optional: channel that message edits and deletions are logged to.
    # Unset means the listeners resolve nothing and post nothing.
    log_channel_id: int | None = None
    # Optional: base URL of a local Ollama server. Unset disables chat
    # entirely -- the cog does not load and the bot never answers.
    ollama_url: str | None = None
    # Which model that server should answer with.
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    # Optional: channel where mentioning the bot starts a conversation.
    # Replying to the bot works anywhere, with or without this.
    chat_channel_id: int | None = None
    # Posted to a guild's system channel when someone joins. ``{member}``
    # becomes a mention and ``{guild}`` the server name. Empty posts nothing.
    welcome_message: str = DEFAULT_WELCOME_MESSAGE

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
            birthday_channel_id=_parse_snowflake(
                os.getenv("BIRTHDAY_CHANNEL_ID"), "BIRTHDAY_CHANNEL_ID"
            ),
            log_channel_id=_parse_snowflake(os.getenv("LOG_CHANNEL_ID"), "LOG_CHANNEL_ID"),
            ollama_url=os.getenv("OLLAMA_URL", "").strip() or None,
            ollama_model=os.getenv("OLLAMA_MODEL", "").strip() or DEFAULT_OLLAMA_MODEL,
            chat_channel_id=_parse_snowflake(os.getenv("CHAT_CHANNEL_ID"), "CHAT_CHANNEL_ID"),
            welcome_message=_parse_welcome_message(os.getenv("WELCOME_MESSAGE")),
        )

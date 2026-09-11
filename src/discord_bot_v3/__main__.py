"""Entry point: `python -m discord_bot_v3` or the `discord-bot-v3` script."""

from __future__ import annotations

import logging
import signal
import sys
import types

import discord

from .bot import DiscordBot
from .config import Config, ConfigError


def _raise_keyboard_interrupt(signum: int, frame: types.FrameType | None) -> None:
    """Turn SIGTERM into the interrupt Pycord already shuts down cleanly on.

    Docker sends SIGTERM on `stop`; Python's default handler would exit
    immediately, dropping the gateway connection without closing it.
    """
    raise KeyboardInterrupt


def main() -> int:
    """Start the bot. Returns a process exit code."""
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    bot = DiscordBot(config)
    bot.load_cogs()

    try:
        bot.run(config.token)
    except discord.LoginFailure:
        print("Discord rejected the token. Check DISCORD_TOKEN in your .env.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Shutting down")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

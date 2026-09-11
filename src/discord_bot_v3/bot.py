"""The bot class: wiring, extension loading, and global error handling."""

from __future__ import annotations

import logging
import pkgutil

import discord
from discord.ext import commands

from . import cogs
from .config import Config
from .services.cache import Cache
from .services.database import Database, DatabaseError

_log = logging.getLogger(__name__)

COGS_PACKAGE = "discord_bot_v3.cogs"


class DiscordBot(discord.Bot):
    """A slash-command bot that loads its features from the cogs package."""

    def __init__(self, config: Config) -> None:
        self.config = config

        # None when MYSQL_USER is unset; cogs that need persistence check this
        # and decline to load rather than failing at the first query.
        # Always present, but a no-op unless REDIS_URL is set and reachable.
        # Cogs can use it unconditionally.
        self.cache = Cache(config.redis_url)

        self.db: Database | None = None
        if config.mysql is not None:
            self.db = Database(
                host=config.mysql.host,
                port=config.mysql.port,
                user=config.mysql.user,
                password=config.mysql.password,
                name=config.mysql.name,
            )

        # Only request what we use; privileged intents must also be enabled
        # in the Discord Developer Portal before they will be granted.
        intents = discord.Intents.default()
        # Members are recorded on join and synchronised at startup for the
        # birthday feature. This also needs the Server Members Intent enabled
        # in the Discord Developer Portal.
        intents.members = True

        super().__init__(
            intents=intents,
            # Commands register instantly in these guilds. Leave GUILD_IDS unset
            # in production so commands register globally instead.
            debug_guilds=config.guild_ids or None,
        )

    def load_cogs(self) -> None:
        """Import and load every module under the cogs package.

        Discovery is import-based rather than Pycord's ``load_extensions``
        folder walk, which resolves paths relative to the current working
        directory and so does not work with a ``src/`` layout.
        """
        for module in pkgutil.walk_packages(cogs.__path__, prefix=f"{COGS_PACKAGE}."):
            if module.name.rsplit(".", 1)[-1].startswith("_"):
                continue
            try:
                self.load_extension(module.name)
            except Exception:
                _log.exception("Failed to load %s", module.name)
            else:
                _log.info("Loaded %s", module.name)

    async def start(self, *args: object, **kwargs: object) -> None:
        """Open the database before connecting to Discord.

        Pycord has no ``setup_hook``, and ``on_ready`` fires again on every
        reconnect, so this is the one place that runs once inside the event
        loop before the gateway comes up.
        """
        if self.db is not None:
            try:
                await self.db.connect()
            except DatabaseError:
                _log.exception("Could not open the database; continuing without it")
                self.db = None

        # Never fatal: a failed connect leaves every cache call a no-op.
        await self.cache.connect()

        await super().start(*args, **kwargs)

    async def close(self) -> None:
        """Close the database alongside the gateway connection."""
        if self.db is not None:
            await self.db.close()
        await self.cache.close()
        await super().close()

    async def on_ready(self) -> None:
        _log.info("Connected as %s (id=%s)", self.user, self.user.id if self.user else "?")
        scope = (
            f"{len(self.config.guild_ids)} debug guild(s)" if self.config.guild_ids else "globally"
        )
        _log.info("Serving %d guild(s); commands registered %s", len(self.guilds), scope)

    async def on_application_command_error(
        self,
        ctx: discord.ApplicationContext,
        error: discord.DiscordException,
    ) -> None:
        """Report command failures to the user without leaking internals."""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.respond(
                f"That command is on cooldown. Try again in {error.retry_after:.1f}s.",
                ephemeral=True,
            )
            return

        if isinstance(error, commands.NotOwner):
            await ctx.respond("Only the bot owner can use that.", ephemeral=True)
            return

        if isinstance(error, commands.MissingPermissions):
            await ctx.respond("You do not have permission to use that.", ephemeral=True)
            return

        _log.exception("Unhandled error in /%s", ctx.command.qualified_name, exc_info=error)
        await ctx.respond("Something went wrong. The error has been logged.", ephemeral=True)

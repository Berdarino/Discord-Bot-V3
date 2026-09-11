"""General-purpose commands available to everyone.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
Pycord introspects the ``discord.Option`` objects in command signatures at runtime,
and PEP 563 would turn them into plain strings, silently degrading every option to
a required string.
"""

import logging

import discord
from discord.ext import commands

_log = logging.getLogger(__name__)


class General(commands.Cog):
    """Basic health-check and greeting commands."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @discord.slash_command(name="ping", description="Check whether the bot is responsive.")
    async def ping(self, ctx: discord.ApplicationContext) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await ctx.respond(f"Pong! `{latency_ms}ms`")

    @discord.slash_command(name="hello", description="Greet a member.")
    async def hello(
        self,
        ctx: discord.ApplicationContext,
        member: discord.Option(
            discord.Member,
            description="Who to greet. Defaults to you.",
            required=False,
        ),
    ) -> None:
        target = member or ctx.author
        await ctx.respond(f"Hello, {target.mention}!")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        _log.info("%s joined %s", member, member.guild)


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    bot.add_cog(General(bot))

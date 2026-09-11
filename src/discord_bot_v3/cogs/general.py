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
    """General event listeners."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        _log.info("%s joined %s", member, member.guild)


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    bot.add_cog(General(bot))

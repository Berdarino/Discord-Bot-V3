"""What the *server* sees when someone joins.

The counterpart to ``serverlog.py``, which writes to the log channel for your
eyes. This posts one public greeting and nothing else, which is why it survives
on a bot with no ``LOG_CHANNEL_ID`` and no database: a welcome should not
depend on either. The character writes the greeting when Ollama is reachable
and ``WELCOME_MESSAGE`` is posted when it is not, so that stays true of the
model too.

Three cogs listen to ``on_member_join`` and the split is deliberate — this one
greets, ``serverlog.py`` records it for you, and ``members.py`` writes the
database row. Each works when the other two are switched off.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
Pycord introspects the ``discord.Option`` objects in command signatures at runtime,
and PEP 563 would turn them into plain strings, silently degrading every option to
a required string.
"""

import logging

import discord
from discord.ext import commands

from ..services.chat import welcome_greeting

_log = logging.getLogger(__name__)


class General(commands.Cog):
    """Greet new members in the server's system channel."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Post a greeting, if there is a channel to post it in."""
        _log.info("%s joined %s", member, member.guild)

        channel = member.guild.system_channel
        template = self.bot.config.welcome_message
        # A blank WELCOME_MESSAGE switches the greeting off entirely, model
        # included: with nothing to fall back to, a model that is merely slow
        # would decide whether a setting meant "off".
        if not template or channel is None:
            return

        greeting = await self._greeting(member, template)
        if not greeting:
            return

        try:
            await channel.send(
                greeting,
                # A welcome may mention the member who just joined, and nothing
                # else -- never @everyone, whatever the configured text says.
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[member]
                ),
            )
        except discord.Forbidden:
            _log.warning("Cannot post the welcome in #%s (%s)", channel, member.guild)
        except discord.HTTPException:
            _log.exception("Could not welcome %s to %s", member.id, member.guild)

    async def _greeting(self, member: discord.Member, template: str) -> str:
        """The bot's own words if it can manage them, the configured line if not.

        A first impression is worth a generation, and the character is the
        whole point of the server. But nobody should be met with silence
        because a local model was unloaded, so this falls back rather than
        failing -- the same bargain the birthday greeting strikes.

        The mention goes on its own line above whatever the model wrote: the
        character answers in three short lines, and a name glued to the front
        of the first one reads as a fourth.
        """
        if self.bot.ollama is not None:
            spoken = await welcome_greeting(
                self.bot.ollama, name=member.display_name, guild=member.guild.name
            )
            if spoken:
                return f"{member.mention}\n{spoken}"
        return _format_welcome(template, member)


def _format_welcome(template: str, member: discord.Member) -> str:
    """Fill ``{member}`` and ``{guild}`` in, tolerating a malformed template.

    A stray brace in a hand-written setting should post the text as typed
    rather than raise inside a gateway handler.
    """
    if not template:
        return ""
    try:
        return template.format(member=member.mention, guild=member.guild.name)
    except KeyError, IndexError, ValueError:
        _log.warning("WELCOME_MESSAGE has an unknown placeholder; posting it verbatim")
        return template


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    bot.add_cog(General(bot))

"""Talking to the bot: an ``on_message`` listener over a local Ollama model.

Ported from V2's chat handler. The trigger rules are V2's, because they are the
thing that keeps the bot from answering every message in the server:

1. The author is not a bot, and it is not a DM.
2. The message does not carry ``@everyone`` or ``@here``.
3. It is *either* a reply to one of the bot's own messages, *or* it mentions
   the bot in ``CHAT_CHANNEL_ID``.

Rule 3 is what makes it feel like a person rather than a chatbot: you talk to
it by replying, exactly as you would to anyone else in the channel.

**One generation at a time.** A local model serves a single request; firing
three at once does not make three replies arrive sooner, it makes all three
arrive late and heats the machine. ``_lock`` serialises them, and Discord's
typing indicator covers the wait. That is the main structural difference from
talking to a hosted API, where you would just let them run concurrently.

**Failure is silent by design.** If Ollama is not running, the bot says
nothing rather than posting an error into the conversation. A chat bot that
announces its own stack traces to a room full of friends is worse than one that
occasionally does not answer. The reason is logged instead.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
See the note in ``general.py``.
"""

import asyncio
import logging
import re

import discord
from discord.ext import commands

from ..services.chat import (
    MAX_REPLY,
    MAX_TOKENS,
    TEMPERATURE,
    ChatMemory,
    build_system,
    detect_language,
    pin_language,
)
from ..services.members import MemberStore
from ..services.ollama import OllamaClient, OllamaError, OllamaModelMissingError

_log = logging.getLogger(__name__)

# Matches a mention of any user, with or without the legacy nickname `!`.
_MENTION = re.compile(r"<@!?\d+>")

# Nothing useful comes of asking a model to reply to an empty string, and a
# bare mention with no text is usually someone tagging for attention.
_MIN_PROMPT = 1


class Chat(commands.Cog):
    """Reply in character when spoken to."""

    def __init__(self, bot: discord.Bot, client: OllamaClient) -> None:
        self.bot = bot
        self.client = client
        self.memory = ChatMemory(bot.cache)
        # None without MySQL. Chat still works; the bot just does not know who
        # it is talking to, so everyone gets the same treatment.
        self.members = MemberStore(bot.db) if bot.db is not None else None
        self._lock = asyncio.Lock()

    def cog_unload(self) -> None:
        # The cog owns the aiohttp session, so it has to close it.
        self.bot.loop.create_task(self.client.close())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Answer when replied to, or when mentioned in the chat channel."""
        if not self._should_answer(message):
            return

        prompt = _strip_mentions(message.content)
        if len(prompt) < _MIN_PROMPT:
            return

        # Pinned per message, not per conversation: someone who switches to
        # English mid-thread should get English back, even though the
        # replayed history is still full of Chinese.
        language = detect_language(prompt)
        system = build_system(await self._describe(message), language)
        # The replayed turns are filtered to the same language. The system
        # instruction alone loses to a history in the other one -- measured, not
        # assumed; see `ChatMemory.history`.
        history = await self.memory.history(message.author.id, language=language)

        try:
            # The lock is held across the whole generation, so a busy channel
            # queues rather than thrashing a model that serves one at a time.
            # Typing wraps it, so the wait is visible while queued.
            async with message.channel.typing(), self._lock:
                reply = await self.client.chat(
                    system=system,
                    messages=[
                        *history,
                        {"role": "user", "content": pin_language(prompt, language)},
                    ],
                    max_tokens=MAX_TOKENS,
                    temperature=TEMPERATURE,
                )
        except OllamaModelMissingError:
            _log.exception("The configured model is not pulled; chat is unavailable")
            return
        except OllamaError:
            _log.exception("Could not get a reply from Ollama")
            return
        except discord.HTTPException:
            _log.exception("Could not open a typing indicator in #%s", message.channel)
            return

        try:
            sent = await message.reply(
                reply[:MAX_REPLY],
                # It answers in the character's voice; it should not be able to
                # ping the server because the model wrote something that parses
                # as a mention.
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            _log.exception("Could not post a reply in #%s", message.channel)
            return

        await self.memory.remember(message.author.id, asked=prompt, replied=reply)
        _log.info("Replied to %s in #%s (%s)", message.author, message.channel, sent.id)

    async def _describe(self, message: discord.Message) -> str | None:
        """Look up who this is, so the bot can treat them accordingly.

        Best-effort on purpose: a database that is down or a member with no row
        yet should cost the reply its flavour, not the reply itself.
        """
        if self.members is None:
            return None
        try:
            return await self.members.description(
                guild_id=message.guild.id, user_id=message.author.id
            )
        except Exception:
            _log.exception("Could not read the description for %s", message.author.id)
            return None

    def _should_answer(self, message: discord.Message) -> bool:
        """V2's trigger rules, in the order that rejects fastest."""
        if message.author.bot or message.guild is None:
            return False
        if message.mention_everyone:
            return False
        if self.bot.user is None:
            return False

        if _is_reply_to(message, self.bot.user.id):
            return True
        return (
            self.bot.config.chat_channel_id is not None
            and message.channel.id == self.bot.config.chat_channel_id
            and self.bot.user in message.mentions
        )

    @discord.slash_command(name="forget", description="Make the bot forget your conversation.")
    async def forget(self, ctx: discord.ApplicationContext) -> None:
        """Clear one person's history without touching anyone else's."""
        await self.memory.forget(ctx.author.id)
        await ctx.respond("Okay, forgotten.", ephemeral=True)


def _is_reply_to(message: discord.Message, user_id: int) -> bool:
    """Whether this message is a reply to a message from ``user_id``.

    ``resolved`` is ``None`` when Discord did not send the referenced message
    and a ``DeletedReferencedMessage`` when it is gone, so the type check is
    doing real work here rather than satisfying a linter.
    """
    reference = message.reference
    if reference is None:
        return False
    resolved = reference.resolved
    return isinstance(resolved, discord.Message) and resolved.author.id == user_id


def _strip_mentions(content: str) -> str:
    """Drop the mention that summoned the bot, leaving what was actually said."""
    return _MENTION.sub("", content or "").strip()


def setup(bot: discord.Bot) -> None:
    """Load only when an Ollama server is configured."""
    if bot.config.ollama_url is None:
        _log.warning("Chat disabled: OLLAMA_URL is not configured")
        return
    bot.add_cog(Chat(bot, OllamaClient(bot.config.ollama_url, bot.config.ollama_model)))

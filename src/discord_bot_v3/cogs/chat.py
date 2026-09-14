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
    Person,
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
        # Owned by the bot, which closes it; the birthday announcer uses the
        # same one.
        self.client = client
        self.memory = ChatMemory(bot.cache)
        # None without MySQL. Chat still works; the bot just does not know who
        # it is talking to, so everyone gets the same treatment.
        self.members = MemberStore(bot.db) if bot.db is not None else None
        self._lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Answer when replied to, or when mentioned in the chat channel."""
        if not self._should_answer(message):
            return

        prompt = _render_mentions(message, self.bot.user.id)
        if len(prompt) < _MIN_PROMPT:
            return

        # Pinned per message, not per conversation: someone who switches to
        # English mid-thread should get English back, even though the
        # replayed history is still full of Chinese.
        language = detect_language(prompt)
        speaker, others = await self._people(message)
        system = build_system(speaker, others, language)
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

    async def _people(self, message: discord.Message) -> tuple[Person | None, list[Person]]:
        """Who is talking, and who they are talking about.

        The second half is what lets the bot banter across the group rather
        than one-to-one: without it, "he never fixes anything" names someone it
        has never heard of.

        Best-effort on purpose: a database that is down, or people with no rows
        yet, should cost the reply its flavour, not the reply itself.
        """
        if self.members is None:
            return None, []

        mentioned = [
            user
            for user in message.mentions
            if user.id not in (message.author.id, self.bot.user.id) and not user.bot
        ]
        wanted = [message.author.id, *(u.id for u in mentioned)]

        try:
            found = await self.members.descriptions(guild_id=message.guild.id, user_ids=wanted)
        except Exception:
            _log.exception("Could not read member descriptions in %s", message.guild.id)
            return None, []

        speaker = None
        if message.author.id in found:
            speaker = Person(_name(message.author), found[message.author.id])
        others = [Person(_name(u), found[u.id]) for u in mentioned if u.id in found]
        return speaker, others

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


def _name(user: discord.abc.User) -> str:
    """What to call someone in the prompt: what the server calls them."""
    return getattr(user, "display_name", None) or user.name


def _render_mentions(message: discord.Message, bot_id: int) -> str:
    """Drop the bot's own mention; turn everyone else's into their name.

    Deleting every mention leaves the model a sentence full of holes -- "why
    does never fix anything" -- with no way to connect the descriptions it was
    handed to the person being complained about. Substituting the display name
    keeps the sentence intact and matches what the prompt calls them.
    """
    content = message.content or ""
    for user in message.mentions:
        if user.id == bot_id:
            continue
        content = re.sub(rf"<@!?{user.id}>", _name(user), content)
    return _MENTION.sub("", content).strip()


def setup(bot: discord.Bot) -> None:
    """Load only when an Ollama server is configured."""
    if bot.ollama is None:
        _log.warning("Chat disabled: OLLAMA_URL is not configured")
        return
    bot.add_cog(Chat(bot, bot.ollama))

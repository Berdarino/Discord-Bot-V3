"""The server log: one channel, and the formatting everything in it shares.

This owns the channel and how an entry reaches it. It deliberately knows
nothing about *which* events exist — the cogs decide what is worth logging,
this decides how it gets written.

Fails soft, like the rest of the storage layer. With ``LOG_CHANNEL_ID`` unset,
or pointing at something unusable, every call is a no-op and the bot carries on
unchanged: a log that can take the bot down is worse than no log. A permanent
failure (missing, invisible, not a guild text channel, cannot post) disables
logging after the first attempt rather than retrying on every event; a
transient HTTP error leaves it enabled to try again.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from typing import Any

import discord

_log = logging.getLogger(__name__)

# Discord caps an embed field value at 1024 characters and a whole embed at
# 6000. Leave room for the code fence, the marker, and backtick escaping.
MAX_FIELD = 900
TRUNCATED = "\n… (truncated)"

# Shown where message content should be but is not: an empty body almost always
# means the message predates the cache or the intent is off, not a blank message.
NO_CONTENT = "*empty — not cached, or the message content intent is off*"

# How far back to sift the audit log, and how recent an entry has to be to
# plausibly describe the event in hand. See `find_actor`.
AUDIT_LOOKBACK = 8
AUDIT_WINDOW = dt.timedelta(seconds=10)


class EventLog:
    """The log channel, resolved once and written to by every listener."""

    def __init__(self, bot: discord.Bot, channel_id: int | None) -> None:
        self._bot = bot
        self._channel_id = channel_id
        self._channel: discord.TextChannel | None = None
        self._disabled = channel_id is None

    @property
    def enabled(self) -> bool:
        """False once logging has given up, or was never configured."""
        return not self._disabled

    def is_log_channel(self, channel_id: int | None) -> bool:
        """Whether this is the log channel itself.

        Events there are skipped, or tidying the log would write more log and a
        deleted log entry would be reported as a deleted message.
        """
        return self._channel_id is not None and channel_id == self._channel_id

    async def _resolve(self) -> discord.TextChannel | None:
        if self._disabled:
            return None
        if self._channel is not None:
            return self._channel

        try:
            channel = self._bot.get_channel(self._channel_id) or await self._bot.fetch_channel(
                self._channel_id
            )
        except discord.NotFound, discord.Forbidden:
            _log.error(
                "LOG_CHANNEL_ID %s is missing or invisible; logging is off", self._channel_id
            )
            self._disabled = True
            return None
        except discord.HTTPException:
            # Transient. Leave logging enabled and try again on the next event.
            _log.exception("Could not resolve LOG_CHANNEL_ID %s", self._channel_id)
            return None

        if getattr(channel, "guild", None) is None or not hasattr(channel, "send"):
            _log.error(
                "LOG_CHANNEL_ID %s is not a guild text channel; logging is off", self._channel_id
            )
            self._disabled = True
            return None

        self._channel = channel
        return channel

    async def write(self, embed: discord.Embed, *, file: discord.File | None = None) -> None:
        """Post one entry, and stop trying if the channel will not take it."""
        channel = await self._resolve()
        if channel is None:
            return

        try:
            await channel.send(
                embed=embed,
                # `None` is the "no file" value here. `discord.MISSING` is not
                # None, so it reaches the isinstance check and is rejected.
                file=file,
                # Entries name people constantly; none of it should ping them.
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            _log.error("Cannot post in #%s; logging is now off", channel)
            self._disabled = True
            self._channel = None
        except discord.HTTPException:
            _log.exception("Could not write an entry to #%s", channel)


def quote(content: str) -> str:
    """Render message text as a code block, truncated to fit an embed field.

    The code block is what stops logged text from *acting*: someone else's
    markdown, mentions and links stay inert and readable in the log.
    """
    text = (content or "").strip()
    if not text:
        return NO_CONTENT

    # A nested fence would close the block early; a zero-width space breaks it
    # up without changing what the text looks like.
    text = text.replace("```", "`​``")
    if len(text) > MAX_FIELD:
        text = text[:MAX_FIELD] + TRUNCATED
    return f"```\n{text}\n```"


def clip(text: str, limit: int = MAX_FIELD) -> str:
    """Trim any rendered value to fit one embed field."""
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED)] + TRUNCATED


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def channel_url(guild_id: int, channel_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def describe(user: Any) -> str:
    """Name someone for a footer: readable name plus the id that is stable."""
    if user is None:
        return "unknown"
    return f"{user} · {user.id}"


def transcript(messages: list[discord.Message]) -> discord.File | None:
    """Write cached messages to a text file for attaching to a bulk-delete entry.

    A purge can span a hundred messages, which no embed can hold. A file keeps
    the whole thing readable and downloadable instead of truncating it to a
    tally, and sidesteps embed formatting entirely.
    """
    if not messages:
        return None

    lines = []
    for message in sorted(messages, key=lambda m: m.id):
        stamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"[{stamp}] {message.author} ({message.author.id}): {message.content}")
        lines.extend(f"    [attachment] {a.filename}" for a in message.attachments)

    buffer = io.BytesIO("\n".join(lines).encode("utf-8"))
    return discord.File(buffer, filename="deleted-messages.txt")


async def find_actor(
    guild: discord.Guild | None,
    *,
    actions: tuple[discord.AuditLogAction, ...],
    target_id: int | None = None,
    channel_id: int | None = None,
) -> discord.AuditLogEntry | None:
    """Ask the audit log who did something. Returns the entry, or None.

    This is a heuristic, and a weaker one than it looks. Discord's own docs
    describe these events in a few words each and specify none of the
    following; it is observed behaviour, so re-check it rather than trusting
    this comment:

    * **A self-action writes no entry.** Someone deleting their own message or
      leaving of their own accord leaves no audit trace at all.
    * **A bot's action writes no entry either** for message deletions, so
      another moderation bot's cleanup is invisible here.
    * **Entries target the person, not the thing.** Repeated actions by one
      moderator against one member are merged into a single entry with an
      incrementing ``count`` rather than a new entry each time, and a merged
      entry keeps its *original* timestamp. Once a spree runs past
      ``AUDIT_WINDOW`` the match fails and attribution degrades to "unrecorded"
      — on precisely the case worth catching. Widening the window trades that
      for false attribution instead.
    * It needs the View Audit Log permission and returns nothing without it.

    So ``None`` means "self-inflicted, bot-inflicted, merged out of the window,
    or unreadable" — never "nobody did it".
    """
    me = guild.me if guild is not None else None
    if me is None or not me.guild_permissions.view_audit_log:
        return None

    cutoff = discord.utils.utcnow() - AUDIT_WINDOW
    try:
        async for entry in guild.audit_logs(limit=AUDIT_LOOKBACK):
            # Newest first, so the first entry past the window ends the search.
            if entry.created_at < cutoff:
                return None
            if entry.action not in actions:
                continue
            if target_id is not None and (entry.target is None or entry.target.id != target_id):
                continue
            if channel_id is not None:
                extra_channel = getattr(entry.extra, "channel", None)
                if extra_channel is None or extra_channel.id != channel_id:
                    continue
            return entry
    except discord.Forbidden:
        return None
    except discord.HTTPException:
        _log.exception("Could not read the audit log in %s", guild)
    return None

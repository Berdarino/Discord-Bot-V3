"""Gateway event listeners: member joins, and an audit trail of message edits
and deletes.

The handlers here watch the guild rather than answer a command, so this cog
owns no slash commands at all.

**Edits and deletions are logged to one channel**, set by ``LOG_CHANNEL_ID``.
Without it the listeners resolve nothing and post nothing. The channel is
resolved once and cached; if it turns out to be missing, unreadable, or not a
guild text channel, logging switches itself off rather than failing on every
subsequent event. Activity *inside* the log channel is never logged, so
clearing the log cannot generate more log.

Only these two events reach that channel. The join greeting goes to the guild's
own system channel, and nothing else writes there.

**Reading message text needs the privileged ``message_content`` intent.** The
bot requests it in ``bot.py``, but Discord withholds it — and refuses the
gateway connection outright — unless *Message Content Intent* is also enabled
in the Developer Portal. Without it every ``content`` below is an empty string.

**Only cached messages carry content.** ``on_message_edit`` and
``on_message_delete`` fire solely for messages the bot still holds in memory,
so anything older arrives as an ``on_raw_*`` event instead. Those are logged
too, but reduced: no before-text on an edit, and no content at all on a delete,
because Discord's delete payload is three ids and nothing else. The audit log
is what recovers *who* on an uncached delete — see ``_audit_delete``.

A bulk delete (``/delete``, or any moderation purge) dispatches
``bulk_message_delete`` and nothing else — not one ``message_delete`` per
message, and not even a raw one. A purge is therefore **not logged at all**.
Closing that gap means listening to ``on_bulk_message_delete`` as well; it is a
deliberate omission rather than an oversight.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
Pycord introspects the ``discord.Option`` objects in command signatures at runtime,
and PEP 563 would turn them into plain strings, silently degrading every option to
a required string.
"""

import datetime as dt
import logging
from typing import Any

import discord
from discord.ext import commands

_log = logging.getLogger(__name__)

# Discord caps an embed field value at 1024 characters. Leave room for the code
# fence, the truncation marker, and any backtick escaping.
_MAX_FIELD = 900
_TRUNCATED = "\n… (truncated)"

# Shown where content should be but is not: an empty message body almost always
# means the privileged intent is missing rather than a genuinely blank message.
_NO_CONTENT = "*empty — or the message content intent is not enabled*"

# How many audit entries to sift, and how recent one has to be to plausibly
# describe *this* deletion rather than an older, unrelated one.
_AUDIT_LOOKBACK = 5
_AUDIT_WINDOW = dt.timedelta(seconds=10)

# Attachment filenames are logged, never their URLs: Discord's CDN links stop
# resolving once the message is gone, so a saved URL is a dead link.
_MAX_ATTACHMENTS = 10


class General(commands.Cog):
    """Greet new members, and log edited and deleted messages to one channel."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._channel: discord.TextChannel | None = None
        # Set once the channel proves unusable, so a misconfigured id does not
        # mean a failed API call on every single edit in every guild.
        self._disabled = bot.config.log_channel_id is None

    # ------------------------------------------------------------------ joins

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Greet a new member in the guild's system channel.

        Deliberately not the log channel: a welcome is for the server, the log
        is for whoever is watching it. The member's own database record is
        written by the ``Members`` cog, which listens to this event too, so
        joins are still greeted on a bot running without MySQL.
        """
        _log.info("%s joined %s", member, member.guild)

        greeting = _format_welcome(self.bot.config.welcome_message, member)
        channel = member.guild.system_channel
        if not greeting or channel is None:
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

    # --------------------------------------------------------------- messages

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Report an edit, with the text on both sides of it."""
        if not _is_loggable(before) or self._is_log_channel(before.channel.id):
            return
        # Discord also fires this when a link unfurls into an embed or the
        # message is pinned. Neither touched the text, and neither is an edit.
        if before.content == after.content:
            return
        await self._write(_edit_embed(before, after))

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        """Report a delete, naming who did it when the audit log says so."""
        if not _is_loggable(message) or self._is_log_channel(message.channel.id):
            return
        deleter, _ = await _audit_delete(
            message.guild, channel_id=message.channel.id, author_id=message.author.id
        )
        await self._write(_delete_embed(message, deleter))

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Report an edit to a message the bot never had in memory."""
        if payload.cached_message is not None or payload.guild_id is None:
            return
        if self._is_log_channel(payload.channel_id) or not _is_user_edit(payload.data):
            return
        await self._write(_raw_edit_embed(payload))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Report a delete of a message the bot never had in memory.

        The payload names nobody, so the audit log is the only way to answer
        "who deleted whose message" here. It is asked without an author to
        match against, which makes the answer weaker — see ``_audit_delete``.
        """
        if payload.cached_message is not None or payload.guild_id is None:
            return
        if self._is_log_channel(payload.channel_id):
            return

        guild = self.bot.get_guild(payload.guild_id)
        deleter, author = await _audit_delete(guild, channel_id=payload.channel_id)
        await self._write(_raw_delete_embed(payload, deleter, author))

    # -------------------------------------------------------------- log sink

    def _is_log_channel(self, channel_id: int) -> bool:
        """Never log activity in the log channel itself.

        Otherwise tidying the log would write more log, and a deleted log entry
        would be reported as a deleted message.
        """
        return channel_id == self.bot.config.log_channel_id

    async def _resolve(self) -> discord.TextChannel | None:
        """Find the log channel once, then reuse it."""
        if self._disabled:
            return None
        if self._channel is not None:
            return self._channel

        channel_id = self.bot.config.log_channel_id
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except discord.NotFound, discord.Forbidden:
            _log.error("LOG_CHANNEL_ID %s is missing or invisible; logging is off", channel_id)
            self._disabled = True
            return None
        except discord.HTTPException:
            # Transient. Leave logging enabled and try again on the next event.
            _log.exception("Could not resolve LOG_CHANNEL_ID %s", channel_id)
            return None

        if getattr(channel, "guild", None) is None or not hasattr(channel, "send"):
            _log.error("LOG_CHANNEL_ID %s is not a guild text channel; logging is off", channel_id)
            self._disabled = True
            return None

        self._channel = channel
        return channel

    async def _write(self, embed: discord.Embed) -> None:
        """Post one entry, and stop trying if the channel will not take it."""
        channel = await self._resolve()
        if channel is None:
            return

        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            _log.error("Cannot post in #%s; message logging is now off", channel)
            self._disabled = True
            self._channel = None
        except discord.HTTPException:
            _log.exception("Could not write a message log to #%s", channel)


def _is_loggable(message: discord.Message) -> bool:
    """Log human messages sent in a guild, and nothing else."""
    return message.guild is not None and not message.author.bot


def _is_user_edit(data: dict[str, Any]) -> bool:
    """Decide whether a raw MESSAGE_UPDATE payload describes a real edit.

    The same gateway event covers embed unfurls and pins. Only an edit made by
    a person sets ``edited_timestamp``, so its absence is the filter -- and the
    partial payload is also the only place a raw event names the author.
    """
    if data.get("edited_timestamp") is None:
        return False
    author = data.get("author") or {}
    return not author.get("bot", False)


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


def _quote(content: str) -> str:
    """Render message text as a code block, truncated to fit an embed field.

    A code block is what stops a logged message from *acting* -- someone else's
    markdown, mentions and links stay inert and readable in the log.
    """
    text = content.strip()
    if not text:
        return _NO_CONTENT

    # A nested fence would close the block early; a zero-width space breaks it
    # up without changing what the text looks like.
    text = text.replace("```", "`​``")
    if len(text) > _MAX_FIELD:
        text = text[:_MAX_FIELD] + _TRUNCATED
    return f"```\n{text}\n```"


def _attachment_list(message: discord.Message) -> str:
    """Name the attachments. Their URLs die with the message, so they are omitted."""
    names = [discord.utils.escape_markdown(a.filename) for a in message.attachments]
    shown = names[:_MAX_ATTACHMENTS]
    if len(names) > _MAX_ATTACHMENTS:
        shown.append(f"…and {len(names) - _MAX_ATTACHMENTS} more")
    return "\n".join(f"- {name}" for name in shown)[:1024]


def _jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _footer(message: discord.Message) -> str:
    """Identify the author and the server, so one channel can watch several."""
    guild = message.guild.name if message.guild is not None else "unknown server"
    return f"{message.author} · {message.author.id} · {guild}"


async def _audit_delete(
    guild: discord.Guild | None,
    *,
    channel_id: int,
    author_id: int | None = None,
) -> tuple[discord.User | None, discord.User | None]:
    """Ask the audit log who deleted a message, and whose it was.

    Returns ``(deleter, author)``; either may be ``None``.

    This is a heuristic, and a weaker one than it looks. Discord's own docs
    describe action 72 as, in full, "Single message was deleted" -- none of what
    follows is specified anywhere, it is observed behaviour, so re-check it
    rather than trusting this comment:

    * **A self-delete writes no entry.** Someone deleting their own message
      leaves no audit trace at all.
    * **A bot's delete writes no entry either.** Another moderation bot
      removing a message is exactly as invisible as a self-delete.
    * **The entry targets the message's author, not the message.** Repeated
      deletions by one moderator against one author are merged into a single
      entry with an incrementing ``count``, rather than a new entry each time.
      A merged entry keeps its original ``created_at``, so ``_AUDIT_WINDOW``
      will reject it once the spree runs past ten seconds and attribution
      quietly degrades to "unrecorded" -- on precisely the case worth catching.
      Widening the window trades that for false attribution instead; matching
      properly would mean tracking each entry's ``count`` between events.
    * It needs the View Audit Log permission, and returns nothing without it.

    So ``(None, None)`` means "self-deleted, bot-deleted, merged out of the
    window, or unreadable" -- never "nobody did it".

    With ``author_id`` (the message was cached) the entry must name that same
    author, which makes a match strong evidence. Without it -- an uncached
    delete, where the payload names nobody -- the only available evidence is
    "an entry for this channel, seconds ago", so the returned author is the
    audit log's best guess rather than a certainty.
    """
    me = guild.me if guild is not None else None
    if me is None or not me.guild_permissions.view_audit_log:
        return None, None

    cutoff = discord.utils.utcnow() - _AUDIT_WINDOW
    try:
        async for entry in guild.audit_logs(
            limit=_AUDIT_LOOKBACK, action=discord.AuditLogAction.message_delete
        ):
            # Newest first, so the first entry older than the window ends it.
            if entry.created_at < cutoff:
                break
            channel = getattr(entry.extra, "channel", None)
            if channel is None or channel.id != channel_id:
                continue
            if author_id is not None and (entry.target is None or entry.target.id != author_id):
                continue
            return entry.user, entry.target
    except discord.Forbidden:
        return None, None
    except discord.HTTPException:
        _log.exception("Could not read the audit log in %s", guild)
    return None, None


def _edit_embed(before: discord.Message, after: discord.Message) -> discord.Embed:
    """Describe an edit to a message the bot had cached."""
    embed = discord.Embed(
        title="Message edited",
        colour=discord.Colour.gold(),
        timestamp=discord.utils.utcnow(),
        description=(
            f"{after.author.mention} edited a message in {after.channel.mention}\n"
            f"[Jump to message]({after.jump_url})"
        ),
    )
    embed.add_field(name="Before", value=_quote(before.content), inline=False)
    embed.add_field(name="After", value=_quote(after.content), inline=False)
    embed.set_footer(text=_footer(after))
    return embed


def _delete_embed(message: discord.Message, deleter: discord.User | None) -> discord.Embed:
    """Describe a delete of a message the bot had cached."""
    by = deleter.mention if deleter is not None else "**the author, or someone unrecorded**"
    embed = discord.Embed(
        title="Message deleted",
        colour=discord.Colour.red(),
        timestamp=discord.utils.utcnow(),
        description=(
            f"A message from {message.author.mention} in {message.channel.mention} "
            f"was deleted by {by}."
        ),
    )
    embed.add_field(name="Content", value=_quote(message.content), inline=False)
    if message.attachments:
        embed.add_field(name="Attachments", value=_attachment_list(message), inline=False)
    embed.set_footer(text=_footer(message))
    return embed


def _raw_edit_embed(payload: discord.RawMessageUpdateEvent) -> discord.Embed:
    """Describe an edit to a message that was not in the cache."""
    data = payload.data
    author_id = (data.get("author") or {}).get("id")
    who = f"<@{author_id}>" if author_id else "An unknown author"
    link = _jump_url(payload.guild_id, payload.channel_id, payload.message_id)

    embed = discord.Embed(
        title="Message edited (not cached)",
        colour=discord.Colour.dark_gold(),
        timestamp=discord.utils.utcnow(),
        description=(
            f"{who} edited a message in <#{payload.channel_id}>\n"
            f"[Jump to message]({link})\n"
            "The previous version predates this session, so only the new text is known."
        ),
    )
    embed.add_field(name="After", value=_quote(str(data.get("content") or "")), inline=False)
    embed.set_footer(text=f"Message {payload.message_id}")
    return embed


def _raw_delete_embed(
    payload: discord.RawMessageDeleteEvent,
    deleter: discord.User | None,
    author: discord.User | None,
) -> discord.Embed:
    """Describe a delete of a message that was not in the cache.

    Discord's MESSAGE_DELETE payload is three ids and nothing more, so whatever
    the audit log gives back is the entire answer to "who deleted what". The
    wording says so rather than presenting a guess as a fact.
    """
    link = _jump_url(payload.guild_id, payload.channel_id, payload.message_id)
    if deleter is not None:
        whose = f"{author.mention}'s" if author is not None else "someone's"
        opening = (
            f"The audit log says {deleter.mention} deleted {whose} message "
            f"in <#{payload.channel_id}>."
        )
    else:
        opening = (
            f"A message was deleted in <#{payload.channel_id}>, by its own author "
            "or by someone the audit log did not record."
        )

    embed = discord.Embed(
        title="Message deleted (not cached)",
        colour=discord.Colour.dark_red(),
        timestamp=discord.utils.utcnow(),
        description=(
            f"{opening}\n[Jump to channel]({link})\n"
            "It predates this session, so its content is unknown."
        ),
    )
    embed.set_footer(text=f"Message {payload.message_id}")
    return embed


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    bot.add_cog(General(bot))

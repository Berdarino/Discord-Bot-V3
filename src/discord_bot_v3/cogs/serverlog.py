"""Everything that writes to the log channel.

``general.py`` talks to the *server* — the welcome a new member sees. This cog
talks to *you*: who joined, who left and how, who renamed, who edited or
deleted what, and every moderator action the audit log records. All of it lands
in ``LOG_CHANNEL_ID``; without that setting nothing here does anything.

**Two sources, deliberately not merged.** Discord tells us about an event twice
over: once as a specific gateway event (``on_message_delete``,
``on_member_remove``) carrying *what* happened, and once as an audit log entry
carrying *who* did it. ``on_audit_log_entry`` is close to a superset of the
moderation events, so listening to both naively would double-log almost
everything. The split used here:

* Handlers below own the six actions where they say more than the audit entry
  would — they have the message content, the old nickname, the roles. Those
  actions are in ``_AUDIT_HANDLED`` and the generic renderer skips them, doing
  its own ``find_actor`` lookup to name the moderator.
* ``on_audit_log_entry`` renders everything else — bans lifted, roles and
  channels created and edited, invites, webhooks, automod — generically, from
  the entry's own before/after diff. New Discord features land in the log for
  free rather than needing a listener each.

**What the log cannot see.** Message *edits* are never audit-logged, so an
edit's author is whoever the message belongs to and there is nothing to
attribute. Self-deletes and bot-deletes write no audit entry at all, so a
deletion often has no recorded actor — see ``find_actor``. And
``on_audit_log_entry`` only fires when the acting user is already in the cache;
otherwise only the raw form does, which this cog does not listen to.

**Cached versus not.** ``on_message_edit`` and ``on_message_delete`` fire only
for messages still in Pycord's cache (``MESSAGE_CACHE`` in ``bot.py``).
Everything older arrives as an ``on_raw_*`` event and is logged in reduced
form: no before-text on an edit, no content at all on a delete, because
Discord's delete payload is three ids and nothing else.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
See the note in ``general.py``.
"""

import logging
from typing import Any

import discord
from discord.ext import commands

from ..services.eventlog import (
    EventLog,
    channel_url,
    clip,
    describe,
    find_actor,
    jump_url,
    quote,
    transcript,
)

_log = logging.getLogger(__name__)

# Actions a dedicated handler below reports better than the generic renderer
# would, so the generic renderer leaves them alone. Keep this in step with the
# listeners: an action removed from here starts being logged twice.
_AUDIT_HANDLED = frozenset(
    {
        discord.AuditLogAction.message_delete,
        discord.AuditLogAction.message_bulk_delete,
        discord.AuditLogAction.kick,
        discord.AuditLogAction.ban,
        discord.AuditLogAction.member_update,
        discord.AuditLogAction.member_role_update,
    }
)

# Roles and changed fields are listed, not dumped: a role purge or a mass
# permission edit would otherwise overflow the embed.
_MAX_LISTED = 10
_MAX_ATTACHMENTS = 10


class ServerLog(commands.Cog):
    """Write member, message and moderation activity to the log channel."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.log = EventLog(bot, bot.config.log_channel_id)

    # ---------------------------------------------------------------- members

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Record a join, flagging an account young enough to be a throwaway."""
        if member.bot:
            return
        embed = _embed(
            "Member joined",
            discord.Colour.green(),
            f"{member.mention} joined the server.",
        )
        embed.add_field(
            name="Account created",
            value=discord.utils.format_dt(member.created_at, "R"),
            inline=True,
        )
        embed.add_field(name="Members", value=str(member.guild.member_count), inline=True)
        embed.set_footer(text=f"{describe(member)} · {member.guild.name}")
        await self.log.write(embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Record a departure, and say whether they left or were removed.

        Discord sends the same ``GUILD_MEMBER_REMOVE`` for a leave, a kick and
        a ban, so the three are indistinguishable from the event alone. Unlike
        a self-deleted message, though, kicks and bans *are* reliably audit
        logged, so the lookup usually settles it.
        """
        if member.bot:
            return

        entry = await find_actor(
            member.guild,
            actions=(discord.AuditLogAction.kick, discord.AuditLogAction.ban),
            target_id=member.id,
        )
        if entry is None:
            title, colour, what = "Member left", discord.Colour.orange(), "left the server"
        elif entry.action is discord.AuditLogAction.ban:
            title, colour = "Member banned", discord.Colour.dark_red()
            what = f"was banned by {entry.user.mention}"
        else:
            title, colour = "Member kicked", discord.Colour.red()
            what = f"was kicked by {entry.user.mention}"

        embed = _embed(title, colour, f"{member.mention} {what}.")
        if entry is not None and entry.reason:
            embed.add_field(name="Reason", value=clip(entry.reason), inline=False)
        if member.joined_at is not None:
            embed.add_field(
                name="Joined", value=discord.utils.format_dt(member.joined_at, "R"), inline=True
            )
        embed.add_field(name="Members", value=str(member.guild.member_count), inline=True)

        roles = [role.mention for role in member.roles if not role.is_default()]
        if roles:
            embed.add_field(name="Roles held", value=_listing(roles), inline=False)
        embed.set_footer(text=f"{describe(member)} · {member.guild.name}")
        await self.log.write(embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Record a nickname, role or timeout change, and who made it.

        Also keeps the member directory honest: ``display_name`` is stored at
        join and at startup, so without this a rename goes stale until the next
        restart.
        """
        if after.bot:
            return

        changes: list[tuple[str, str]] = []
        actions: list[discord.AuditLogAction] = []

        if before.nick != after.nick:
            changes.append(("Nickname", f"{_name(before.nick)} → {_name(after.nick)}"))
            actions.append(discord.AuditLogAction.member_update)

        gained = [r.mention for r in after.roles if r not in before.roles]
        lost = [r.mention for r in before.roles if r not in after.roles]
        if gained:
            changes.append(("Roles added", _listing(gained)))
        if lost:
            changes.append(("Roles removed", _listing(lost)))
        if gained or lost:
            actions.append(discord.AuditLogAction.member_role_update)

        if before.communication_disabled_until != after.communication_disabled_until:
            until = after.communication_disabled_until
            value = discord.utils.format_dt(until, "R") if until is not None else "lifted"
            changes.append(("Timeout", value))
            actions.append(discord.AuditLogAction.member_update)

        if not changes:
            # Fires for presence and avatar churn too; nothing we log changed.
            return

        entry = await find_actor(after.guild, actions=tuple(actions), target_id=after.id)
        by = (
            f" by {entry.user.mention}"
            if entry is not None and entry.user is not None and entry.user.id != after.id
            else ""
        )
        embed = _embed(
            "Member updated", discord.Colour.blurple(), f"{after.mention} was updated{by}."
        )
        for name, value in changes:
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text=f"{describe(after)} · {after.guild.name}")
        await self.log.write(embed)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        """Record a global username or display-name change.

        This is the account itself changing, not a per-server nickname, so
        there is no guild and no audit entry — a person renaming themselves is
        never a moderator action.
        """
        if after.bot:
            return

        changes = []
        if before.name != after.name:
            changes.append(("Username", f"{_name(before.name)} → {_name(after.name)}"))
        if before.global_name != after.global_name:
            changes.append(
                ("Display name", f"{_name(before.global_name)} → {_name(after.global_name)}")
            )
        if not changes:
            # Avatar and banner changes land here too, and are not worth a row.
            return

        embed = _embed("User renamed", discord.Colour.blurple(), f"{after.mention} changed name.")
        for name, value in changes:
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text=describe(after))
        await self.log.write(embed)

    # --------------------------------------------------------------- messages

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Report an edit, with the text on both sides of it."""
        if not self._loggable(before):
            return
        # Discord fires this when a link unfurls into an embed or a message is
        # pinned. Neither touched the text, and neither is an edit.
        if before.content == after.content:
            return

        embed = _embed(
            "Message edited",
            discord.Colour.gold(),
            f"{after.author.mention} edited a message in {after.channel.mention}\n"
            f"[Jump to message]({after.jump_url})",
        )
        embed.add_field(name="Before", value=quote(before.content), inline=False)
        embed.add_field(name="After", value=quote(after.content), inline=False)
        embed.set_footer(text=f"{describe(after.author)} · {after.guild.name}")
        await self.log.write(embed)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        """Report a delete, naming who did it when the audit log says so."""
        if not self._loggable(message):
            return

        entry = await find_actor(
            message.guild,
            actions=(discord.AuditLogAction.message_delete,),
            target_id=message.author.id,
            channel_id=message.channel.id,
        )
        by = (
            entry.user.mention
            if entry is not None and entry.user is not None
            else "**the author, or someone unrecorded**"
        )
        embed = _embed(
            "Message deleted",
            discord.Colour.red(),
            f"A message from {message.author.mention} in {message.channel.mention} "
            f"was deleted by {by}.",
        )
        embed.add_field(name="Content", value=quote(message.content), inline=False)
        if message.attachments:
            names = [discord.utils.escape_markdown(a.filename) for a in message.attachments]
            # Filenames, never URLs: Discord's CDN links die with the message.
            embed.add_field(name="Attachments", value=_listing(names), inline=False)
        embed.set_footer(text=f"{describe(message.author)} · {message.guild.name}")
        await self.log.write(embed)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Report an edit to a message the bot never had in memory."""
        if payload.guild_id is None or payload.cached_message is not None:
            return
        if self.log.is_log_channel(payload.channel_id) or not _is_user_edit(payload.data):
            return

        author_id = (payload.data.get("author") or {}).get("id")
        who = f"<@{author_id}>" if author_id else "An unknown author"
        link = jump_url(payload.guild_id, payload.channel_id, payload.message_id)
        embed = _embed(
            "Message edited (not cached)",
            discord.Colour.dark_gold(),
            f"{who} edited a message in <#{payload.channel_id}>\n"
            f"[Jump to message]({link})\n"
            "The previous version predates this session, so only the new text is known.",
        )
        embed.add_field(
            name="After", value=quote(str(payload.data.get("content") or "")), inline=False
        )
        embed.set_footer(text=f"Message {payload.message_id}")
        await self.log.write(embed)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Report a delete of a message the bot never had in memory.

        The payload names nobody, so the audit log is the whole answer to "who
        deleted what" here — and it is asked without an author to match
        against, which makes it weaker than the cached path.
        """
        if payload.guild_id is None or payload.cached_message is not None:
            return
        if self.log.is_log_channel(payload.channel_id):
            return

        entry = await find_actor(
            self.bot.get_guild(payload.guild_id),
            actions=(discord.AuditLogAction.message_delete,),
            channel_id=payload.channel_id,
        )
        link = channel_url(payload.guild_id, payload.channel_id)
        if entry is not None and entry.user is not None:
            whose = f"{entry.target.mention}'s" if entry.target is not None else "someone's"
            opening = (
                f"The audit log says {entry.user.mention} deleted {whose} message "
                f"in <#{payload.channel_id}>."
            )
        else:
            opening = (
                f"A message was deleted in <#{payload.channel_id}>, by its own author "
                "or by someone the audit log did not record."
            )

        embed = _embed(
            "Message deleted (not cached)",
            discord.Colour.dark_red(),
            f"{opening}\n[Jump to channel]({link})\n"
            "It predates this session, so its content is unknown.",
        )
        embed.set_footer(text=f"Message {payload.message_id}")
        await self.log.write(embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        """Report a purge, with whatever of it was still cached.

        A bulk delete dispatches this and nothing else — not one
        ``message_delete`` per message — so without this listener ``/delete``
        and every moderator purge pass silently. The raw form is used rather
        than ``on_bulk_message_delete`` because it knows the true total even
        when only some of the messages were cached.

        The cached ones go out as a text file. A purge can span a hundred
        messages, which no embed can hold, and truncating them to a tally would
        throw away the only copy of the content that exists.
        """
        if payload.guild_id is None or self.log.is_log_channel(payload.channel_id):
            return

        entry = await find_actor(
            self.bot.get_guild(payload.guild_id),
            actions=(discord.AuditLogAction.message_bulk_delete,),
            channel_id=payload.channel_id,
        )
        by = (
            f" by {entry.user.mention}"
            if entry is not None and entry.user is not None
            else " by someone the audit log did not record"
        )
        cached = [m for m in payload.cached_messages if not m.author.bot]
        embed = _embed(
            "Messages bulk deleted",
            discord.Colour.dark_red(),
            f"**{len(payload.message_ids)}** message(s) were deleted in "
            f"<#{payload.channel_id}>{by}.",
        )
        if cached:
            tally: dict[Any, int] = {}
            for message in cached:
                tally[message.author] = tally.get(message.author, 0) + 1
            embed.add_field(
                name="Authors",
                value=_listing([f"{author.mention} — {count}" for author, count in tally.items()]),
                inline=False,
            )
        embed.add_field(
            name="Recovered",
            value=f"{len(cached)} of {len(payload.message_ids)} were cached"
            + (" and are attached." if cached else "; the rest are unrecoverable."),
            inline=False,
        )
        await self.log.write(embed, file=transcript(cached))

    # ------------------------------------------------------------------ audit

    @commands.Cog.listener()
    async def on_audit_log_entry(self, entry: discord.AuditLogEntry) -> None:
        """Render every moderator action no handler above already covers.

        Discord pushes these over the gateway as they happen, so this needs no
        polling and no permission round trip — but it only fires when the
        acting user is already cached, and it carries no message content.
        """
        if entry.action in _AUDIT_HANDLED:
            return

        actor = entry.user.mention if entry.user is not None else "Someone"
        embed = _embed(
            _action_name(entry.action),
            discord.Colour.purple(),
            f"{actor} performed **{_action_name(entry.action).lower()}**"
            + (f" on {_target_name(entry.target)}" if entry.target is not None else "")
            + ".",
        )
        if entry.reason:
            embed.add_field(name="Reason", value=clip(entry.reason), inline=False)

        changed = _changes(entry)
        if changed:
            embed.add_field(name="Changes", value=changed, inline=False)
        embed.set_footer(text=f"{describe(entry.user)} · {entry.guild.name}")
        await self.log.write(embed)

    # ----------------------------------------------------------------- shared

    def _loggable(self, message: discord.Message) -> bool:
        """Log human messages sent in a guild, outside the log channel itself."""
        return (
            message.guild is not None
            and not message.author.bot
            and not self.log.is_log_channel(message.channel.id)
        )


def _embed(title: str, colour: discord.Colour, description: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        colour=colour,
        description=description,
        timestamp=discord.utils.utcnow(),
    )


def _listing(items: list[str]) -> str:
    """Render a bullet list, collapsing a long tail rather than overflowing."""
    shown = items[:_MAX_LISTED]
    if len(items) > _MAX_LISTED:
        shown.append(f"…and {len(items) - _MAX_LISTED} more")
    return clip("\n".join(f"- {item}" for item in shown), 1024)


def _name(value: str | None) -> str:
    """Render a name that may be unset, without letting it style the entry."""
    return f"`{discord.utils.escape_markdown(value)}`" if value else "*none*"


def _action_name(action: discord.AuditLogAction) -> str:
    """Turn `member_role_update` into `Member role update`."""
    return action.name.replace("_", " ").capitalize()


def _target_name(target: Any) -> str:
    """Name whatever an entry points at, which may be a user, role or channel."""
    mention = getattr(target, "mention", None)
    if mention is not None:
        return mention
    name = getattr(target, "name", None)
    return f"`{discord.utils.escape_markdown(str(name))}`" if name else "something"


def _changes(entry: discord.AuditLogEntry) -> str:
    """Render an entry's before/after diff, field by field.

    ``AuditLogDiff`` iterates as ``(attribute, value)`` pairs and holds only
    what actually changed, so this stays empty for actions that carry no diff.
    """
    before = dict(entry.changes.before)
    after = dict(entry.changes.after)
    lines = []
    for key in list(before) + [k for k in after if k not in before]:
        old, new = _render(before.get(key)), _render(after.get(key))
        if old == new:
            continue
        lines.append(f"- **{key.replace('_', ' ')}**: {old} → {new}")
        if len(lines) >= _MAX_LISTED:
            break
    return clip("\n".join(lines), 1024)


def _render(value: Any) -> str:
    """Flatten one side of a diff to something an embed field can hold."""
    if value is None:
        return "*none*"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_target_name(item) for item in value[:_MAX_LISTED]) or "*none*"
    text = getattr(value, "name", None) or str(value)
    return f"`{discord.utils.escape_markdown(str(text))[:120]}`"


def _is_user_edit(data: dict[str, Any]) -> bool:
    """Decide whether a raw MESSAGE_UPDATE payload describes a real edit.

    The same gateway event covers embed unfurls and pins. Only an edit made by
    a person sets ``edited_timestamp``, so its absence is the filter — and the
    partial payload is also the only place a raw event names the author.
    """
    if data.get("edited_timestamp") is None:
        return False
    author = data.get("author") or {}
    return not author.get("bot", False)


def setup(bot: discord.Bot) -> None:
    """Load only when a log channel is configured."""
    if bot.config.log_channel_id is None:
        _log.warning("Server logging disabled: LOG_CHANNEL_ID is not configured")
        return
    bot.add_cog(ServerLog(bot))

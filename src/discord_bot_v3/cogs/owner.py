"""Owner-only administrative commands.

Every command here is gated by ``commands.is_owner()``. The owner is whoever
owns the application in the Discord Developer Portal: Pycord fetches that from
the API on the first check and caches it, so there is nothing to configure.

Discord still shows these commands to everyone; the check is what refuses them,
and ``on_application_command_error`` turns the resulting ``NotOwner`` into a
polite ephemeral reply.

Both commands go through Discord's own UI rather than answering in one shot.
``/send`` puts the whole thing in one modal — channel picker and body — because
slash-command options are single-line and could never hold a multi-line
announcement; submitting the modal *is* the confirmation. ``/delete`` asks for
an explicit confirmation instead, since a purge cannot be undone.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
See the note in ``general.py``.
"""

import logging
from collections import Counter

import discord
from discord.ext import commands

from ..ui import ConfirmView

_log = logging.getLogger(__name__)

# Discord's hard limit on a message body.
MAX_MESSAGE_LENGTH = 2000

# Channels /send may post to. Voice channels carry a built-in text chat and are
# Messageable like any other, so they belong here alongside text and
# announcement channels.
_SENDABLE_CHANNELS = (
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.voice,
)

# Discord has no "bot owner" permission, so it would otherwise list these
# commands for everyone. Requiring Administrator hides them from ordinary
# members; ``is_owner`` is still what actually refuses them. Server admins can
# override this per role in Server Settings -> Integrations.
_OWNER_ONLY = discord.Permissions(administrator=True)

# Bulk delete is capped at 100 messages per call, and "delete up to here" could
# otherwise span a whole channel if an old message is picked.
_MAX_CONTEXT_DELETE = 100

# How many distinct authors to name in a /delete summary before collapsing the
# rest into a "+N others" line, so the reply cannot exceed the message limit.
_MAX_SUMMARY_AUTHORS = 15


class SendModal(discord.ui.DesignerModal):
    """Picks the target channel *and* composes the body for ``/send``.

    A ``DesignerModal`` rather than the legacy ``Modal`` because that is what
    accepts components other than text inputs — here a channel select — each
    wrapped in a ``Label``. The whole command is therefore one dialog: nothing
    is chosen while typing the command.

    A modal is also the only way to write a multi-line message. Slash-command
    string options are single-line inputs, so an option could never hold one.
    """

    def __init__(self) -> None:
        super().__init__(title="Send a message")

        self.channel_select = discord.ui.Select(
            select_type=discord.ComponentType.channel_select,
            channel_types=list(_SENDABLE_CHANNELS),
            placeholder="Pick a channel",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(discord.ui.Label("Channel", self.channel_select))

        # No label of its own: the wrapping Label supplies it.
        self.body = discord.ui.InputText(
            style=discord.InputTextStyle.long,
            placeholder="Markdown works. Line breaks are kept.",
            max_length=MAX_MESSAGE_LENGTH,
        )
        self.add_item(discord.ui.Label("Message", self.body))

    async def callback(self, interaction: discord.Interaction) -> None:
        """Send the composed message. Submitting the modal is the confirmation."""
        # Resolved from the interaction's payload into real channel objects,
        # which needs a guild — hence the guard on the command itself.
        selected = self.channel_select.values or []
        channel = selected[0] if selected else None
        if channel is None:
            await interaction.response.send_message("No channel was selected.", ephemeral=True)
            return

        content = (self.body.value or "").strip()
        if not content:
            await interaction.response.send_message(
                "Nothing to send — the message was empty.", ephemeral=True
            )
            return

        await _deliver(interaction, channel, content)


class Owner(commands.Cog):
    """Commands only the bot owner may run."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @discord.slash_command(
        name="send",
        default_member_permissions=_OWNER_ONLY,
        description="Send a message to a channel as the bot. Owner only.",
    )
    @commands.is_owner()
    async def send(self, ctx: discord.ApplicationContext) -> None:
        """Open the compose modal. The modal does the rest."""
        if ctx.guild is None:
            await ctx.respond("Use this in a server.", ephemeral=True)
            return

        # A modal must be the *first* response to an interaction, so there is
        # nothing to defer and no permission pre-check to do here.
        await ctx.send_modal(SendModal())

    @discord.slash_command(
        name="delete",
        default_member_permissions=_OWNER_ONLY,
        description="Bulk delete recent messages in this channel. Owner only.",
    )
    @commands.is_owner()
    async def delete(
        self,
        ctx: discord.ApplicationContext,
        message_count: discord.Option(
            int,
            description="How many recent messages to delete.",
            min_value=1,
            max_value=100,
        ),
    ) -> None:
        """Purge the last ``message_count`` messages, once confirmed."""
        if ctx.guild is None:
            await ctx.respond("Messages can only be bulk deleted in a server.", ephemeral=True)
            return

        view = ConfirmView(
            ctx.interaction,
            user_id=ctx.author.id,
            confirm_label="Delete",
            timeout_message="Timed out — nothing was deleted.",
        )
        await ctx.respond(
            f"Delete the last **{message_count}** message(s) in {ctx.channel.mention}?\n"
            "This cannot be undone.",
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await view.wait()
        if view.choice is None:
            return
        if not view.choice:
            await view.finish("Cancelled — nothing was deleted.")
            return

        try:
            deleted = await ctx.channel.purge(
                limit=message_count,
                reason=f"/delete by {ctx.author} ({ctx.author.id})",
            )
        except discord.Forbidden:
            await view.finish(
                "I need the **Manage Messages** and **Read Message History** "
                f"permissions in {ctx.channel.mention}."
            )
            return
        except discord.HTTPException:
            _log.exception("Purge failed in #%s (id=%s)", ctx.channel, ctx.channel.id)
            await view.finish(
                "Deleting failed. Discord refuses to bulk delete messages older than 14 days."
            )
            return

        _log.info("%s deleted %d message(s) in #%s", ctx.author, len(deleted), ctx.channel)
        await view.finish(_summarize(deleted))

    @discord.message_command(
        name="Delete up to here",
        default_member_permissions=_OWNER_ONLY,
    )
    @commands.is_owner()
    async def delete_to_here(
        self,
        ctx: discord.ApplicationContext,
        message: discord.Message,
    ) -> None:
        """Purge the picked message and everything posted after it.

        Right-clicking the message where a mess started beats counting the
        messages by eye for ``/delete``.
        """
        if ctx.guild is None:
            await ctx.respond("Messages can only be bulk deleted in a server.", ephemeral=True)
            return

        # Counting first needs a round trip, which can outrun the 3 second
        # window. Everything below therefore edits this deferred response,
        # rather than sending followups that ConfirmView could not reach.
        await ctx.defer(ephemeral=True)

        # `after` is exclusive, so step one id back to include the pick itself.
        boundary = discord.Object(id=message.id - 1)

        try:
            doomed = await ctx.channel.history(
                after=boundary,
                limit=_MAX_CONTEXT_DELETE + 1,
                oldest_first=True,
            ).flatten()
        except discord.Forbidden:
            await ctx.interaction.edit_original_response(
                content="I need **Read Message History** in this channel."
            )
            return

        if not doomed:
            await ctx.interaction.edit_original_response(content="Nothing to delete.")
            return

        # Knowing the count before an irreversible bulk delete is the point of
        # confirming at all, so refuse rather than silently truncating.
        if len(doomed) > _MAX_CONTEXT_DELETE:
            await ctx.interaction.edit_original_response(
                content=(
                    f"That would delete more than {_MAX_CONTEXT_DELETE} messages. "
                    "Pick a more recent message."
                )
            )
            return

        view = ConfirmView(
            ctx.interaction,
            user_id=ctx.author.id,
            confirm_label="Delete",
            timeout_message="Timed out — nothing was deleted.",
        )
        await ctx.interaction.edit_original_response(
            content=(
                f"Delete **{len(doomed)}** message(s) — {message.jump_url} "
                "and everything after it?\nThis cannot be undone."
            ),
            view=view,
        )

        await view.wait()
        if view.choice is None:
            return
        if not view.choice:
            await view.finish("Cancelled — nothing was deleted.")
            return

        try:
            deleted = await ctx.channel.purge(
                limit=len(doomed),
                after=boundary,
                oldest_first=True,
                reason=f"Delete up to here by {ctx.author} ({ctx.author.id})",
            )
        except discord.Forbidden:
            await view.finish(
                "I need the **Manage Messages** and **Read Message History** "
                f"permissions in {ctx.channel.mention}."
            )
            return
        except discord.HTTPException:
            _log.exception("Purge failed in #%s (id=%s)", ctx.channel, ctx.channel.id)
            await view.finish(
                "Deleting failed. Discord refuses to bulk delete messages older than 14 days."
            )
            return

        _log.info(
            "%s deleted %d message(s) up to %s in #%s",
            ctx.author,
            len(deleted),
            message.id,
            ctx.channel,
        )
        await view.finish(_summarize(deleted))


async def _deliver(
    interaction: discord.Interaction,
    channel: discord.abc.GuildChannel,
    content: str,
) -> None:
    """Post the message and report the outcome back to the submitter."""
    # Acknowledge the submit before touching the network, so a slow send
    # cannot blow Discord's 3 second response window.
    await interaction.response.defer(ephemeral=True)

    try:
        sent = await channel.send(content)
    except discord.Forbidden:
        await interaction.followup.send(
            f"I am not allowed to send messages in {channel.mention}.", ephemeral=True
        )
        return
    except discord.HTTPException:
        _log.exception("Failed to send to #%s (id=%s)", channel, channel.id)
        await interaction.followup.send(
            f"Discord rejected the message to {channel.mention}.", ephemeral=True
        )
        return

    _log.info("%s sent a message to #%s via /send", interaction.user, channel)
    await interaction.followup.send(
        f"Sent to {channel.mention}: {sent.jump_url}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


def _summarize(deleted: list[discord.Message]) -> str:
    """Describe a purge as a per-author tally.

    Message *content* is deliberately not echoed back: reading it needs the
    privileged ``message_content`` intent, which this bot does not request, so
    every ``Message.content`` here would be an empty string.
    """
    if not deleted:
        return "Nothing to delete — the channel had no recent messages."

    tally = Counter(message.author for message in deleted)
    lines = [f"Deleted **{len(deleted)}** message(s):"]
    lines.extend(
        f"- {author.mention} — {count}" for author, count in tally.most_common(_MAX_SUMMARY_AUTHORS)
    )

    remaining = len(tally) - _MAX_SUMMARY_AUTHORS
    if remaining > 0:
        lines.append(f"- …and {remaining} other author(s)")

    return "\n".join(lines)


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    bot.add_cog(Owner(bot))

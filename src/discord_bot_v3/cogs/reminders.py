"""Reminders: right-click a message, get pinged about it later.

Two message context menus rather than slash commands, because a reminder is
always *about* a specific message — right-clicking it is the natural gesture,
and it hands us the jump URL for free.

Loads only when MySQL is configured; see ``setup`` at the bottom.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
See the note in ``general.py``.
"""

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from ..services.reminders import MAX_HORIZON, Reminder, ReminderStore

_log = logging.getLogger(__name__)

# How often to look for due reminders. V2 polled every second; this is one
# indexed query, and 5s is well inside what anyone notices.
_TICK_SECONDS = 5

# Rows to fire per tick, so a backlog after downtime cannot stall the loop.
_BATCH = 25

# Discord's cap on select menu options, which bounds the cancel list.
_MAX_LISTED = 25

_DATE_FORMAT = "%Y-%m-%d"
_TIME_FORMATS = ("%H:%M:%S", "%H:%M")


class DurationModal(discord.ui.Modal):
    """Ask for an offset from now."""

    def __init__(self, cog: Reminders, message: discord.Message) -> None:
        super().__init__(title="Remind me in...")
        self.cog = cog
        self.message = message

        self.days = _number_field("Days", "0")
        self.hours = _number_field("Hours", "0")
        self.minutes = _number_field("Minutes", "30")
        self.note = _note_field()
        for item in (self.days, self.hours, self.minutes, self.note):
            self.add_item(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            days = _as_int(self.days.value, "Days")
            hours = _as_int(self.hours.value, "Hours")
            minutes = _as_int(self.minutes.value, "Minutes")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        delta = dt.timedelta(days=days, hours=hours, minutes=minutes)
        if delta <= dt.timedelta(0):
            await interaction.response.send_message(
                "That is not a duration — give me at least a minute.", ephemeral=True
            )
            return
        if delta > MAX_HORIZON:
            await interaction.response.send_message(
                f"That is further out than {MAX_HORIZON.days} days.", ephemeral=True
            )
            return

        await self.cog.schedule(
            interaction, self.message, dt.datetime.now(dt.UTC) + delta, self.note.value or ""
        )


class TimeModal(discord.ui.Modal):
    """Ask for a wall-clock date and time in the configured zone."""

    def __init__(self, cog: Reminders, message: discord.Message) -> None:
        super().__init__(title="Remind me at...")
        self.cog = cog
        self.message = message

        local_now = dt.datetime.now(cog.zone)
        self.date = discord.ui.InputText(
            label="Date",
            placeholder=_DATE_FORMAT.replace("%Y", "YYYY").replace("%m", "MM").replace("%d", "DD"),
            value=local_now.strftime(_DATE_FORMAT),
            max_length=10,
        )
        self.time = discord.ui.InputText(
            label=f"Time ({cog.zone.key})",
            placeholder="HH:MM or HH:MM:SS",
            value=local_now.strftime("%H:%M"),
            max_length=8,
        )
        self.note = _note_field()
        for item in (self.date, self.time, self.note):
            self.add_item(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            when = _parse_local(self.date.value, self.time.value, self.cog.zone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        now = dt.datetime.now(dt.UTC)
        if when <= now:
            await interaction.response.send_message(
                f"{discord.utils.format_dt(when, 'F')} is in the past.", ephemeral=True
            )
            return
        if when - now > MAX_HORIZON:
            await interaction.response.send_message(
                f"That is further out than {MAX_HORIZON.days} days.", ephemeral=True
            )
            return

        await self.cog.schedule(interaction, self.message, when, self.note.value or "")


class CancelView(discord.ui.View):
    """Pick pending reminders to cancel.

    Built fresh on each ``/reminders`` call rather than persisted, so it keeps
    working across restarts without needing a persistent view.
    """

    def __init__(self, cog: Reminders, reminders: list[Reminder], *, user_id: int) -> None:
        super().__init__(timeout=120.0)
        self.cog = cog
        self._user_id = user_id

        select = discord.ui.Select(
            placeholder="Cancel a reminder...",
            min_values=1,
            max_values=len(reminders),
            options=[
                discord.SelectOption(
                    label=_summarise(r)[:100],
                    description=(r.note or r.message_url)[:100],
                    value=str(r.id),
                )
                for r in reminders
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id != self._user_id:
            await interaction.response.send_message("Those are not yours.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        chosen = [int(v) for v in interaction.data.get("values", [])]
        removed = 0
        for reminder_id in chosen:
            if await self.cog.store.delete(reminder_id, user_id=self._user_id):
                removed += 1

        self.disable_all_items()
        await interaction.response.edit_message(
            content=f"Cancelled **{removed}** reminder(s).", embed=None, view=None
        )
        self.stop()


class Reminders(commands.Cog):
    """Message context menus, the scheduler, and /reminders."""

    def __init__(self, bot: discord.Bot, store: ReminderStore) -> None:
        self.bot = bot
        self.store = store
        self.zone = ZoneInfo(bot.config.timezone)
        self._ready = False
        self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    @tasks.loop(seconds=_TICK_SECONDS)
    async def tick(self) -> None:
        """Fire every reminder that has come due."""
        if not self._ready:
            return

        try:
            due = await self.store.due(now=dt.datetime.now(dt.UTC), limit=_BATCH)
        except Exception:
            _log.exception("Could not read due reminders")
            return

        for reminder in due:
            try:
                await self._fire(reminder)
            except Exception:
                _log.exception("Could not deliver reminder %s", reminder.id)
            finally:
                # Delete either way: a reminder that cannot be delivered must
                # not be retried forever on every tick.
                await self.store.delete(reminder.id)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.store.setup()
        except Exception:
            _log.exception("Could not create the reminders table; reminders are disabled")
            return
        self._ready = True

    async def _fire(self, reminder: Reminder) -> None:
        """Deliver one reminder into the channel it was set in."""
        channel = self.bot.get_channel(reminder.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(reminder.channel_id)

        content = f"<@{reminder.user_id}> here is your reminder."
        if reminder.note:
            content += f"\n> {discord.utils.escape_markdown(reminder.note)}"
        content += f"\n{reminder.message_url}"

        # Reply to the confirmation when it is still around, so the reminder
        # lands in context rather than as a loose message.
        reference = None
        if reminder.response_id is not None:
            reference = discord.MessageReference(
                message_id=reminder.response_id,
                channel_id=reminder.channel_id,
                guild_id=reminder.guild_id,
                fail_if_not_exists=False,
            )

        await channel.send(
            content,
            reference=reference,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    async def schedule(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        when: dt.datetime,
        note: str,
    ) -> None:
        """Persist a reminder and confirm it publicly."""
        if not self._ready:
            await interaction.response.send_message(
                "Reminders are not available right now.", ephemeral=True
            )
            return

        try:
            reminder_id = await self.store.add(
                user_id=interaction.user.id,
                channel_id=interaction.channel_id,
                guild_id=interaction.guild_id,
                message_url=message.jump_url,
                note=note,
                remind_at=when,
            )
        except Exception:
            _log.exception("Could not store a reminder")
            await interaction.response.send_message(
                "I could not save that reminder.", ephemeral=True
            )
            return

        # Public, not ephemeral: the reminder replies to this message later,
        # and Discord's timestamp renders in each reader's own timezone.
        await interaction.response.send_message(
            f"Reminder set for {discord.utils.format_dt(when, 'F')} "
            f"({discord.utils.format_dt(when, 'R')}).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        try:
            confirmation = await interaction.original_response()
            await self.store.set_response_id(reminder_id, confirmation.id)
        except discord.HTTPException, Exception:
            # Only costs the reply-in-context; the reminder still fires.
            _log.debug("Could not record the confirmation message", exc_info=True)

    @discord.message_command(name="Remind me in...")
    async def remind_in(self, ctx: discord.ApplicationContext, message: discord.Message) -> None:
        """Offset-based reminder for the right-clicked message."""
        await ctx.send_modal(DurationModal(self, message))

    @discord.message_command(name="Remind me at...")
    async def remind_at(self, ctx: discord.ApplicationContext, message: discord.Message) -> None:
        """Wall-clock reminder for the right-clicked message."""
        await ctx.send_modal(TimeModal(self, message))

    @discord.slash_command(name="reminders", description="List and cancel your reminders.")
    async def reminders(self, ctx: discord.ApplicationContext) -> None:
        """Show the caller's pending reminders, with a way to cancel them."""
        await ctx.defer(ephemeral=True)

        try:
            pending = await self.store.for_user(ctx.author.id, limit=_MAX_LISTED)
        except Exception:
            _log.exception("Could not list reminders")
            await ctx.respond("I could not read your reminders.", ephemeral=True)
            return

        if not pending:
            await ctx.respond("You have no reminders set.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Your reminders",
            description="\n".join(
                f"{discord.utils.format_dt(r.remind_at_utc, 'R')} — [message]({r.message_url})"
                + (f"\n> {discord.utils.escape_markdown(r.note)}" if r.note else "")
                for r in pending
            )[:4096],
            colour=discord.Colour.blurple(),
        )
        await ctx.respond(
            embed=embed,
            view=CancelView(self, pending, user_id=ctx.author.id),
            ephemeral=True,
        )


def _number_field(label: str, default: str) -> discord.ui.InputText:
    return discord.ui.InputText(
        label=label, value=default, placeholder="0", max_length=4, required=False
    )


def _note_field() -> discord.ui.InputText:
    return discord.ui.InputText(
        label="Note",
        style=discord.InputTextStyle.long,
        placeholder="What is this about? (optional)",
        max_length=255,
        required=False,
    )


def _as_int(raw: str | None, label: str) -> int:
    """Parse a modal number field, treating blank as zero."""
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"**{label}** must be a whole number, not {text!r}.") from None
    if value < 0:
        raise ValueError(f"**{label}** cannot be negative.")
    return value


def _parse_local(date_text: str | None, time_text: str | None, zone: ZoneInfo) -> dt.datetime:
    """Read a date and time as local wall clock, returning aware UTC."""
    date_raw = (date_text or "").strip()
    time_raw = (time_text or "").strip()

    try:
        date_part = dt.datetime.strptime(date_raw, _DATE_FORMAT).date()
    except ValueError:
        raise ValueError(f"**Date** must look like YYYY-MM-DD, not {date_raw!r}.") from None

    for fmt in _TIME_FORMATS:
        try:
            time_part = dt.datetime.strptime(time_raw, fmt).time()
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"**Time** must look like HH:MM, not {time_raw!r}.") from None

    return dt.datetime.combine(date_part, time_part, tzinfo=zone).astimezone(dt.UTC)


def _summarise(reminder: Reminder) -> str:
    """A one-line label for the cancel menu."""
    stamp = reminder.remind_at_utc.strftime("%Y-%m-%d %H:%M UTC")
    return f"{stamp} — {reminder.note}" if reminder.note else stamp


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    if bot.db is None:
        _log.warning("MySQL is not configured — reminders will not be registered.")
        return

    bot.add_cog(Reminders(bot, ReminderStore(bot.db)))

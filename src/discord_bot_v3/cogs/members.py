"""Member records, self-service birthdays, and the daily announcer.

This cog deliberately does not use ``from __future__ import annotations``:
Pycord reads ``discord.Option`` objects from command signatures at runtime.
"""

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from ..services.members import BirthdayMember, MemberStore

_log = logging.getLogger(__name__)
_EARLIEST_BIRTHDAY = dt.date(1900, 1, 1)
# Keep this as KLIPY's share URL rather than copying an asset into the repo.
# Discord unfurls the page's animated media in the birthday channel.
_BIRTHDAY_GIF_URL = "https://klipy.com/gifs/happy-birthday-chicken"


class Members(commands.Cog):
    """Keep a per-guild member directory and announce opted-in birthdays."""

    birthday = discord.SlashCommandGroup("birthday", "Manage your birthday.")

    def __init__(self, bot: discord.Bot, store: MemberStore) -> None:
        self.bot = bot
        self.store = store
        self.zone = ZoneInfo(bot.config.timezone)
        self._ready = False
        self.announce_birthdays.change_interval(time=dt.time(hour=0, tzinfo=self.zone))
        self.announce_birthdays.start()

    def cog_unload(self) -> None:
        self.announce_birthdays.cancel()

    @tasks.loop(hours=24)
    async def announce_birthdays(self) -> None:
        """Post today's birthdays at midnight in the configured timezone."""
        if not self._ready or self.bot.config.birthday_channel_id is None:
            return

        channel_id = self.bot.config.birthday_channel_id
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            guild = getattr(channel, "guild", None)
            if guild is None or not hasattr(channel, "send"):
                _log.error("BIRTHDAY_CHANNEL_ID %s is not a guild text channel", channel_id)
                return

            today = dt.datetime.now(self.zone).date()
            birthdays = await self.store.birthdays_on(
                guild_id=guild.id, month_days=_birthday_keys(today)
            )
            for member in birthdays:
                # The row outlives the membership: birthdays are kept so that
                # someone who rejoins does not have to set theirs again. Which
                # means the table is not a guest list, and announcing straight
                # from it would ping people who left months ago.
                if guild.get_member(member.user_id) is None:
                    continue
                await channel.send(
                    f"Happy birthday, <@{member.user_id}>! 🎉",
                    embed=_birthday_embed(member),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                # Keep the GIF in a separate message: Discord can unfurl it
                # without competing with the birthday greeting's embed.
                await channel.send(
                    _BIRTHDAY_GIF_URL, allowed_mentions=discord.AllowedMentions.none()
                )
        except Exception:
            _log.exception("Could not announce today's birthdays")

    @announce_birthdays.before_loop
    async def _before_announce_birthdays(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.store.setup()
            await self._sync_members()
        except Exception:
            _log.exception("Could not initialise member storage; birthdays are disabled")
            return
        self._ready = True

    async def _sync_members(self) -> None:
        """Populate existing members once; new joins are handled by the listener."""
        for guild in self.bot.guilds:
            try:
                async for member in guild.fetch_members(limit=None):
                    if not member.bot:
                        await self._upsert_member(member)
            except discord.HTTPException:
                _log.exception("Could not synchronise members for %s", guild)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Add a new member without discarding any existing birthday record."""
        if not self._ready or member.bot:
            return
        try:
            await self._upsert_member(member)
        except Exception:
            _log.exception("Could not record new member %s", member.id)

    async def _upsert_member(self, member: discord.Member) -> None:
        await self.store.upsert(
            guild_id=member.guild.id,
            user_id=member.id,
            username=member.name,
            display_name=member.display_name,
        )

    @birthday.command(name="set", description="Save your birthday for this server.")
    async def birthday_set(
        self,
        ctx: discord.ApplicationContext,
        date: discord.Option(str, description="YYYY-MM-DD", min_length=10, max_length=10),
    ) -> None:
        """Save the invoking member's date of birth privately."""
        birthday = _parse_birthday(date)
        if birthday is None:
            await ctx.respond("Use a real past date in `YYYY-MM-DD` format.", ephemeral=True)
            return
        if not self._ready or ctx.guild is None:
            await ctx.respond("Birthdays are only available in a server right now.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        try:
            await self._upsert_member(ctx.author)
            await self.store.set_birthday(
                guild_id=ctx.guild.id, user_id=ctx.author.id, birthday=birthday
            )
        except Exception:
            _log.exception("Could not save birthday for %s", ctx.author.id)
            await ctx.respond("I could not save your birthday.", ephemeral=True)
            return

        await ctx.respond(
            f"Saved your birthday as **{birthday.strftime('%B')} {birthday.day}**. "
            "Only the day and month are used for announcements.",
            ephemeral=True,
        )

    @birthday.command(name="remove", description="Remove your saved birthday from this server.")
    async def birthday_remove(self, ctx: discord.ApplicationContext) -> None:
        """Let a member revoke their birthday without needing an administrator."""
        if not self._ready or ctx.guild is None:
            await ctx.respond("Birthdays are only available in a server right now.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        try:
            removed = await self.store.clear_birthday(guild_id=ctx.guild.id, user_id=ctx.author.id)
        except Exception:
            _log.exception("Could not remove birthday for %s", ctx.author.id)
            await ctx.respond("I could not remove your birthday.", ephemeral=True)
            return
        await ctx.respond(
            "Your birthday has been removed." if removed else "You do not have a birthday saved.",
            ephemeral=True,
        )


def _parse_birthday(raw: str) -> dt.date | None:
    """Accept an ISO date that is plausible and in the past."""
    try:
        birthday = dt.date.fromisoformat(raw.strip())
    except ValueError:
        return None
    if not _EARLIEST_BIRTHDAY <= birthday < dt.date.today():
        return None
    return birthday


def _birthday_keys(today: dt.date) -> tuple[str, ...]:
    """Return the indexed month-day keys to celebrate on this date."""
    keys = (today.strftime("%m-%d"),)
    if today.month == 2 and today.day == 28 and not _is_leap_year(today.year):
        return (*keys, "02-29")
    return keys


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _birthday_embed(member: BirthdayMember) -> discord.Embed:
    """Create a public greeting without exposing the member's age or birth year."""
    return discord.Embed(
        title="Happy birthday!",
        description=(
            f"Wishing **{discord.utils.escape_markdown(member.display_name)}** a wonderful day!"
        ),
        colour=discord.Colour.magenta(),
    )


def setup(bot: discord.Bot) -> None:
    """Load only when durable storage is configured."""
    if bot.db is None:
        _log.warning("Members and birthdays disabled: MYSQL_USER is not configured")
        return
    bot.add_cog(Members(bot, MemberStore(bot.db)))

"""Member records and the daily birthday announcer.

This cog owns **no commands**. It keeps the ``members`` table in step with the
server — a row per person, synchronised at startup and on join — and posts
birthdays at midnight.

Both of the columns a human curates, ``birthday`` and ``description``, are set
by hand in SQL. There is deliberately no ``/birthday set``: for one small
server, a row edit is less work than a command, an autocomplete and a
validation path. The cost is that only someone with database access can change
them, which for these two is the point.
"""

import datetime as dt
import logging
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from ..services.members import BirthdayMember, MemberStore

_log = logging.getLogger(__name__)
# Keep this as KLIPY's share URL rather than copying an asset into the repo.
# Discord unfurls the page's animated media in the birthday channel.
_BIRTHDAY_GIF_URL = "https://klipy.com/gifs/happy-birthday-chicken"


class Members(commands.Cog):
    """Keep a per-guild member directory and announce opted-in birthdays."""

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

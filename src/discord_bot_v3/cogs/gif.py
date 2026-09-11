"""GIF search, backed by KLIPY.

This cog only registers when ``KLIPY_API_KEY`` is set; see ``setup`` below.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
See the note in ``general.py``.
"""

import contextlib
import logging
import random

import discord
from discord.ext import commands

from ..services.klipy import Gif, KlipyClient, KlipyError

_log = logging.getLogger(__name__)

# The bot's mascot, and V2's default when /gif was called bare.
DEFAULT_KEYWORD = "chicken"

# How many results to pull before picking one at random. Enough variety that
# repeat calls rarely collide, without paging through the whole catalogue.
_SEARCH_SIZE = 24

# KLIPY filters server-side; age-restricted channels opt into a looser setting.
_FILTER_DEFAULT = "high"
_FILTER_NSFW = "low"

# Long enough to flip through a few GIFs, short enough not to leave a live view
# lying around. Well inside the 15 minute interaction token lifetime.
_PICKER_TIMEOUT = 120.0


class GifPicker(discord.ui.View):
    """Preview one GIF privately, shuffle through the rest, then post it.

    The whole result page is already in memory, so shuffling costs no further
    API calls. Results are shuffled once and then walked in order rather than
    re-rolled each time, which guarantees no repeat until every GIF has been
    seen.
    """

    def __init__(
        self,
        origin: discord.Interaction,
        *,
        gifs: list[Gif],
        keyword: str,
        user_id: int,
        timeout: float = _PICKER_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)

        self._origin = origin
        self._user_id = user_id
        self._keyword = keyword
        self._gifs = list(gifs)
        random.shuffle(self._gifs)
        self._index = 0

        self._shuffle = discord.ui.Button(
            label="Shuffle",
            style=discord.ButtonStyle.secondary,
            emoji="🔀",
            # Nothing to shuffle between when the search found a single GIF.
            disabled=len(self._gifs) < 2,
        )
        self._shuffle.callback = self._on_shuffle
        self.add_item(self._shuffle)

        post = discord.ui.Button(label="Post", style=discord.ButtonStyle.success)
        post.callback = self._on_post
        self.add_item(post)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    @property
    def current(self) -> Gif:
        """The GIF currently being previewed."""
        return self._gifs[self._index]

    def embed(self) -> discord.Embed:
        """Render the preview for the current GIF."""
        gif = self.current
        embed = discord.Embed(
            title=gif.title or self._keyword,
            url=gif.url,
            colour=discord.Colour.blurple(),
        )
        embed.set_image(url=gif.url)
        embed.set_footer(text=f"KLIPY • {self._index + 1}/{len(self._gifs)}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Refuse clicks from anyone but the user who ran the command."""
        if interaction.user is not None and interaction.user.id != self._user_id:
            await interaction.response.send_message("That is not your GIF.", ephemeral=True)
            return False
        return True

    async def _on_shuffle(self, interaction: discord.Interaction) -> None:
        self._index = (self._index + 1) % len(self._gifs)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _on_post(self, interaction: discord.Interaction) -> None:
        gif = self.current
        self.disable_all_items()

        try:
            await interaction.channel.send(gif.url)
        except discord.Forbidden, discord.HTTPException:
            _log.exception("Could not post a GIF to #%s", interaction.channel)
            await interaction.response.edit_message(
                content="I could not post that here.", embed=None, view=None
            )
            self.stop()
            return

        # The GIF itself is now in the channel, so the private preview has
        # nothing left to say: drop it rather than leaving a receipt behind.
        # An empty edit is not allowed, hence acknowledge-then-delete.
        await interaction.response.defer()
        with contextlib.suppress(discord.HTTPException):
            await interaction.delete_original_response()
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.disable_all_items()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()

    async def on_timeout(self) -> None:
        """Drop the preview rather than leaving dead buttons behind."""
        with contextlib.suppress(discord.HTTPException):
            await self._origin.edit_original_response(
                content="Timed out — nothing was posted.", embed=None, view=None
            )


class Gifs(commands.Cog):
    """Fetches GIFs from KLIPY."""

    def __init__(self, bot: discord.Bot, klipy: KlipyClient) -> None:
        self.bot = bot
        self.klipy = klipy

    def cog_unload(self) -> None:
        """Release the HTTP session.

        ``cog_unload`` is synchronous in Pycord, so the close is scheduled
        rather than awaited.
        """
        self.bot.loop.create_task(self.klipy.close())

    @discord.slash_command(name="gif", description="Find a GIF on KLIPY and post it.")
    async def gif(
        self,
        ctx: discord.ApplicationContext,
        keyword: discord.Option(
            str,
            description=f"What to search for. Defaults to {DEFAULT_KEYWORD}.",
            default=DEFAULT_KEYWORD,
            max_length=100,
        ),
    ) -> None:
        """Search KLIPY, preview one privately, and post it once chosen."""
        # A round trip to KLIPY can outrun Discord's 3 second ack window. The
        # preview is private, so everything below edits this deferred response.
        await ctx.defer(ephemeral=True)

        try:
            results = await self.klipy.search(
                keyword,
                per_page=_SEARCH_SIZE,
                content_filter=self._content_filter(ctx.channel),
            )
        except KlipyError as exc:
            _log.warning("KLIPY search for %r failed: %s", keyword, exc)
            await ctx.interaction.edit_original_response(
                content="KLIPY is not answering right now. Try again shortly."
            )
            return

        if not results:
            await ctx.interaction.edit_original_response(
                content=f"No GIFs found for **{discord.utils.escape_markdown(keyword)}**."
            )
            return

        view = GifPicker(
            ctx.interaction,
            gifs=results,
            keyword=keyword,
            user_id=ctx.author.id,
        )
        await ctx.interaction.edit_original_response(embed=view.embed(), view=view)

    @staticmethod
    def _content_filter(channel: discord.abc.GuildChannel | discord.DMChannel) -> str:
        """Strictest filtering unless the channel is explicitly age-restricted."""
        is_nsfw = getattr(channel, "is_nsfw", None)
        if callable(is_nsfw) and is_nsfw():
            return _FILTER_NSFW
        return _FILTER_DEFAULT


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    api_key = bot.config.klipy_api_key
    if not api_key:
        _log.warning("KLIPY_API_KEY is not set — /gif will not be registered.")
        return

    bot.add_cog(Gifs(bot, KlipyClient(api_key)))

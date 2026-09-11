"""Pokémon TCG Pocket card browsing, backed by TCGdex.

The set list is cached in memory at startup and refreshed daily, because
autocomplete has to answer inside Discord's 3 second window.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
See the note in ``general.py``.
"""

import logging

import discord
from discord.ext import commands, pages, tasks

from ..services.tcgdex import CardStub, PokemonCard, PokemonSet, TcgdexClient, TcgdexError

_log = logging.getLogger(__name__)

# Cards per page when listing a set. One embed per card would mean 300+ pages
# for the larger sets; a compact list is far quicker to scan, and
# `/pokemon cards get` is there for the artwork.
_CARDS_PER_PAGE = 20

_PAGINATOR_TIMEOUT = 300.0

# Cap on name-search results, so a one-letter query does not page forever.
_SEARCH_LIMIT = 100

# Discord shows at most 25 autocomplete choices.
_MAX_CHOICES = 25

# New sets appear every few weeks, so a daily rebuild is ample.
_REFRESH_HOURS = 24

# `/pokemon update` refetches every set; keep it from being spammed.
_UPDATE_COOLDOWN = 300.0

# TCG Pocket rarities, as the game itself draws them. Anything unrecognised
# falls through to its plain name.
_RARITY_SYMBOLS = {
    "One Diamond": "◆",
    "Two Diamond": "◆◆",
    "Three Diamond": "◆◆◆",
    "Four Diamond": "◆◆◆◆",
    "One Star": "★",
    "Two Star": "★★",
    "Three Star": "★★★",
    "One Shiny": "✦",
    "Two Shiny": "✦✦",
    "Crown": "♛",
    "Crown Rare": "♛",
}

# Energy colours, so a card embed reads as its type at a glance.
_TYPE_COLOURS = {
    "Grass": 0x7DB808,
    "Fire": 0xE8442D,
    "Water": 0x1BA7DB,
    "Lightning": 0xF3D22B,
    "Psychic": 0x9B5BA5,
    "Fighting": 0xC6612F,
    "Darkness": 0x2C4A5A,
    "Metal": 0x8A9BA8,
    "Dragon": 0xC9A227,
    "Colorless": 0xD5D5D5,
    "Fairy": 0xE86FA8,
}


class CardPicker(discord.ui.View):
    """A select menu for opening a card straight from a listing.

    Merged into the paginator alongside its navigation buttons, so browsing a
    set and viewing a card is one flow rather than copying an id into another
    command.
    """

    def __init__(self, cog: Pokemon, stubs: list[CardStub]) -> None:
        # The paginator owns the lifetime and disables everything on timeout.
        super().__init__(timeout=None)
        self.cog = cog

        select = discord.ui.Select(
            placeholder="View a card...",
            min_values=1,
            max_values=1,
            # The paginator's navigation sits on row 0 and a select is full
            # width, so leaving this to auto-placement overflows the row.
            row=1,
            options=[
                discord.SelectOption(
                    label=f"{stub.local_id} · {stub.name}"[:100],
                    description=stub.set_name[:100] or None,
                    value=stub.id,
                )
                for stub in stubs[:_MAX_CHOICES]
            ],
        )
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        card_id = (interaction.data or {}).get("values", [None])[0]
        if not card_id:
            return

        await interaction.response.defer()
        try:
            card = await self.cog.tcgdex.get_card(card_id)
        except TcgdexError:
            _log.warning("Could not fetch %s from a picker", card_id)
            await interaction.followup.send("TCGdex is not answering.", ephemeral=True)
            return

        if card is None:
            await interaction.followup.send(f"`{card_id}` is gone.", ephemeral=True)
            return

        await interaction.followup.send(embed=_card_embed(card))


class CardPaginator(pages.Paginator):
    """A paginator that swaps a page's view instead of stacking them.

    Pycord's ``update_custom_view`` only removes items belonging to the
    *paginator-level* ``custom_view``; a view attached to a :class:`Page` is
    never taken off again. With one select per page that means the second page
    flip raises ``item would not fit at row 1``. Remembering whichever view was
    applied last lets the base class clear it properly on the next flip.
    """

    def update_custom_view(self, custom_view: discord.ui.View) -> None:
        super().update_custom_view(custom_view)
        self.custom_view = custom_view


class Pokemon(commands.Cog):
    """/pokemon — browse the TCG Pocket card pool."""

    pokemon = discord.SlashCommandGroup("pokemon", "Pokémon TCG Pocket.")
    sets = pokemon.create_subgroup("sets", "Browse TCG Pocket sets.")
    cards = pokemon.create_subgroup("cards", "Browse TCG Pocket cards.")

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.tcgdex = TcgdexClient(bot.cache)
        self._first_pass = True
        self.refresh_cache.start()

    def cog_unload(self) -> None:
        """Stop the refresh loop and release the session."""
        self.refresh_cache.cancel()
        self.bot.loop.create_task(self.tcgdex.close())

    @tasks.loop(hours=_REFRESH_HOURS)
    async def refresh_cache(self) -> None:
        """Keep the set cache fresh, daily.

        On the first pass Redis is tried before the API, so a restart has
        working autocomplete immediately instead of after 16 requests.
        """
        try:
            if self._first_pass and await self.tcgdex.load_cached():
                return
            await self.tcgdex.refresh()
        except TcgdexError:
            _log.exception("Could not refresh the TCG Pocket cache")
        finally:
            self._first_pass = False

    @refresh_cache.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    async def set_autocomplete(
        self, ctx: discord.AutocompleteContext
    ) -> list[discord.OptionChoice]:
        """Suggest cached sets. Empty until the cache has loaded."""
        typed = (ctx.value or "").casefold()
        return [
            discord.OptionChoice(name=f"{s.name} ({s.id})", value=s.id)
            for s in self.tcgdex.sets
            if typed in s.name.casefold() or typed in s.id.casefold()
        ][:_MAX_CHOICES]

    async def card_autocomplete(
        self, ctx: discord.AutocompleteContext
    ) -> list[discord.OptionChoice]:
        """Suggest cards from whichever set is selected in the same command."""
        found = self.tcgdex.get_set(str((ctx.options or {}).get("set") or ""))
        if found is None:
            return []

        typed = (ctx.value or "").casefold()
        return [
            discord.OptionChoice(name=f"{c.local_id} · {c.name}"[:100], value=c.id)
            for c in found.cards
            if typed in c.name.casefold() or typed in c.local_id.casefold()
        ][:_MAX_CHOICES]

    @pokemon.command(name="update", description="Refresh the cached TCG Pocket data.")
    @commands.cooldown(1, _UPDATE_COOLDOWN, commands.BucketType.default)
    async def update(self, ctx: discord.ApplicationContext) -> None:
        """Rebuild the cache on demand, between the daily refreshes."""
        await ctx.defer()
        try:
            count = await self.tcgdex.refresh()
        except TcgdexError as exc:
            _log.warning("Manual TCGdex refresh failed: %s", exc)
            await ctx.respond("TCGdex is not answering right now. Try again shortly.")
            return

        await ctx.respond(f"Refreshed **{count}** sets and **{self.tcgdex.card_count}** cards.")

    @sets.command(name="list", description="List every TCG Pocket set.")
    async def sets_list(self, ctx: discord.ApplicationContext) -> None:
        """Page through the sets, one embed each."""
        await ctx.defer()
        if not await self._ready(ctx):
            return

        known = self.tcgdex.sets
        embeds = [_set_embed(s, index=i, total=len(known)) for i, s in enumerate(known, start=1)]
        await _paginate(ctx, embeds)

    @sets.command(name="get", description="Show one TCG Pocket set.")
    async def sets_get(
        self,
        ctx: discord.ApplicationContext,
        set: discord.Option(  # the option name users see; shadows the builtin
            str,
            description="Which set.",
            autocomplete=set_autocomplete,
        ),
    ) -> None:
        """Show a single set's details."""
        await ctx.defer()
        if not await self._ready(ctx):
            return

        found = self.tcgdex.get_set(set)
        if found is None:
            await ctx.respond(f"No set called `{set}`.")
            return

        await ctx.respond(embed=_set_embed(found))

    @cards.command(name="list", description="List the cards in a TCG Pocket set.")
    async def cards_list(
        self,
        ctx: discord.ApplicationContext,
        set: discord.Option(
            str,
            description="Which set.",
            autocomplete=set_autocomplete,
        ),
    ) -> None:
        """Page through a set's card list."""
        await ctx.defer()
        if not await self._ready(ctx):
            return

        found = self.tcgdex.get_set(set)
        if found is None:
            await ctx.respond(f"No set called `{set}`.")
            return
        if not found.cards:
            await ctx.respond(f"**{found.name}** has no cards listed yet.")
            return

        chunks = _chunk(found.cards)
        await _paginate(
            ctx,
            _card_list_embeds(found, chunks),
            pickers=[CardPicker(self, chunk) for chunk in chunks],
        )

    @cards.command(name="get", description="Show one card from a TCG Pocket set.")
    async def cards_get(
        self,
        ctx: discord.ApplicationContext,
        set: discord.Option(
            str,
            description="Which set.",
            autocomplete=set_autocomplete,
        ),
        card: discord.Option(
            str,
            description="Which card.",
            autocomplete=card_autocomplete,
        ),
    ) -> None:
        """Show one card, picked from a set."""
        await ctx.defer()
        if not await self._ready(ctx):
            return
        await self._respond_with_card(ctx, card)

    @cards.command(name="search", description="Find cards by name across every set.")
    async def cards_search(
        self,
        ctx: discord.ApplicationContext,
        name: discord.Option(
            str,
            description="Part of a card name, e.g. pikachu.",
            max_length=64,
        ),
    ) -> None:
        """Search every cached set by name."""
        await ctx.defer()
        if not await self._ready(ctx):
            return

        hits = self.tcgdex.search(name, limit=_SEARCH_LIMIT)
        if not hits:
            await ctx.respond(
                f"No cards matching **{discord.utils.escape_markdown(name)}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if len(hits) == 1:
            await self._respond_with_card(ctx, hits[0].id)
            return

        chunks = _chunk(hits)
        await _paginate(
            ctx,
            _search_embeds(name, hits, chunks),
            pickers=[CardPicker(self, chunk) for chunk in chunks],
        )

    @cards.command(name="random", description="Show a random TCG Pocket card.")
    async def cards_random(self, ctx: discord.ApplicationContext) -> None:
        """Pull one card at random out of the cache."""
        await ctx.defer()
        if not await self._ready(ctx):
            return

        stub = self.tcgdex.random_stub()
        if stub is None:
            await ctx.respond("No cards cached yet.")
            return
        await self._respond_with_card(ctx, stub.id)

    @cards.command(name="id", description="Show a card by its exact id, e.g. A1-001.")
    async def cards_id(
        self,
        ctx: discord.ApplicationContext,
        id: discord.Option(  # the option name users see; shadows the builtin
            str,
            description="Exact card id, e.g. A1-001.",
            max_length=40,
        ),
    ) -> None:
        """Look a card up directly, without picking a set first."""
        await ctx.defer()
        if not await self._ready(ctx):
            return

        # TCGdex is case-insensitive but strict about width: it 404s on
        # "A1-1". Resolve through the cache so the short form works too.
        wanted = id.strip()
        stub = self.tcgdex.find_stub(wanted)
        await self._respond_with_card(ctx, stub.id if stub else wanted)

    async def _ready(self, ctx: discord.ApplicationContext) -> bool:
        """Load the cache if needed, reporting failure to the caller."""
        try:
            await self.tcgdex.ensure_loaded()
        except TcgdexError as exc:
            _log.warning("TCGdex unavailable: %s", exc)
            await ctx.respond("TCGdex is not answering right now. Try again shortly.")
            return False
        return True

    async def _respond_with_card(self, ctx: discord.ApplicationContext, card_id: str) -> None:
        """Fetch and render one card, or explain that it does not exist."""
        try:
            found = await self.tcgdex.get_card(card_id)
        except TcgdexError as exc:
            _log.warning("Card lookup for %r failed: %s", card_id, exc)
            await ctx.respond("TCGdex is not answering right now. Try again shortly.")
            return

        if found is None:
            await ctx.respond(
                f"No card with id `{discord.utils.escape_markdown(card_id)}`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await ctx.respond(embed=_card_embed(found))


async def _paginate(
    ctx: discord.ApplicationContext,
    embeds: list[discord.Embed],
    *,
    pickers: list[discord.ui.View] | None = None,
) -> None:
    """Send embeds as one pageable message driven by the caller.

    ``pickers`` supplies a per-page view; Pycord merges its items into the
    paginator next to the navigation buttons.
    """
    built = [
        pages.Page(embeds=[embed], custom_view=pickers[i] if pickers else None)
        for i, embed in enumerate(embeds)
    ]
    paginator = CardPaginator(
        pages=built,
        timeout=_PAGINATOR_TIMEOUT,
        author_check=True,
        disable_on_timeout=True,
        show_indicator=True,
        loop_pages=True,
    )
    await paginator.respond(ctx.interaction)


def _set_embed(found: PokemonSet, *, index: int = 0, total: int = 0) -> discord.Embed:
    """Render one set."""
    embed = discord.Embed(
        title=found.name,
        description=f"`{found.id}` · {found.series_name}",
        colour=discord.Colour.gold(),
    )
    if found.logo_url:
        embed.set_image(url=found.logo_url)
    if found.symbol_url:
        embed.set_thumbnail(url=found.symbol_url)

    embed.add_field(name="Released", value=found.release_date or "—")
    embed.add_field(name="Cards", value=f"{found.total}")
    embed.add_field(name="Official", value=f"{found.official}" if found.official else "—")
    if found.boosters:
        embed.add_field(name="Packs", value=", ".join(found.boosters), inline=False)

    embed.set_footer(text=f"TCGdex • {index}/{total}" if total else "TCGdex")
    return embed


def _chunk(stubs: list[CardStub]) -> list[list[CardStub]]:
    """Split a card listing into pages."""
    return [stubs[i : i + _CARDS_PER_PAGE] for i in range(0, len(stubs), _CARDS_PER_PAGE)]


def _card_list_embeds(found: PokemonSet, chunks: list[list[CardStub]]) -> list[discord.Embed]:
    """Render a set's cards as compact, pageable listings."""
    embeds = []
    for page_number, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title=f"{found.name} · cards",
            description="\n".join(f"`{c.local_id}` {c.name} — `{c.id}`" for c in chunk),
            colour=discord.Colour.gold(),
        )
        if found.symbol_url:
            embed.set_thumbnail(url=found.symbol_url)
        embed.set_footer(text=f"{len(found.cards)} cards • TCGdex • {page_number}/{len(chunks)}")
        embeds.append(embed)
    return embeds


def _search_embeds(
    query: str, hits: list[CardStub], chunks: list[list[CardStub]]
) -> list[discord.Embed]:
    """Render name-search results, naming the set each card came from."""
    embeds = []
    for page_number, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title=f"Cards matching “{query}”",
            description="\n".join(f"`{c.id}` {c.name} — {c.set_name}" for c in chunk),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text=f"{len(hits)} found • TCGdex • {page_number}/{len(chunks)}")
        embeds.append(embed)
    return embeds


def _rarity(rarity: str) -> str:
    """Render a rarity the way the game draws it, keeping the words too."""
    if not rarity:
        return "—"
    symbol = _RARITY_SYMBOLS.get(rarity)
    return f"{symbol} {rarity}" if symbol else rarity


def _card_colour(card: PokemonCard) -> discord.Colour:
    """Tint by the card's first energy type."""
    for kind in card.types:
        if kind in _TYPE_COLOURS:
            return discord.Colour(_TYPE_COLOURS[kind])
    return discord.Colour.gold()


def _attack_lines(card: PokemonCard) -> str:
    """Format the attack block, cost first."""
    lines = []
    for attack in card.attacks:
        cost = " ".join(f"[{c}]" for c in attack.cost) or "[—]"
        damage = f" — **{attack.damage}**" if attack.damage else ""
        lines.append(f"{cost} **{attack.name}**{damage}")
        if attack.effect:
            lines.append(f"> {attack.effect}")
    return "\n".join(lines)


def _card_embed(card: PokemonCard) -> discord.Embed:
    """Render one card in full."""
    embed = discord.Embed(
        title=card.name,
        description=card.description or card.effect or None,
        colour=_card_colour(card),
    )
    if card.image_url:
        embed.set_image(url=card.image_url)

    embed.add_field(name="Set", value=f"{card.set_name}\n`{card.set_id}`")
    embed.add_field(name="Card", value=f"#{card.local_id}\n`{card.id}`")
    embed.add_field(name="Rarity", value=_rarity(card.rarity))

    if card.hp is not None:
        embed.add_field(name="HP", value=str(card.hp))
    if card.types:
        embed.add_field(name="Type", value=", ".join(card.types))
    if card.stage:
        embed.add_field(name="Stage", value=card.stage)
    if card.trainer_type:
        embed.add_field(name="Trainer", value=card.trainer_type)

    if card.attacks:
        embed.add_field(name="Attacks", value=_attack_lines(card)[:1024], inline=False)
    if card.weaknesses:
        embed.add_field(name="Weakness", value=", ".join(card.weaknesses))
    if card.retreat is not None:
        embed.add_field(name="Retreat", value=str(card.retreat))
    if card.boosters:
        embed.add_field(name="Found in", value=", ".join(card.boosters), inline=False)

    footer = card.category or "Card"
    if card.illustrator:
        footer += f" • illus. {card.illustrator}"
    embed.set_footer(text=f"{footer} • TCGdex")
    return embed


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    bot.add_cog(Pokemon(bot))

"""Anime and manga search, backed by AniList with a MyAnimeList fallback.

Three things shape this cog:

* **Autocomplete is the primary way in.** Typing three characters into
  ``search`` suggests real titles; picking one returns that exact title as a
  single embed, which is both faster and more accurate than searching and then
  paging. A picked suggestion arrives as ``provider:id`` rather than as text.
* **Results are ranked locally.** Neither provider puts the obvious answer
  first — MAL ranks ``Cowboy Bebop: The Movie`` above ``Cowboy Bebop`` — so
  :func:`~..services.media.rank_by_relevance` reorders every title search.
* **AniList outages are remembered.** A 403 opens a short circuit breaker in
  Redis, so the next search goes straight to MyAnimeList instead of paying a
  failed round trip each time. Without Redis the breaker simply never opens and
  behaviour is unchanged.

Note: this module deliberately does NOT use ``from __future__ import annotations``.
See the note in ``general.py``.
"""

import asyncio
import contextlib
import datetime as dt
import logging
import re

import discord
from discord.ext import commands, pages

from ..services.anilist import (
    AniListClient,
    AniListError,
    AniListRateLimitedError,
    AniListUnavailableError,
)
from ..services.mal import MalClient, MalError, MalNotConfiguredError, unsupported_filters
from ..services.media import (
    Media,
    MediaDetails,
    details_from_cacheable,
    details_to_cacheable,
    from_cacheable,
    rank_by_relevance,
    to_cacheable,
)

_log = logging.getLogger(__name__)

# How many results to pull. AniList allows 50; 25 is plenty to page through and
# keeps the payload small.
_PER_PAGE = 25

# Embed descriptions cap at 4096, but a synopsis that long buries every field
# below it.
_MAX_SYNOPSIS = 600

_PAGINATOR_TIMEOUT = 300.0

# Search results are cached briefly: long enough that paging back through the
# same query is free and repeat searches do not burn AniList's rate limit,
# short enough that a newly added title shows up the same day.
_SEARCH_TTL = 15 * 60

# Relations and recommendations change far more slowly than scores do.
_DETAILS_TTL = 6 * 60 * 60

# How long to stop calling AniList after it refuses with a 403. Long enough
# that a sustained outage costs one failed call per half hour, short enough
# that a recovery is picked up without a restart.
_ANILIST_DOWN_KEY = "anilist:down"
_ANILIST_DOWN_TTL = 30 * 60

# Discord gives autocomplete three seconds and shows nothing if the deadline
# passes, so the lookup is capped well inside it.
_AUTOCOMPLETE_BUDGET = 2.0
_AUTOCOMPLETE_LIMIT = 25
# Shorter than this and MAL rejects the query outright.
_MIN_AUTOCOMPLETE = 3

# Discord caps a choice name at 100 characters.
_MAX_CHOICE_NAME = 100

# What autocomplete puts in the option when a suggestion is picked, so the
# command can fetch that exact title instead of searching for its name.
_PICKED = re.compile(r"^(anilist|mal):(\d+)$")
_PROVIDER_TAGS = {"AniList": "anilist", "MyAnimeList": "mal"}
_PROVIDER_NAMES = {"anilist": "AniList", "mal": "MyAnimeList"}

# AniList's northern-hemisphere seasons, by starting month.
_SEASON_BY_MONTH = {
    12: "WINTER",
    1: "WINTER",
    2: "WINTER",
    3: "SPRING",
    4: "SPRING",
    5: "SPRING",
    6: "SUMMER",
    7: "SUMMER",
    8: "SUMMER",
    9: "FALL",
    10: "FALL",
    11: "FALL",
}

_ANIME_FORMATS = ["TV", "TV Short", "Movie", "Special", "OVA", "ONA", "Music"]
_MANGA_FORMATS = ["Manga", "Novel", "One Shot"]
_STATUSES = ["Finished", "Releasing", "Not Yet Released", "Cancelled", "Hiatus"]
_SEASONS = ["Winter", "Spring", "Summer", "Fall"]
_COUNTRIES = ["Japan", "South Korea", "China", "Taiwan"]
_COUNTRY_CODES = {"Japan": "JP", "South Korea": "KR", "China": "CN", "Taiwan": "TW"}
_SOURCES = [
    "Original",
    "Manga",
    "Light Novel",
    "Visual Novel",
    "Video Game",
    "Novel",
    "Doujinshi",
    "Anime",
    "Web Novel",
    "Live Action",
    "Game",
    "Comic",
    "Multimedia Project",
    "Picture Book",
    "Other",
]
# AniList's fixed genre list. `Hentai` is omitted deliberately: adult results
# are gated on the channel, not offered as a filter.
_GENRES = [
    "Action",
    "Adventure",
    "Comedy",
    "Drama",
    "Ecchi",
    "Fantasy",
    "Horror",
    "Mahou Shoujo",
    "Mecha",
    "Music",
    "Mystery",
    "Psychological",
    "Romance",
    "Sci-Fi",
    "Slice of Life",
    "Sports",
    "Supernatural",
    "Thriller",
]

_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

# How the MAL and AniList watch-status keys are labelled in the details panel.
_STATUS_LABELS = {
    "watching": "Watching",
    "current": "Watching",
    "completed": "Completed",
    "on_hold": "On hold",
    "paused": "On hold",
    "dropped": "Dropped",
    "plan_to_watch": "Planned",
    "planning": "Planned",
    "repeating": "Rewatching",
}


def _enum(value: str | None) -> str | None:
    """Turn a human choice into the SCREAMING_SNAKE AniList expects."""
    if not value:
        return None
    return value.upper().replace(" ", "_").replace("-", "_")


# `.title()` would render these as "Tv", "Ova", "Ona".
_DISPLAY_OVERRIDES = {
    "TV": "TV",
    "TV_SHORT": "TV Short",
    "OVA": "OVA",
    "ONA": "ONA",
    "ONE_SHOT": "One Shot",
}


def _pretty(value: str) -> str:
    """Inverse of :func:`_enum`, for display."""
    if not value:
        return "—"
    if value in _DISPLAY_OVERRIDES:
        return _DISPLAY_OVERRIDES[value]
    return value.replace("_", " ").title()


def _date_bounds(year: int | None, month: str | None) -> tuple[int | None, int | None]:
    """Build AniList FuzzyDateInt bounds (YYYYMMDD) from a year and/or month.

    Both ends are inclusive, so a bare year spans 0101 to 1231 and a year plus
    month spans that month. A month without a year cannot be expressed.
    """
    if year is None:
        return None, None
    if month is None:
        return year * 10000 + 101, year * 10000 + 1231
    m = _MONTHS.index(month) + 1
    return year * 10000 + m * 100 + 1, year * 10000 + m * 100 + 31


async def _anime_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Suggest anime titles. Module level so Pycord treats it as a plain callable."""
    return await _suggest(ctx, anime=True)


async def _manga_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Suggest manga titles."""
    return await _suggest(ctx, anime=False)


async def _suggest(ctx: discord.AutocompleteContext, *, anime: bool) -> list[discord.OptionChoice]:
    """Look up title suggestions for the half-typed value.

    Never raises and never blocks past :data:`_AUTOCOMPLETE_BUDGET`: a failed
    autocomplete should quietly offer nothing and let the user press enter,
    not break the command they are in the middle of typing.
    """
    cog = ctx.cog
    value = str(ctx.value or "").strip()
    if not isinstance(cog, MediaSearch) or len(value) < _MIN_AUTOCOMPLETE:
        return []

    filters = {"search": value}
    if not _nsfw_allowed(ctx.interaction.channel):
        filters["isAdult"] = False

    try:
        async with asyncio.timeout(_AUTOCOMPLETE_BUDGET):
            results, _ = await cog.search(anime=anime, filters=filters)
    except TimeoutError, AniListError, MalError:
        # Includes MalNotConfiguredError: nothing to suggest, nothing to say.
        return []
    except Exception:
        _log.exception("Autocomplete failed for %r", value)
        return []

    choices = []
    for media in results[:_AUTOCOMPLETE_LIMIT]:
        tag = _PROVIDER_TAGS.get(media.provider)
        if tag is None or not media.id:
            continue
        choices.append(
            discord.OptionChoice(
                name=_choice_label(media)[:_MAX_CHOICE_NAME],
                value=f"{tag}:{media.id}",
            )
        )
    return choices


def _choice_label(media: Media) -> str:
    """Label a suggestion so near-identical titles can be told apart."""
    bits = []
    if media.format:
        bits.append(_pretty(media.format))
    if media.season_year:
        bits.append(str(media.season_year))
    elif media.start_date.year:
        bits.append(str(media.start_date.year))
    return f"{media.title} ({', '.join(bits)})" if bits else media.title


def _nsfw_allowed(channel: object) -> bool:
    """Whether Discord says adult results are acceptable here."""
    is_nsfw = getattr(channel, "is_nsfw", None)
    return bool(callable(is_nsfw) and is_nsfw())


class DetailsView(discord.ui.View):
    """A Details button that reports on whichever result is on screen.

    The button is handed to the paginator as a custom view, so it survives page
    flips; it reads ``Paginator.current_page`` rather than being rebuilt per
    page, which is what caused components to accumulate in ``/pokemon``.
    """

    def __init__(
        self,
        cog: MediaSearch,
        results: list[Media],
        *,
        anime: bool,
        row: int,
        timeout: float = _PAGINATOR_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)

        self._cog = cog
        self._results = results
        self._anime = anime
        self._paginator: pages.Paginator | None = None

        button = discord.ui.Button(
            label="Details",
            style=discord.ButtonStyle.secondary,
            emoji="🔎",
            row=row,
        )
        button.callback = self._on_details
        self.add_item(button)

    def attach(self, paginator: pages.Paginator) -> None:
        """Tell the view which paginator to read the current page from."""
        self._paginator = paginator

    @property
    def current(self) -> Media:
        """The result currently on screen."""
        index = self._paginator.current_page if self._paginator is not None else 0
        if 0 <= index < len(self._results):
            return self._results[index]
        return self._results[0]

    async def _on_details(self, interaction: discord.Interaction) -> None:
        media = self.current

        # Ephemeral, so the panel does not bury the result everyone is reading,
        # and deferred because it may cost an upstream call.
        await interaction.response.defer(ephemeral=True)

        try:
            details = await self._cog.details(media, anime=self._anime)
        except (AniListError, MalError) as exc:
            _log.warning("Details lookup failed for %s %s: %s", media.provider, media.id, exc)
            await interaction.followup.send(
                f"{media.provider} could not be reached for more detail.", ephemeral=True
            )
            return

        if not details:
            await interaction.followup.send(
                f"{media.provider} has nothing more on **{media.title}**.", ephemeral=True
            )
            return

        await interaction.followup.send(embed=_details_embed(media, details), ephemeral=True)


class MediaSearch(commands.Cog):
    """/anime and /manga."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.anilist = AniListClient()
        # Fallback for when AniList refuses requests, which it currently does
        # for some clients. See ``_fetch``.
        self.mal = MalClient(bot.config.mal_client_id)
        self.cache = bot.cache

    def cog_unload(self) -> None:
        """Release the HTTP sessions. ``cog_unload`` is synchronous in Pycord."""
        self.bot.loop.create_task(self.anilist.close())
        self.bot.loop.create_task(self.mal.close())

    @discord.slash_command(name="anime", description="Search anime on AniList.")
    async def anime(
        self,
        ctx: discord.ApplicationContext,
        search: discord.Option(
            str,
            description="Title to search for. Pick a suggestion for an exact match.",
            autocomplete=_anime_autocomplete,
            default=None,
        ),
        genre: discord.Option(str, description="Genre.", choices=_GENRES, default=None),
        format: discord.Option(  # shadows the builtin; it is the option name users see
            str, description="Format.", choices=_ANIME_FORMATS, default=None
        ),
        status: discord.Option(str, description="Status.", choices=_STATUSES, default=None),
        season: discord.Option(str, description="Season.", choices=_SEASONS, default=None),
        year: discord.Option(
            int, description="Release year.", min_value=1940, max_value=2100, default=None
        ),
        month: discord.Option(
            str, description="Release month (needs a year).", choices=_MONTHS, default=None
        ),
        source: discord.Option(str, description="Source material.", choices=_SOURCES, default=None),
        country: discord.Option(
            str, description="Country of origin.", choices=_COUNTRIES, default=None
        ),
    ) -> None:
        """Search anime, defaulting to the current season when given nothing."""
        if await self._handle_pick(ctx, search, anime=True):
            return

        after, before = _date_bounds(year, month)

        # Season needs a year to mean anything; with neither, and no other
        # filter, fall back to what is airing now.
        season_enum = _enum(season)
        season_year = year if season_enum else None
        filters = {
            "search": search,
            "genre": genre,
            "format": _enum(format),
            "status": _enum(status),
            "season": season_enum,
            "seasonYear": season_year,
            "startDate_greater": after,
            "startDate_lesser": before,
            "source": _enum(source),
            "countryOfOrigin": _COUNTRY_CODES.get(country) if country else None,
        }

        described = _describe(filters, season=season, year=year, month=month, country=country)
        if not any(v is not None for v in filters.values()):
            now = dt.datetime.now(dt.UTC)
            filters["season"] = _SEASON_BY_MONTH[now.month]
            # December belongs to the *next* year's winter season.
            filters["seasonYear"] = now.year + 1 if now.month == 12 else now.year
            described = f"this season ({_pretty(filters['season'])} {filters['seasonYear']})"

        await self._run(ctx, anime=True, filters=filters, described=described)

    @discord.slash_command(name="manga", description="Search manga on AniList.")
    async def manga(
        self,
        ctx: discord.ApplicationContext,
        search: discord.Option(
            str,
            description="Title to search for. Pick a suggestion for an exact match.",
            autocomplete=_manga_autocomplete,
            default=None,
        ),
        genre: discord.Option(str, description="Genre.", choices=_GENRES, default=None),
        format: discord.Option(  # shadows the builtin; it is the option name users see
            str, description="Format.", choices=_MANGA_FORMATS, default=None
        ),
        status: discord.Option(str, description="Status.", choices=_STATUSES, default=None),
        year: discord.Option(
            int, description="Release year.", min_value=1940, max_value=2100, default=None
        ),
        month: discord.Option(
            str, description="Release month (needs a year).", choices=_MONTHS, default=None
        ),
        source: discord.Option(str, description="Source material.", choices=_SOURCES, default=None),
        country: discord.Option(
            str, description="Country of origin.", choices=_COUNTRIES, default=None
        ),
    ) -> None:
        """Search manga, defaulting to the current year when given nothing."""
        if await self._handle_pick(ctx, search, anime=False):
            return

        after, before = _date_bounds(year, month)
        filters = {
            "search": search,
            "genre": genre,
            "format": _enum(format),
            "status": _enum(status),
            "startDate_greater": after,
            "startDate_lesser": before,
            "source": _enum(source),
            "countryOfOrigin": _COUNTRY_CODES.get(country) if country else None,
        }

        described = _describe(filters, year=year, month=month, country=country)
        if not any(v is not None for v in filters.values()):
            this_year = dt.datetime.now(dt.UTC).year
            filters["startDate_greater"], filters["startDate_lesser"] = _date_bounds(
                this_year, None
            )
            described = f"this year ({this_year})"

        await self._run(ctx, anime=False, filters=filters, described=described)

    async def _handle_pick(
        self, ctx: discord.ApplicationContext, search: str | None, *, anime: bool
    ) -> bool:
        """Answer directly when ``search`` is an autocomplete pick.

        Returns whether the command was handled. Every other filter is ignored
        on purpose: the user chose one specific title, so narrowing it further
        could only ever produce nothing.
        """
        match = _PICKED.match(str(search or ""))
        if match is None:
            return False

        provider, raw_id = _PROVIDER_NAMES[match.group(1)], int(match.group(2))
        await ctx.defer()

        try:
            media = await self._one(provider, raw_id, anime=anime)
        except (AniListError, MalError) as exc:
            _log.warning("Direct %s lookup of %s failed: %s", provider, raw_id, exc)
            await ctx.respond("That title could not be loaded. Try searching for it instead.")
            return True

        if media.is_adult and not _nsfw_allowed(ctx.channel):
            await ctx.respond("That title is marked adult; open it in an age-restricted channel.")
            return True

        await self._present(ctx, [media], anime=anime, notice="")
        return True

    async def _run(
        self,
        ctx: discord.ApplicationContext,
        *,
        anime: bool,
        filters: dict,
        described: str,
    ) -> None:
        """Search, then hand the results to the paginator."""
        await ctx.defer()

        # Adult titles only where Discord says they are allowed.
        if not _nsfw_allowed(ctx.channel):
            filters["isAdult"] = False

        kind = "anime" if anime else "manga"
        try:
            results, notice = await self.search(anime=anime, filters=filters)
        except AniListRateLimitedError as exc:
            await ctx.respond(str(exc))
            return
        except MalNotConfiguredError as exc:
            _log.warning("AniList unavailable and MAL not usable: %s", exc)
            await ctx.respond(
                "AniList is not answering, and the MyAnimeList fallback is not "
                "configured. Set `MAL_CLIENT_ID` in the bot's .env."
            )
            return
        except (AniListError, MalError) as exc:
            _log.warning("%s search failed: %s", kind, exc)
            await ctx.respond(
                "Neither AniList nor MyAnimeList could be reached. Try again shortly."
            )
            return

        if not results:
            await ctx.respond(
                f"No {kind} found for {described}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self._present(ctx, results, anime=anime, notice=notice)

    async def _present(
        self,
        ctx: discord.ApplicationContext,
        results: list[Media],
        *,
        anime: bool,
        notice: str,
    ) -> None:
        """Show one result plainly, or many through the paginator."""
        embeds = [
            _build_embed(m, anime=anime, index=i, total=len(results))
            for i, m in enumerate(results, start=1)
        ]

        if len(results) == 1:
            # Nav buttons over a single page are just noise, so the Details
            # button stands alone on the first row.
            view = DetailsView(self, results, anime=anime, row=0)
            await ctx.respond(embed=embeds[0], view=view)
        else:
            # Row 0 belongs to the paginator's own navigation.
            view = DetailsView(self, results, anime=anime, row=1)
            paginator = pages.Paginator(
                pages=[pages.Page(embeds=[e]) for e in embeds],
                timeout=_PAGINATOR_TIMEOUT,
                # Only the person who ran the command drives it; everyone can read.
                author_check=True,
                disable_on_timeout=True,
                show_indicator=True,
                loop_pages=True,
                custom_view=view,
            )
            view.attach(paginator)
            await paginator.respond(ctx.interaction)

        if notice:
            await ctx.followup.send(notice, ephemeral=True)

    async def search(self, *, anime: bool, filters: dict) -> tuple[list[Media], str]:
        """Search, cached. Public because autocomplete calls it too.

        The switch to MyAnimeList is not announced — every embed footer already
        names the provider that answered. The returned note only ever covers
        filters MAL could not honour, which nothing else would reveal.
        """
        degraded = await self._anilist_is_down()
        key = _cache_key(anime=anime, filters=filters, degraded=degraded)

        cached = await self.cache.get(key)
        if cached is not None:
            return [from_cacheable(m) for m in cached["results"]], str(cached.get("notice") or "")

        results, notice = await self._fetch(anime=anime, filters=filters, skip_anilist=degraded)

        # The key is read on a *prediction* of who will answer, but this call
        # may itself have flipped the breaker. Write under whoever actually
        # answered, which is the key the next read will compute -- otherwise
        # the first search of an outage caches under a key nothing ever reads.
        answered_degraded = results[0].provider != "AniList" if results else degraded
        write_key = _cache_key(anime=anime, filters=filters, degraded=answered_degraded)

        # Serialising happens here, outside Cache.set's own error handling, so
        # guard it too: a search must never fail because caching it did.
        try:
            await self.cache.set(
                write_key,
                {"results": [to_cacheable(m) for m in results], "notice": notice},
                ttl=_SEARCH_TTL,
            )
        except TypeError, ValueError:
            _log.exception("Could not cache a %s search", "anime" if anime else "manga")

        return results, notice

    async def _fetch(
        self, *, anime: bool, filters: dict, skip_anilist: bool
    ) -> tuple[list[Media], str]:
        """Hit AniList, falling back to MyAnimeList, with no caching."""
        query = filters.get("search")

        if not skip_anilist:
            try:
                results = await self.anilist.search(anime=anime, per_page=_PER_PAGE, **filters)
                return rank_by_relevance(results, query), ""
            except AniListUnavailableError as exc:
                # A deliberate refusal, not a blip: stop asking for a while.
                _log.warning("AniList unavailable, falling back to MyAnimeList: %s", exc)
                await self._mark_anilist_down()
            except AniListRateLimitedError:
                # A backoff is not an outage, and switching provider would only
                # hide that the caller should wait.
                raise
            except AniListError as exc:
                # Could be a passing network fault, so do not open the breaker.
                _log.warning("AniList failed, falling back to MyAnimeList: %s", exc)

        results = await self.mal.search(anime=anime, per_page=_PER_PAGE, **filters)

        dropped = unsupported_filters(filters, anime=anime)
        notice = ""
        if dropped:
            ignored = ", ".join(sorted(set(dropped)))
            notice = f"MyAnimeList cannot filter by {ignored}, so that was ignored."
        return rank_by_relevance(results, query), notice

    async def _one(self, provider: str, media_id: int, *, anime: bool) -> Media:
        """Fetch one title by id from the provider that suggested it."""
        key = f"media:{'anime' if anime else 'manga'}:one:{provider}:{media_id}"
        cached = await self.cache.get(key)
        if cached is not None:
            return from_cacheable(cached)

        client = self.anilist if provider == "AniList" else self.mal
        media = await client.get(media_id, anime=anime)

        with contextlib.suppress(TypeError, ValueError):
            await self.cache.set(key, to_cacheable(media), ttl=_SEARCH_TTL)
        return media

    async def details(self, media: Media, *, anime: bool) -> MediaDetails:
        """Fetch the second layer for one title, cached."""
        key = f"media:{'anime' if anime else 'manga'}:details:{media.provider}:{media.id}"
        cached = await self.cache.get(key)
        if cached is not None:
            return details_from_cacheable(cached)

        client = self.anilist if media.provider == "AniList" else self.mal
        found = await client.details(media.id, anime=anime)

        with contextlib.suppress(TypeError, ValueError):
            await self.cache.set(key, details_to_cacheable(found), ttl=_DETAILS_TTL)
        return found

    async def _anilist_is_down(self) -> bool:
        """Whether AniList refused us recently enough to skip it.

        Falls to ``False`` with no Redis, which just means every search pays the
        failed AniList call as it did before the breaker existed.
        """
        return bool(await self.cache.get(_ANILIST_DOWN_KEY))

    async def _mark_anilist_down(self) -> None:
        await self.cache.set(_ANILIST_DOWN_KEY, True, ttl=_ANILIST_DOWN_TTL)


def _cache_key(*, anime: bool, filters: dict, degraded: bool) -> str:
    """A stable key for one search.

    Sorted, and with unset filters dropped, so the same query always produces
    the same key regardless of how the options were ordered. ``degraded`` is
    part of the key so that results fetched while AniList was down are not
    still being served once it recovers.
    """
    active = sorted((k, str(v)) for k, v in filters.items() if v is not None)
    shape = ",".join(f"{k}={v}" for k, v in active)
    suffix = ":mal" if degraded else ""
    return f"media:{'anime' if anime else 'manga'}{suffix}:{shape}"


def _describe(filters: dict, **friendly: object) -> str:
    """Summarise the active filters for the no-results message."""
    parts = [str(v) for v in friendly.values() if v]
    if filters.get("search"):
        parts.insert(0, f"“{filters['search']}”")
    for key in ("genre", "format", "status", "source"):
        if filters.get(key):
            parts.append(_pretty(str(filters[key])))
    return ", ".join(parts) if parts else "that search"


def _score(media: Media) -> str:
    scores = []
    if media.average_score is not None:
        scores.append(f"{media.average_score}% avg")
    if media.mean_score is not None and media.mean_score != media.average_score:
        scores.append(f"{media.mean_score}% mean")
    return " · ".join(scores) or "—"


def _counts(media: Media, *, anime: bool) -> str:
    """The episodes/duration or chapters/volumes line."""
    if anime:
        bits = []
        if media.episodes:
            bits.append(f"{media.episodes} ep")
        if media.duration:
            bits.append(f"{media.duration} min each")
        return " · ".join(bits) or "—"

    bits = []
    if media.chapters:
        bits.append(f"{media.chapters} ch")
    if media.volumes:
        bits.append(f"{media.volumes} vol")
    return " · ".join(bits) or "—"


def _run_dates(media: Media) -> str:
    if not media.start_date:
        return "—"
    if not media.end_date:
        return f"{media.start_date} → ?"
    if str(media.start_date) == str(media.end_date):
        return str(media.start_date)
    return f"{media.start_date} → {media.end_date}"


def _next_episode(media: Media) -> str:
    """The next-episode line, rendered as a Discord timestamp.

    Discord localises ``<t:...:R>`` per viewer, which is the whole point: JST
    broadcast slots mean nothing to most people reading them.
    """
    when = media.airing_at()
    if when is None:
        return ""

    moment = dt.datetime.fromtimestamp(when, dt.UTC)
    stamp = f"{discord.utils.format_dt(moment, 'R')} ({discord.utils.format_dt(moment, 'f')})"
    if media.next_episode:
        return f"Episode {media.next_episode} — {stamp}"
    return stamp


def _embed_colour(media: Media) -> discord.Colour:
    """Use the cover's dominant colour, so each entry looks like its artwork."""
    raw = (media.cover_color or "").lstrip("#")
    if len(raw) == 6:
        try:
            return discord.Colour(int(raw, 16))
        except ValueError:
            pass
    return discord.Colour.blurple()


def _build_embed(media: Media, *, anime: bool, index: int, total: int) -> discord.Embed:
    """Render one result."""
    synopsis = media.description
    if len(synopsis) > _MAX_SYNOPSIS:
        synopsis = synopsis[:_MAX_SYNOPSIS].rstrip() + "…"

    header = " · ".join(p for p in (_pretty(media.format), _pretty(media.status)) if p != "—")

    embed = discord.Embed(
        title=media.title,
        url=media.site_url or None,
        description=synopsis or "No synopsis.",
        colour=_embed_colour(media),
    )
    if media.subtitle:
        embed.set_author(name=media.subtitle[:256])
    if media.cover_url:
        embed.set_thumbnail(url=media.cover_url)

    embed.add_field(name="Episodes" if anime else "Chapters", value=_counts(media, anime=anime))
    embed.add_field(name="Score", value=_score(media))
    embed.add_field(
        name="Popularity",
        value=f"{media.popularity:,}" if media.popularity else "—",
    )

    if anime:
        season = " ".join(
            str(p) for p in (_pretty(media.season) if media.season else "", media.season_year) if p
        ).strip()
        embed.add_field(name="Season", value=season or "—")
    embed.add_field(name="Source", value=_pretty(media.source))
    embed.add_field(name="Aired" if anime else "Published", value=_run_dates(media))

    # Only ever set while a show is actually airing, so it doubles as a badge
    # for "this is on right now".
    upcoming = _next_episode(media) if anime else ""
    if upcoming:
        embed.add_field(name="Next episode", value=upcoming, inline=False)

    if media.genres:
        embed.add_field(name="Genres", value=", ".join(media.genres), inline=False)
    if anime and media.studios:
        embed.add_field(name="Studio", value=", ".join(media.studios), inline=False)
    if media.trailer_url:
        embed.add_field(name="Trailer", value=media.trailer_url, inline=False)

    provider = media.provider or "AniList"
    tail = f"{provider} • {index}/{total}"
    footer = f"{header} • {tail}" if header else tail
    if media.is_adult:
        footer = f"18+ • {footer}"
    embed.set_footer(text=footer)
    return embed


def _linked(items: list, limit: int) -> str:
    """Render titles as markdown links, trimmed to fit one embed field."""
    lines = []
    for item in items:
        label = discord.utils.escape_markdown(item.title)
        line = f"[{label}]({item.url})" if item.url else label
        if item.kind and item.kind != "Recommended":
            line = f"**{item.kind}** — {line}"
        lines.append(f"- {line}")

    text = "\n".join(lines)
    if len(text) <= limit:
        return text

    # Drop whole entries rather than cutting one in half mid-link.
    kept = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def _details_embed(media: Media, details: MediaDetails) -> discord.Embed:
    """Render the Details panel for one title."""
    embed = discord.Embed(
        title=f"{media.title} — more",
        url=media.site_url or None,
        colour=_embed_colour(media),
    )

    if details.relations:
        embed.add_field(name="Related", value=_linked(details.relations, 1024), inline=False)
    if details.recommendations:
        embed.add_field(
            name="If you liked this",
            value=_linked(details.recommendations, 1024),
            inline=False,
        )
    if details.links:
        embed.add_field(
            name="Where to watch",
            value=" · ".join(
                f"[{discord.utils.escape_markdown(link.name)}]({link.url})"
                for link in details.links
            )[:1024],
            inline=False,
        )
    if details.staff:
        embed.add_field(name="Credits", value="\n".join(f"- {s}" for s in details.staff)[:1024])
    if details.stats:
        embed.add_field(name="On lists", value=_stats_line(details.stats), inline=False)

    embed.set_footer(text=media.provider or "AniList")
    return embed


def _stats_line(stats: dict) -> str:
    """Render the watch-status breakdown, largest first."""
    ordered = sorted(stats.items(), key=lambda kv: -kv[1])
    return " · ".join(
        f"{_STATUS_LABELS.get(key, key.replace('_', ' ').capitalize())} {value:,}"
        for key, value in ordered
        if value
    )


def setup(bot: discord.Bot) -> None:
    """Pycord extension hook. Note: synchronous, unlike discord.py."""
    bot.add_cog(MediaSearch(bot))

"""The media domain model, shared by every provider.

``Media`` is deliberately provider-neutral: AniList and MyAnimeList both map
onto it, so the embed builder never needs to know where a result came from
beyond the :attr:`Media.provider` label it prints in the footer.

Field vocabulary follows AniList's (``FINISHED``, ``TV_SHORT``, ``SPRING``)
because it is the richer of the two; :mod:`.mal` translates into it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

# Anime broadcast times are quoted in JST by both providers. Japan has no DST,
# so a naive weekday+time is unambiguous once anchored to this zone.
BROADCAST_TZ = ZoneInfo("Asia/Tokyo")

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True, slots=True)
class FuzzyDate:
    """AniList dates where any part may be missing."""

    year: int | None = None
    month: int | None = None
    day: int | None = None

    def __str__(self) -> str:
        if self.year is None:
            return "?"
        if self.month is None:
            return str(self.year)
        if self.day is None:
            return f"{self.year}-{self.month:02d}"
        return f"{self.year}-{self.month:02d}-{self.day:02d}"

    def __bool__(self) -> bool:
        return self.year is not None


@dataclass(frozen=True, slots=True)
class Media:
    """One anime or manga, reduced to what the embed shows."""

    id: int
    site_url: str
    title_romaji: str
    title_english: str
    title_native: str
    description: str
    format: str
    status: str
    season: str
    season_year: int | None
    country: str
    source: str
    genres: list[str]
    average_score: int | None
    mean_score: int | None
    popularity: int | None
    favourites: int | None
    is_adult: bool
    cover_url: str
    cover_color: str
    banner_url: str
    start_date: FuzzyDate
    end_date: FuzzyDate
    trailer_url: str
    # Which API this came from, shown in the embed footer.
    provider: str = "AniList"
    studios: list[str] = field(default_factory=list)
    # Anime only
    episodes: int | None = None
    duration: int | None = None
    # Anime only, and only while airing. AniList knows exactly which episode is
    # next and when; MAL only publishes a weekly slot, hence both shapes.
    next_episode: int | None = None
    next_airing_at: int | None = None
    broadcast_day: str = ""
    broadcast_time: str = ""
    # Manga only
    chapters: int | None = None
    volumes: int | None = None

    @property
    def title(self) -> str:
        """Best available display title."""
        return self.title_english or self.title_romaji or self.title_native or "Untitled"

    @property
    def subtitle(self) -> str:
        """The other titles, when they differ from the display title."""
        others = [t for t in (self.title_romaji, self.title_native) if t and t != self.title]
        return " • ".join(others)

    def airing_at(self, *, now: dt.datetime | None = None) -> int | None:
        """Unix timestamp of the next episode, or None if nothing is scheduled.

        AniList's exact timestamp wins when it is still in the future. Otherwise
        the weekly MAL slot is projected forward, which is why this is computed
        on render rather than stored: a cached timestamp goes stale, a weekday
        does not.
        """
        moment = now or dt.datetime.now(BROADCAST_TZ)

        if self.next_airing_at and self.next_airing_at > moment.timestamp():
            return self.next_airing_at

        # MAL keeps the historical slot on finished shows -- Cowboy Bebop still
        # reports "friday" -- so the weekly projection is only meaningful while
        # a title is actually airing.
        if self.status != "RELEASING":
            return None

        weekday = _WEEKDAYS.get(self.broadcast_day.strip().casefold())
        if weekday is None:
            return None

        try:
            hour, minute = (int(part) for part in self.broadcast_time.split(":")[:2])
        except ValueError:
            return None

        local = moment.astimezone(BROADCAST_TZ)
        slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        slot += dt.timedelta(days=(weekday - slot.weekday()) % 7)
        if slot <= local:
            slot += dt.timedelta(days=7)
        return int(slot.timestamp())


@dataclass(frozen=True, slots=True)
class Relation:
    """A linked title — a sequel, a spin-off, or a recommendation."""

    kind: str
    title: str
    url: str = ""


@dataclass(frozen=True, slots=True)
class Link:
    """An external link, such as a streaming site."""

    name: str
    url: str


@dataclass(frozen=True, slots=True)
class MediaDetails:
    """The second layer, fetched only when someone asks for it.

    Every field is optional because the two providers expose different subsets:
    MAL has no external links and no statistics for manga, AniList has no
    author credits in the shape MAL uses.
    """

    relations: list[Relation] = field(default_factory=list)
    recommendations: list[Relation] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    staff: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.relations or self.recommendations or self.links or self.stats or self.staff
        )


def to_cacheable(media: Media) -> dict[str, Any]:
    """Flatten a :class:`Media` for JSON storage."""
    return asdict(media)


def from_cacheable(data: dict[str, Any]) -> Media:
    """Rebuild a :class:`Media` written by :func:`to_cacheable`.

    ``asdict`` turns the nested :class:`FuzzyDate` fields into plain dicts, so
    they have to be reconstructed — otherwise ``media.start_date.year`` is an
    ``AttributeError`` on anything read back from the cache.

    Unknown keys are dropped rather than raising, so an entry written before a
    field was removed still loads instead of poisoning every read until it
    expires.
    """
    known = {f.name for f in Media.__dataclass_fields__.values()}
    payload = {k: v for k, v in data.items() if k in known}
    for key in ("start_date", "end_date"):
        payload[key] = FuzzyDate(**(payload.get(key) or {}))
    return Media(**payload)


def details_to_cacheable(details: MediaDetails) -> dict[str, Any]:
    """Flatten a :class:`MediaDetails` for JSON storage."""
    return asdict(details)


def details_from_cacheable(data: dict[str, Any]) -> MediaDetails:
    """Rebuild a :class:`MediaDetails` written by :func:`details_to_cacheable`."""
    return MediaDetails(
        relations=[Relation(**r) for r in data.get("relations") or []],
        recommendations=[Relation(**r) for r in data.get("recommendations") or []],
        links=[Link(**link) for link in data.get("links") or []],
        stats={str(k): int(v) for k, v in (data.get("stats") or {}).items()},
        staff=[str(s) for s in data.get("staff") or []],
    )


def _normalise(text: str) -> str:
    """Casefold and collapse whitespace, for comparing titles."""
    return " ".join(text.casefold().split())


def _match_rank(media: Media, needle: str) -> int:
    """How well a title matches, lowest is best."""
    titles = [
        _normalise(t) for t in (media.title_english, media.title_romaji, media.title_native) if t
    ]
    if any(t == needle for t in titles):
        return 0
    if any(t.startswith(needle) for t in titles):
        return 1
    if any(needle in t for t in titles):
        return 2
    return 3


def rank_by_relevance(results: list[Media], query: str | None) -> list[Media]:
    """Reorder a title search so the title actually searched for comes first.

    Neither provider does this well on its own: MAL returns ``Cowboy Bebop: The
    Movie`` above ``Cowboy Bebop``, and ``Frieren`` Season 2 above Season 1.
    Exact matches win, then prefixes, then substrings, each tier broken by
    popularity. Nothing is ever dropped — only reordered.
    """
    if not query:
        return results

    needle = _normalise(query)
    if not needle:
        return results

    return sorted(results, key=lambda m: (_match_rank(m, needle), -(m.popularity or 0)))

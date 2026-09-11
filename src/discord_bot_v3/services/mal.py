"""Client for the official MyAnimeList API v2, the fallback for AniList.

Docs: https://myanimelist.net/apiconfig/references/api/v2

Authentication is a registered application's client id, sent as
``X-MAL-CLIENT-ID``. That is enough for public read-only data; the OAuth flow is
only needed to touch a user's own list.

**MAL's search barely filters.** ``GET /anime`` accepts only ``q``, ``limit``,
``offset`` and ``fields`` — no genre, format, status, source or date
parameters. Rather than drop those filters, this client asks for a large page
and narrows it locally: every field needed is already in the response. The cost
is that a narrow filter over a broad query can come back thin, never that it
comes back wrong. Country of origin is the one filter MAL cannot express at
all, because it does not carry the field.

Where MAL has a better endpoint than free-text search, it is used: a season
query goes to ``/anime/season/{year}/{season}``, and an unfiltered browse goes
to ``/anime/ranking`` or ``/manga/ranking``.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .media import FuzzyDate, Media, MediaDetails, Relation

_log = logging.getLogger(__name__)

BASE_URL = "https://api.myanimelist.net/v2"
SITE_URL = "https://myanimelist.net"

# MAL caps a search page at 100.
MAX_PER_PAGE = 100

# MAL rejects a search term shorter than this.
MIN_QUERY = 3

# How many candidates to pull before narrowing locally. Generous, because the
# filtering happens here rather than server-side.
CANDIDATE_POOL = 100

_TIMEOUT = aiohttp.ClientTimeout(total=15)

_ANIME_FIELDS = ",".join(
    (
        "id",
        "title",
        "main_picture",
        "alternative_titles",
        "start_date",
        "end_date",
        "synopsis",
        "mean",
        "rank",
        "popularity",
        "num_list_users",
        "num_scoring_users",
        "nsfw",
        "genres",
        "media_type",
        "status",
        "num_episodes",
        "start_season",
        "source",
        "average_episode_duration",
        "rating",
        "studios",
        "broadcast",
    )
)

_MANGA_FIELDS = ",".join(
    (
        "id",
        "title",
        "main_picture",
        "alternative_titles",
        "start_date",
        "end_date",
        "synopsis",
        "mean",
        "rank",
        "popularity",
        "num_list_users",
        "num_scoring_users",
        "nsfw",
        "genres",
        "media_type",
        "status",
        "num_volumes",
        "num_chapters",
        "authors",
    )
)

# Fields for the one-title detail view. `statistics` is anime-only: manga
# returns null for it. Neither kind exposes external/streaming links.
_ANIME_DETAIL_FIELDS = ",".join((_ANIME_FIELDS, "related_anime", "recommendations", "statistics"))
_MANGA_DETAIL_FIELDS = ",".join(
    (_MANGA_FIELDS, "related_manga", "recommendations", "authors{first_name,last_name}")
)

# MAL status vocabulary -> AniList's.
_STATUS_FROM_MAL = {
    "finished_airing": "FINISHED",
    "currently_airing": "RELEASING",
    "not_yet_aired": "NOT_YET_RELEASED",
    "finished": "FINISHED",
    "currently_publishing": "RELEASING",
    "not_yet_published": "NOT_YET_RELEASED",
    "on_hiatus": "HIATUS",
    "discontinued": "CANCELLED",
}

# AniList media formats MAL has no equivalent for.
_FORMAT_UNSUPPORTED = {"TV_SHORT"}

# MAL's nsfw ratings: white is safe, gray and black are not.
_SFW = "white"


class MalError(RuntimeError):
    """MAL was unreachable or returned something unusable."""


class MalNotConfiguredError(MalError):
    """No client id was supplied, so MAL cannot be called at all."""


def unsupported_filters(filters: dict[str, Any], *, anime: bool) -> list[str]:
    """Name the requested filters MAL cannot honour, even locally."""
    dropped = []
    if filters.get("countryOfOrigin") is not None:
        dropped.append("country")
    if filters.get("format") in _FORMAT_UNSUPPORTED:
        dropped.append("format")
    if not anime and filters.get("season"):
        dropped.append("season")
    return dropped


class MalClient:
    """Minimal async wrapper over MAL's public anime and manga endpoints."""

    def __init__(self, client_id: str | None) -> None:
        self._client_id = client_id
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        """Whether a client id is available."""
        return bool(self._client_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        """Create the session lazily; aiohttp binds it to the running loop."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=_TIMEOUT,
                headers={
                    "X-MAL-CLIENT-ID": self._client_id or "",
                    "Accept": "application/json",
                },
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying session. Safe to call more than once."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def search(
        self,
        *,
        anime: bool,
        per_page: int = 25,
        **filters: Any,
    ) -> list[Media]:
        """Fetch candidates from the best endpoint, then narrow them locally."""
        if not self.configured:
            raise MalNotConfiguredError("MAL_CLIENT_ID is not set.")

        allow_nsfw = filters.get("isAdult") is not False
        path, params = _endpoint_for(anime=anime, filters=filters, allow_nsfw=allow_nsfw)
        payload = await self._get(path, params)

        results = [
            _parse_media(entry.get("node"), anime=anime)
            for entry in (payload.get("data") or [])
            if isinstance(entry, dict) and isinstance(entry.get("node"), dict)
        ]
        return _narrow(results, filters, allow_nsfw=allow_nsfw)[:per_page]

    async def get(self, media_id: int, *, anime: bool) -> Media:
        """Fetch exactly one title by id.

        Used when someone picks a suggestion from autocomplete: the id is
        already known, so there is nothing to search for or rank.
        """
        if not self.configured:
            raise MalNotConfiguredError("MAL_CLIENT_ID is not set.")

        kind = "anime" if anime else "manga"
        fields = _ANIME_FIELDS if anime else _MANGA_FIELDS
        payload = await self._get(f"{kind}/{media_id}", {"fields": fields})
        return _parse_media(payload, anime=anime)

    async def details(self, media_id: int, *, anime: bool) -> MediaDetails:
        """Fetch the second layer for one title: relations, recommendations."""
        if not self.configured:
            raise MalNotConfiguredError("MAL_CLIENT_ID is not set.")

        kind = "anime" if anime else "manga"
        fields = _ANIME_DETAIL_FIELDS if anime else _MANGA_DETAIL_FIELDS
        payload = await self._get(f"{kind}/{media_id}", {"fields": fields})
        return _parse_details(payload, anime=anime)

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Perform one GET and surface MAL's failure modes."""
        session = await self._get_session()

        try:
            async with session.get(f"{BASE_URL}/{path}", params=params) as r:
                status = r.status
                try:
                    payload = await r.json(content_type=None)
                except ValueError as exc:
                    raise MalError(f"MAL sent a non-JSON response (HTTP {status}).") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise MalError("Could not reach MyAnimeList.") from exc

        if status in (400, 401, 403) and _is_client_id_problem(payload):
            raise MalNotConfiguredError(
                "MyAnimeList rejected the client id. Check MAL_CLIENT_ID in your .env."
            )
        if status != 200 or not isinstance(payload, dict):
            raise MalError(f"MAL returned HTTP {status}: {_first_error(payload)}")

        return payload


def _endpoint_for(
    *, anime: bool, filters: dict[str, Any], allow_nsfw: bool
) -> tuple[str, dict[str, str]]:
    """Pick the most specific MAL endpoint the filters allow.

    MAL's ``q`` search cannot be combined with anything, and rejects a query
    shorter than three characters, so a season browse and an unfiltered browse
    each go somewhere better.
    """
    kind = "anime" if anime else "manga"
    fields = _ANIME_FIELDS if anime else _MANGA_FIELDS
    common = {
        "fields": fields,
        "limit": str(min(CANDIDATE_POOL, MAX_PER_PAGE)),
        "nsfw": "true" if allow_nsfw else "false",
    }

    search = str(filters.get("search") or "").strip()
    if len(search) >= MIN_QUERY:
        return kind, {**common, "q": search}

    season = str(filters.get("season") or "").lower()
    year = filters.get("seasonYear") or _year_of(filters.get("startDate_greater"))
    if anime and season and year:
        # limit is capped lower here than the ranking endpoints allow.
        return f"anime/season/{year}/{season}", {**common, "sort": "anime_num_list_users"}

    return f"{kind}/ranking", {**common, "ranking_type": "bypopularity"}


def _narrow(results: list[Media], filters: dict[str, Any], *, allow_nsfw: bool) -> list[Media]:
    """Apply every filter MAL could not, using fields already in the response."""
    genre = filters.get("genre")
    fmt = filters.get("format")
    status = filters.get("status")
    source = filters.get("source")
    season = filters.get("season")
    low, high = filters.get("startDate_greater"), filters.get("startDate_lesser")
    season_year = filters.get("seasonYear")

    kept = []
    for media in results:
        if not allow_nsfw and media.is_adult:
            continue
        if genre and not any(g.casefold() == str(genre).casefold() for g in media.genres):
            continue
        if fmt and fmt not in _FORMAT_UNSUPPORTED and media.format != fmt:
            continue
        if status and media.status != status:
            continue
        if source and media.source != source:
            continue
        if season and media.season and media.season != season:
            continue
        if season_year and media.season_year and media.season_year != season_year:
            continue
        if (low or high) and not _within(media.start_date, low, high):
            continue
        kept.append(media)
    return kept


def _within(date: FuzzyDate, low: Any, high: Any) -> bool:
    """Test a fuzzy date against AniList-style YYYYMMDD bounds."""
    if not date:
        return False
    stamp = date.year * 10000 + (date.month or 1) * 100 + (date.day or 1)
    try:
        if low is not None and stamp < int(low):
            return False
        if high is not None and stamp > int(high):
            return False
    except TypeError, ValueError:
        return True
    return True


def _is_client_id_problem(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    blob = f"{payload.get('error', '')} {payload.get('message', '')}".casefold()
    return "client" in blob or "forbidden" in blob or "unauthor" in blob


def _first_error(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or "no error detail given")
    return "unexpected response body"


def _year_of(fuzzy_date_int: Any) -> int | None:
    """Recover the year from an AniList FuzzyDateInt (YYYYMMDD)."""
    try:
        return int(fuzzy_date_int) // 10000 or None
    except TypeError, ValueError:
        return None


def _date(raw: Any) -> FuzzyDate:
    """Parse MAL's ``YYYY-MM-DD`` dates, which may be partial or absent."""
    if not isinstance(raw, str) or not raw:
        return FuzzyDate()
    parts = raw.split("-")
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return FuzzyDate()
    return FuzzyDate(*(nums + [None] * (3 - len(nums))))


def _score(raw: Any) -> int | None:
    """MAL scores out of 10; AniList and the embed use a percentage."""
    try:
        return round(float(raw) * 10)
    except TypeError, ValueError:
        return None


def _names(raw: Any) -> list[str]:
    """Pull ``name`` out of MAL's ``[{id, name}]`` lists."""
    if not isinstance(raw, list):
        return []
    return [str(e["name"]) for e in raw if isinstance(e, dict) and e.get("name")]


def _parse_media(node: dict[str, Any], *, anime: bool) -> Media:
    """Build a :class:`Media` from one MAL node."""
    alt = node.get("alternative_titles") or {}
    picture = node.get("main_picture") or {}
    start_season = node.get("start_season") or {}
    broadcast = node.get("broadcast") or {}

    # MAL reports episode length in seconds.
    seconds = node.get("average_episode_duration")
    duration = round(seconds / 60) if isinstance(seconds, int) and seconds else None

    mal_id = node.get("id") or 0

    return Media(
        id=int(mal_id),
        site_url=f"{SITE_URL}/{'anime' if anime else 'manga'}/{mal_id}",
        title_romaji=str(node.get("title") or ""),
        title_english=str(alt.get("en") or ""),
        title_native=str(alt.get("ja") or ""),
        description=str(node.get("synopsis") or "").strip(),
        format=str(node.get("media_type") or "").upper(),
        status=_STATUS_FROM_MAL.get(str(node.get("status") or "").lower(), ""),
        season=str(start_season.get("season") or "").upper(),
        season_year=start_season.get("year"),
        country="",
        source=str(node.get("source") or "").upper(),
        genres=_names(node.get("genres")),
        average_score=_score(node.get("mean")),
        mean_score=None,
        popularity=node.get("num_list_users"),
        favourites=None,
        is_adult=str(node.get("nsfw") or _SFW).lower() != _SFW,
        cover_url=str(picture.get("large") or picture.get("medium") or ""),
        cover_color="",
        banner_url="",
        start_date=_date(node.get("start_date")),
        end_date=_date(node.get("end_date")),
        # MAL v2 does not expose trailers.
        trailer_url="",
        provider="MyAnimeList",
        studios=_names(node.get("studios")),
        episodes=node.get("num_episodes"),
        duration=duration,
        # MAL publishes a weekly slot rather than a timestamp; the next
        # occurrence is projected at render time by ``Media.airing_at``.
        broadcast_day=str(broadcast.get("day_of_the_week") or ""),
        broadcast_time=str(broadcast.get("start_time") or ""),
        chapters=node.get("num_chapters"),
        volumes=node.get("num_volumes"),
    )


# How much of each list to keep. Embed fields cap at 1024 characters, and a
# details panel is meant to be skimmed rather than read.
_MAX_RELATIONS = 8
_MAX_RECOMMENDATIONS = 6
_MAX_STAFF = 4


def _related(raw: Any, *, anime: bool) -> list[Relation]:
    """Parse MAL's ``related_anime`` / ``related_manga`` edge list."""
    if not isinstance(raw, list):
        return []

    out = []
    for edge in raw[:_MAX_RELATIONS]:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node")
        if not isinstance(node, dict) or not node.get("title"):
            continue
        kind = str(edge.get("relation_type_formatted") or edge.get("relation_type") or "Related")
        out.append(
            Relation(
                kind=kind,
                title=str(node["title"]),
                url=f"{SITE_URL}/{'anime' if anime else 'manga'}/{node.get('id') or 0}",
            )
        )
    return out


def _recommended(raw: Any, *, anime: bool) -> list[Relation]:
    """Parse MAL's ``recommendations`` edge list."""
    if not isinstance(raw, list):
        return []

    out = []
    for edge in raw[:_MAX_RECOMMENDATIONS]:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node")
        if not isinstance(node, dict) or not node.get("title"):
            continue
        out.append(
            Relation(
                kind="Recommended",
                title=str(node["title"]),
                url=f"{SITE_URL}/{'anime' if anime else 'manga'}/{node.get('id') or 0}",
            )
        )
    return out


def _statistics(raw: Any) -> dict[str, int]:
    """Parse MAL's watch-status breakdown. Anime only; manga returns null."""
    if not isinstance(raw, dict):
        return {}

    status = raw.get("status")
    if not isinstance(status, dict):
        return {}

    out = {}
    for key, value in status.items():
        try:
            out[str(key)] = int(value)
        except TypeError, ValueError:
            continue
    return out


def _authors(raw: Any) -> list[str]:
    """Parse MAL's manga ``authors`` edge list into ``Name (Role)`` strings."""
    if not isinstance(raw, list):
        return []

    out = []
    for edge in raw[:_MAX_STAFF]:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node")
        if not isinstance(node, dict):
            continue
        name = " ".join(
            part for part in (node.get("first_name"), node.get("last_name")) if part
        ).strip()
        if not name:
            continue
        role = str(edge.get("role") or "").strip()
        out.append(f"{name} ({role})" if role else name)
    return out


def _parse_details(node: dict[str, Any], *, anime: bool) -> MediaDetails:
    """Build a :class:`MediaDetails` from one MAL detail response.

    MAL exposes no external links, so :attr:`MediaDetails.links` is always
    empty here — only AniList fills it.
    """
    related = node.get("related_anime") if anime else node.get("related_manga")

    return MediaDetails(
        relations=_related(related, anime=anime),
        recommendations=_recommended(node.get("recommendations"), anime=anime),
        links=[],
        stats=_statistics(node.get("statistics")) if anime else {},
        staff=[] if anime else _authors(node.get("authors")),
    )

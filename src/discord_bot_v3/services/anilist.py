"""Client for the AniList GraphQL API (https://docs.anilist.co).

Public and unauthenticated: one POST endpoint, query plus variables.

Note on access: AniList currently answers some clients with HTTP 403 and
"The AniList API has been temporarily disabled due to severe stability
issues." The block is lifted by sending ``Referer: https://anilist.co``, which
would misrepresent this bot as their own website, so it is deliberately not
sent. :class:`AniListUnavailableError` reports the refusal instead.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import aiohttp

from .media import FuzzyDate, Link, Media, MediaDetails, Relation

_log = logging.getLogger(__name__)

API_URL = "https://graphql.anilist.co"

# AniList allows up to 50 per page.
MAX_PER_PAGE = 50

_TIMEOUT = aiohttp.ClientTimeout(total=15)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Discord-Bot-V3 (+https://github.com/PCloud-Bernard)",
}

# Descriptions come back as HTML even with asHtml:false — <br>, <i>, <b>.
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")


class AniListError(RuntimeError):
    """AniList was unreachable or returned something unusable."""


class AniListUnavailableError(AniListError):
    """AniList is deliberately refusing API requests (HTTP 403)."""


class AniListRateLimitedError(AniListError):
    """AniList rate limit hit (HTTP 429)."""

    def __init__(self, retry_after: int | None) -> None:
        self.retry_after = retry_after
        wait = f" Try again in {retry_after}s." if retry_after else ""
        super().__init__(f"AniList is rate limiting requests.{wait}")


# Fields shared by both media types. `description(asHtml: false)` still returns
# some markup, so it is stripped client-side.
_COMMON_FIELDS = """
    id
    siteUrl
    format
    status(version: 2)
    description(asHtml: false)
    season
    seasonYear
    countryOfOrigin
    source(version: 3)
    genres
    averageScore
    meanScore
    popularity
    favourites
    isAdult
    title { romaji english native }
    startDate { year month day }
    endDate { year month day }
    trailer { id site }
    coverImage { extraLarge large color }
    bannerImage
"""

_ANIME_QUERY = """
query ($page: Int, $perPage: Int, $search: String, $format: MediaFormat,
       $status: MediaStatus, $countryOfOrigin: CountryCode, $isAdult: Boolean,
       $genre: String, $source: MediaSource, $season: MediaSeason,
       $seasonYear: Int, $startDate_greater: FuzzyDateInt,
       $startDate_lesser: FuzzyDateInt, $sort: [MediaSort]) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { total currentPage hasNextPage }
    media(type: ANIME, sort: $sort, search: $search, format: $format,
          status: $status, countryOfOrigin: $countryOfOrigin, isAdult: $isAdult,
          genre: $genre, source: $source, season: $season, seasonYear: $seasonYear,
          startDate_greater: $startDate_greater, startDate_lesser: $startDate_lesser) {
      __COMMON__
      episodes
      duration
      nextAiringEpisode { episode airingAt }
      studios(isMain: true) { nodes { name } }
    }
  }
}
""".replace("__COMMON__", _COMMON_FIELDS)

_MANGA_QUERY = """
query ($page: Int, $perPage: Int, $search: String, $format: MediaFormat,
       $status: MediaStatus, $countryOfOrigin: CountryCode, $isAdult: Boolean,
       $genre: String, $source: MediaSource, $startDate_greater: FuzzyDateInt,
       $startDate_lesser: FuzzyDateInt, $sort: [MediaSort]) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { total currentPage hasNextPage }
    media(type: MANGA, sort: $sort, search: $search, format: $format,
          status: $status, countryOfOrigin: $countryOfOrigin, isAdult: $isAdult,
          genre: $genre, source: $source,
          startDate_greater: $startDate_greater, startDate_lesser: $startDate_lesser) {
      __COMMON__
      chapters
      volumes
    }
  }
}
""".replace("__COMMON__", _COMMON_FIELDS)


_ONE_QUERY = """
query ($id: Int, $type: MediaType) {
  Media(id: $id, type: $type) {
    __COMMON__
    episodes
    duration
    nextAiringEpisode { episode airingAt }
    studios(isMain: true) { nodes { name } }
    chapters
    volumes
  }
}
""".replace("__COMMON__", _COMMON_FIELDS)

# The second layer, fetched only when someone presses Details.
_DETAILS_QUERY = """
query ($id: Int, $type: MediaType) {
  Media(id: $id, type: $type) {
    relations {
      edges {
        relationType(version: 2)
        node { id title { romaji english } siteUrl }
      }
    }
    recommendations(sort: RATING_DESC, perPage: 6) {
      nodes { mediaRecommendation { title { romaji english } siteUrl } }
    }
    externalLinks { site url }
    stats { statusDistribution { status amount } }
    staff(perPage: 4, sort: RELEVANCE) {
      edges { role node { name { full } } }
    }
  }
}
"""

# How much of each list to keep; an embed field caps at 1024 characters.
_MAX_RELATIONS = 8
_MAX_RECOMMENDATIONS = 6
# AniList lists dozens of regional links; the first few are the useful ones.
_MAX_LINKS = 6


class AniListClient:
    """Minimal async wrapper over the two searches this bot performs."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Create the session lazily; aiohttp binds it to the running loop."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS)
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
        """Run one search. Unset filters are dropped so AniList ignores them."""
        # SEARCH_MATCH ranks by how well the title matches; without a search
        # term it means nothing, so an unfiltered browse sorts by popularity.
        variables: dict[str, Any] = {
            "page": 1,
            "perPage": min(max(per_page, 1), MAX_PER_PAGE),
            "sort": ["SEARCH_MATCH"] if filters.get("search") else ["POPULARITY_DESC"],
        }
        variables.update({k: v for k, v in filters.items() if v is not None})

        payload = await self._post(_ANIME_QUERY if anime else _MANGA_QUERY, variables)
        page = (payload.get("data") or {}).get("Page") or {}
        return [_parse_media(item) for item in (page.get("media") or []) if isinstance(item, dict)]

    async def get(self, media_id: int, *, anime: bool) -> Media:
        """Fetch exactly one title by id, for an autocomplete pick."""
        payload = await self._post(
            _ONE_QUERY, {"id": media_id, "type": "ANIME" if anime else "MANGA"}
        )
        item = (payload.get("data") or {}).get("Media")
        if not isinstance(item, dict):
            raise AniListError(f"AniList has no media with id {media_id}.")
        return _parse_media(item)

    async def details(self, media_id: int, *, anime: bool) -> MediaDetails:
        """Fetch the second layer: relations, recommendations, streaming links."""
        payload = await self._post(
            _DETAILS_QUERY, {"id": media_id, "type": "ANIME" if anime else "MANGA"}
        )
        item = (payload.get("data") or {}).get("Media")
        if not isinstance(item, dict):
            raise AniListError(f"AniList has no media with id {media_id}.")
        return _parse_details(item)

    async def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Perform one GraphQL POST and surface AniList's failure modes."""
        session = await self._get_session()

        try:
            async with session.post(API_URL, json={"query": query, "variables": variables}) as r:
                status = r.status
                retry_after = r.headers.get("Retry-After")
                try:
                    payload = await r.json(content_type=None)
                except ValueError as exc:
                    raise AniListError(
                        f"AniList sent a non-JSON response (HTTP {status})."
                    ) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise AniListError("Could not reach AniList.") from exc

        if status == 403:
            raise AniListUnavailableError(_first_error(payload) or "AniList has disabled its API.")
        if status == 429:
            raise AniListRateLimitedError(int(retry_after) if retry_after else None)

        # GraphQL reports query-level problems with HTTP 200 and an errors list.
        if not isinstance(payload, dict) or payload.get("errors"):
            raise AniListError(_first_error(payload) or f"AniList returned HTTP {status}.")
        if status != 200:
            raise AniListError(f"AniList returned HTTP {status}.")

        return payload


def _first_error(payload: Any) -> str:
    """Pull the first human-readable message out of a GraphQL error body."""
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "")
    return ""


def clean_description(raw: str | None) -> str:
    """Turn AniList's semi-HTML synopsis into plain text."""
    if not raw:
        return ""
    text = _BR.sub("\n", raw)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def _fuzzy(raw: Any) -> FuzzyDate:
    if not isinstance(raw, dict):
        return FuzzyDate()
    return FuzzyDate(raw.get("year"), raw.get("month"), raw.get("day"))


def _trailer_url(raw: Any) -> str:
    """Build a watchable URL from AniList's {id, site} trailer stub."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return ""
    site, tid = raw.get("site"), raw["id"]
    if site == "youtube":
        return f"https://www.youtube.com/watch?v={tid}"
    if site == "dailymotion":
        return f"https://www.dailymotion.com/video/{tid}"
    return ""


def _parse_media(item: dict[str, Any]) -> Media:
    """Build a :class:`Media` from one AniList node, tolerating nulls."""
    title = item.get("title") or {}
    cover = item.get("coverImage") or {}
    studios = ((item.get("studios") or {}).get("nodes")) or []
    airing = item.get("nextAiringEpisode") or {}

    return Media(
        id=int(item.get("id") or 0),
        site_url=str(item.get("siteUrl") or ""),
        title_romaji=str(title.get("romaji") or ""),
        title_english=str(title.get("english") or ""),
        title_native=str(title.get("native") or ""),
        description=clean_description(item.get("description")),
        format=str(item.get("format") or ""),
        status=str(item.get("status") or ""),
        season=str(item.get("season") or ""),
        season_year=item.get("seasonYear"),
        country=str(item.get("countryOfOrigin") or ""),
        source=str(item.get("source") or ""),
        genres=[str(g) for g in (item.get("genres") or [])],
        average_score=item.get("averageScore"),
        mean_score=item.get("meanScore"),
        popularity=item.get("popularity"),
        favourites=item.get("favourites"),
        is_adult=bool(item.get("isAdult")),
        cover_url=str(cover.get("extraLarge") or cover.get("large") or ""),
        cover_color=str(cover.get("color") or ""),
        banner_url=str(item.get("bannerImage") or ""),
        start_date=_fuzzy(item.get("startDate")),
        end_date=_fuzzy(item.get("endDate")),
        trailer_url=_trailer_url(item.get("trailer")),
        studios=[str(n.get("name")) for n in studios if isinstance(n, dict) and n.get("name")],
        provider="AniList",
        episodes=item.get("episodes"),
        duration=item.get("duration"),
        next_episode=airing.get("episode"),
        next_airing_at=airing.get("airingAt"),
        chapters=item.get("chapters"),
        volumes=item.get("volumes"),
    )


def _best_title(raw: Any) -> str:
    """Pick a display title out of an AniList ``title`` object."""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("english") or raw.get("romaji") or "")


def _parse_details(item: dict[str, Any]) -> MediaDetails:
    """Build a :class:`MediaDetails` from one AniList node.

    Untested against the live API: AniList has been returning 403 to this
    client throughout development, so this path has only been exercised through
    its parser. It is written against the documented schema and every lookup
    tolerates a missing branch, so a shape surprise degrades to an empty
    section rather than an exception.
    """
    relations = []
    for edge in ((item.get("relations") or {}).get("edges") or [])[:_MAX_RELATIONS]:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node")
        if not isinstance(node, dict):
            continue
        title = _best_title(node.get("title"))
        if not title:
            continue
        relations.append(
            Relation(
                kind=_pretty_relation(edge.get("relationType")),
                title=title,
                url=str(node.get("siteUrl") or ""),
            )
        )

    recommendations = []
    for node in ((item.get("recommendations") or {}).get("nodes") or [])[:_MAX_RECOMMENDATIONS]:
        if not isinstance(node, dict):
            continue
        rec = node.get("mediaRecommendation")
        if not isinstance(rec, dict):
            continue
        title = _best_title(rec.get("title"))
        if not title:
            continue
        recommendations.append(
            Relation(kind="Recommended", title=title, url=str(rec.get("siteUrl") or ""))
        )

    links = [
        Link(name=str(raw.get("site") or "Link"), url=str(raw["url"]))
        for raw in (item.get("externalLinks") or [])[:_MAX_LINKS]
        if isinstance(raw, dict) and raw.get("url")
    ]

    stats = {}
    distribution = (item.get("stats") or {}).get("statusDistribution") or []
    for entry in distribution:
        if not isinstance(entry, dict) or entry.get("status") is None:
            continue
        try:
            stats[str(entry["status"]).lower()] = int(entry.get("amount") or 0)
        except TypeError, ValueError:
            continue

    staff = []
    for edge in (item.get("staff") or {}).get("edges") or []:
        if not isinstance(edge, dict):
            continue
        name = ((edge.get("node") or {}).get("name") or {}).get("full")
        if not name:
            continue
        role = str(edge.get("role") or "").strip()
        staff.append(f"{name} ({role})" if role else str(name))

    return MediaDetails(
        relations=relations,
        recommendations=recommendations,
        links=links,
        stats=stats,
        staff=staff,
    )


def _pretty_relation(raw: Any) -> str:
    """``SIDE_STORY`` -> ``Side story``."""
    if not raw:
        return "Related"
    return str(raw).replace("_", " ").capitalize()

"""Client for the KLIPY GIF API.

Docs: https://docs.klipy.com/gifs-api — KLIPY replaces Tenor, whose public API
was retired. The shape differs from Tenor in two ways worth knowing:

* The API key is a **path segment**, not a query parameter:
  ``/api/v1/{api_key}/gifs/search``. Keep it out of logs.
* Every response is wrapped in a ``{"result": bool, "data": ...}`` envelope,
  and the list lives at ``data.data`` — one level deeper than it looks.
* An invalid key comes back as HTTP 404, not 401.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

_log = logging.getLogger(__name__)

BASE_URL = "https://api.klipy.com/api/v1"

# KLIPY clamps per_page to this range; sending more just wastes the call.
MIN_PER_PAGE = 8
MAX_PER_PAGE = 50

# Content filter strengths KLIPY accepts, strictest first.
CONTENT_FILTERS = ("high", "medium", "low", "off")

# Quality tiers to try, in order. Discord fetches and renders whatever URL we
# post, so "md" is the sweet spot: sharper than "sm", far lighter than "hd".
_QUALITY_ORDER = ("md", "hd", "sm", "xs")

_TIMEOUT = aiohttp.ClientTimeout(total=10)


class KlipyError(RuntimeError):
    """KLIPY was unreachable, refused the request, or sent something unusable."""


@dataclass(frozen=True, slots=True)
class Gif:
    """A single GIF result, reduced to the parts we actually post."""

    url: str
    title: str
    slug: str


class KlipyClient:
    """Minimal async wrapper over the endpoints this bot uses."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared session, creating it on first use.

        Created lazily because aiohttp binds a session to the running event
        loop, which does not exist yet when cogs are constructed at startup.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self) -> None:
        """Close the underlying session. Safe to call more than once."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def search(
        self,
        query: str,
        *,
        per_page: int = 24,
        content_filter: str = "high",
        customer_id: str | None = None,
    ) -> list[Gif]:
        """Search for GIFs matching ``query``.

        Returns an empty list when KLIPY has no matches — only transport and
        protocol failures raise ``KlipyError``.
        """
        params = {
            "q": query,
            "page": "1",
            "per_page": str(min(max(per_page, MIN_PER_PAGE), MAX_PER_PAGE)),
            # Ask for animated GIFs specifically; KLIPY also serves mp4/webp.
            "format_filter": "gif",
            "content_filter": content_filter,
        }
        if customer_id:
            params["customer_id"] = customer_id

        payload = await self._get("gifs/search", params)
        items = (payload.get("data") or {}).get("data") or []
        return [gif for gif in map(_parse_gif, items) if gif is not None]

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Perform one GET and unwrap KLIPY's result envelope."""
        session = await self._get_session()
        url = f"{BASE_URL}/{self._api_key}/{path}"

        try:
            async with session.get(url, params=params) as response:
                # Errors are reported in the body, so read it either way; the
                # API is not consistent about sending a JSON content type.
                status = response.status
                try:
                    payload = await response.json(content_type=None)
                except ValueError as exc:
                    # Gateway errors and rate limit pages arrive as HTML.
                    raise KlipyError(f"KLIPY sent a non-JSON response (HTTP {status}).") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            # Never interpolate `url` — it carries the API key.
            raise KlipyError(f"Could not reach KLIPY ({path}).") from exc

        if status != 200 or not isinstance(payload, dict) or not payload.get("result"):
            raise KlipyError(f"KLIPY returned HTTP {status}: {_describe_errors(payload)}")

        return payload


def _describe_errors(payload: Any) -> str:
    """Flatten KLIPY's ``errors`` object into one readable line."""
    if not isinstance(payload, dict):
        return "unexpected response body"

    errors = payload.get("errors")
    if isinstance(errors, dict):
        messages = [
            str(item)
            for value in errors.values()
            for item in (value if isinstance(value, list) else [value])
        ]
        if messages:
            return "; ".join(messages)
    elif isinstance(errors, str) and errors:
        return errors

    return "no error detail given"


def _parse_gif(item: Any) -> Gif | None:
    """Build a ``Gif`` from one result, or None if it carries no GIF file."""
    if not isinstance(item, dict):
        return None

    url = _extract_gif_url(item)
    if not url:
        return None

    return Gif(
        url=url,
        title=str(item.get("title") or "").strip(),
        slug=str(item.get("slug") or "").strip(),
    )


def _extract_gif_url(item: dict[str, Any]) -> str | None:
    """Pick the best available GIF URL out of a result's media tree.

    Confirmed against the live API: a result is
    ``{"id", "slug", "title", "type", "tags", "blur_preview", "file"}`` and
    ``file`` is ``{quality: {format: {"url", "width", "height", "size"}}}``,
    with the qualities in :data:`_QUALITY_ORDER`. Only the formats allowed by
    ``format_filter`` are present, so with ``format_filter=gif`` each quality
    holds a single ``gif`` entry.
    """
    media = item.get("file") or {}
    if not isinstance(media, dict):
        return None

    for quality in _QUALITY_ORDER:
        variant = media.get(quality)
        if not isinstance(variant, dict):
            continue
        gif = variant.get("gif")
        if isinstance(gif, dict) and gif.get("url"):
            return str(gif["url"])

    _log.debug("No gif variant in result; media keys=%s", list(media))
    return None

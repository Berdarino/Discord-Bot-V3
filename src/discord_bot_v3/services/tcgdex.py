"""Client for TCGdex, covering the Pokémon TCG Pocket series.

Docs: https://tcgdex.dev — public, unauthenticated REST. The official Python
SDK is not used: it is another dependency for what is three GET shapes, and
this project already speaks aiohttp everywhere else.

The set list is **cached in memory**, because slash-command autocomplete has to
answer inside Discord's 3 second window and cannot afford a round trip. Full
card details are fetched on demand instead of cached, since the series holds
roughly 2,400 cards and only a handful are ever looked at.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import asdict, dataclass, field
from typing import Any

import aiohttp

from .cache import Cache

_log = logging.getLogger(__name__)

BASE_URL = "https://api.tcgdex.net/v2/en"

# The Pokémon TCG Pocket series.
SERIES_ID = "tcgp"

# Politeness limit for the fan-out during a refresh.
_CONCURRENCY = 4

_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Where the parsed set list is mirrored, so a restart does not start cold.
# Slightly longer than the refresh interval, so the loop is what expires it.
CACHE_KEY = "tcgdex:tcgp:sets"
CACHE_TTL = 60 * 60 * 26

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Discord-Bot-V3 (+https://github.com/PCloud-Bernard)",
}


class TcgdexError(RuntimeError):
    """TCGdex was unreachable or returned something unusable."""


def asset_url(base: str, *, quality: str = "high", extension: str = "png") -> str:
    """Build a usable image URL.

    TCGdex hands out *stems* like ``.../A1/001``; the quality and extension are
    appended by the caller. Set logos and symbols take only the extension.
    """
    if not base:
        return ""
    suffix = f"/{quality}.{extension}" if quality else f".{extension}"
    return f"{base}{suffix}"


@dataclass(frozen=True, slots=True)
class CardStub:
    """A card as it appears in a set listing: enough to browse and pick."""

    id: str
    local_id: str
    name: str
    image: str
    set_id: str = ""
    set_name: str = ""

    @property
    def image_url(self) -> str:
        return asset_url(self.image)


@dataclass(frozen=True, slots=True)
class Attack:
    """One attack line on a card."""

    name: str
    cost: list[str]
    damage: str
    effect: str


@dataclass(frozen=True, slots=True)
class PokemonSet:
    """One TCG Pocket set, with its card listing."""

    id: str
    name: str
    logo: str
    symbol: str
    release_date: str
    total: int
    official: int
    series_name: str
    cards: list[CardStub] = field(default_factory=list)
    # TCG Pocket splits a set across themed packs, e.g. A1 opens as
    # Mewtwo / Charizard / Pikachu.
    boosters: list[str] = field(default_factory=list)

    @property
    def logo_url(self) -> str:
        return asset_url(self.logo, quality="", extension="png")

    @property
    def symbol_url(self) -> str:
        return asset_url(self.symbol, quality="", extension="png")


@dataclass(frozen=True, slots=True)
class PokemonCard:
    """A fully detailed card."""

    id: str
    local_id: str
    name: str
    image: str
    category: str
    rarity: str
    illustrator: str
    set_id: str
    set_name: str
    hp: int | None = None
    types: list[str] = field(default_factory=list)
    stage: str = ""
    description: str = ""
    effect: str = ""
    trainer_type: str = ""
    retreat: int | None = None
    dex_ids: list[int] = field(default_factory=list)
    attacks: list[Attack] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    # Which packs this card can actually be pulled from.
    boosters: list[str] = field(default_factory=list)

    @property
    def image_url(self) -> str:
        return asset_url(self.image)


class TcgdexClient:
    """Cached access to the TCG Pocket series."""

    def __init__(self, cache: Cache | None = None) -> None:
        self._cache = cache
        self._session: aiohttp.ClientSession | None = None
        self._sets: dict[str, PokemonSet] = {}
        # card id -> set id, so `/pokemon cards id` need not scan every set.
        self._card_sets: dict[str, str] = {}
        # Guards against two commands refreshing at once on a cold cache.
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        """Whether the set cache holds anything yet."""
        return bool(self._sets)

    @property
    def sets(self) -> list[PokemonSet]:
        """Cached sets, oldest release first."""
        return sorted(self._sets.values(), key=lambda s: (s.release_date or "", s.id))

    @property
    def card_count(self) -> int:
        """How many cards the cache knows about."""
        return len(self._card_sets)

    def get_set(self, set_id: str) -> PokemonSet | None:
        """Look a set up by id, case-insensitively."""
        if set_id in self._sets:
            return self._sets[set_id]
        wanted = set_id.casefold()
        return next((s for s in self._sets.values() if s.id.casefold() == wanted), None)

    def find_stub(self, card_id: str) -> CardStub | None:
        """Find a cached card by id, forgiving case and un-padded numbers.

        TCGdex itself is case-insensitive but strict about width: ``A1-001``
        resolves and ``A1-1`` is a 404. People type the short form, so match
        the numeric part by value rather than by string.
        """
        wanted = card_id.strip().casefold()
        if not wanted:
            return None

        for stub in self._all_stubs():
            if stub.id.casefold() == wanted:
                return stub

        # Fall back to comparing set and number separately, so "a1-1" finds
        # "A1-001".
        set_part, _, num_part = wanted.rpartition("-")
        if not set_part or not num_part.isdigit():
            return None

        number = int(num_part)
        for stub in self._all_stubs():
            if (
                stub.set_id.casefold() == set_part
                and stub.local_id.isdigit()
                and int(stub.local_id) == number
            ):
                return stub
        return None

    def search(self, name: str, *, limit: int = 100) -> list[CardStub]:
        """Find cards by name across every cached set.

        Served from the cache rather than TCGdex's own ``name=like:`` filter,
        which has no working series filter and so searches the entire Pokémon
        TCG catalogue — "pikachu" returns 200+ cards from sets this bot does
        not cover.
        """
        wanted = name.strip().casefold()
        if not wanted:
            return []

        hits = [c for c in self._all_stubs() if wanted in c.name.casefold()]
        # Exact names first, then alphabetical, then by id for stability.
        hits.sort(key=lambda c: (c.name.casefold() != wanted, c.name.casefold(), c.id))
        return hits[:limit]

    def random_stub(self) -> CardStub | None:
        """One cached card at random, or None on a cold cache."""
        stubs = self._all_stubs()
        return random.choice(stubs) if stubs else None

    def _all_stubs(self) -> list[CardStub]:
        """Every cached card, in set order."""
        return [c for s in self.sets for c in s.cards]

    async def _get_session(self) -> aiohttp.ClientSession:
        """Create the session lazily; aiohttp binds it to the running loop."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS)
        return self._session

    async def close(self) -> None:
        """Close the underlying session. Safe to call more than once."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def ensure_loaded(self) -> None:
        """Populate the in-memory cache, from Redis if possible.

        A cold start otherwise costs 16 requests before autocomplete answers
        anything, on every restart and every deploy.
        """
        if self.loaded:
            return
        if await self.load_cached():
            return
        await self.refresh()

    async def load_cached(self) -> bool:
        """Rebuild from Redis. Returns whether anything was restored."""
        if self._cache is None:
            return False

        payload = await self._cache.get(CACHE_KEY)
        if not isinstance(payload, list) or not payload:
            return False

        try:
            found = [_set_from_cache(entry) for entry in payload]
        except KeyError, TypeError, ValueError:
            _log.warning("Discarding an unreadable cached set list")
            return False

        self._sets = {s.id: s for s in found}
        self._card_sets = {c.id: s.id for s in found for c in s.cards}
        _log.info("Restored %d sets, %d cards from Redis", len(found), len(self._card_sets))
        return True

    async def _store_cached(self) -> None:
        """Mirror the current set list into Redis."""
        if self._cache is None:
            return
        await self._cache.set(CACHE_KEY, [asdict(s) for s in self.sets], ttl=CACHE_TTL)

    async def refresh(self) -> int:
        """Rebuild the whole cache. Returns the number of sets loaded."""
        async with self._lock:
            series = await self._get(f"series/{SERIES_ID}")
            stubs = series.get("sets") or []
            if not stubs:
                raise TcgdexError("TCGdex returned no sets for the TCG Pocket series.")

            series_name = str(series.get("name") or "")
            semaphore = asyncio.Semaphore(_CONCURRENCY)

            async def load(stub: dict[str, Any]) -> PokemonSet | None:
                async with semaphore:
                    try:
                        return _parse_set(await self._get(f"sets/{stub['id']}"), series_name)
                    except TcgdexError, KeyError:
                        _log.exception("Could not load set %s", stub.get("id"))
                        return None

            loaded = await asyncio.gather(*(load(s) for s in stubs if s.get("id")))
            found = [s for s in loaded if s is not None]
            if not found:
                raise TcgdexError("Could not load any TCG Pocket sets.")

            # Merge rather than replace. A transient failure on a few sets must
            # not silently shrink the cache and make their cards unfindable
            # until the next refresh.
            refreshed = {s.id: s for s in found}
            missed = sorted(set(self._sets) - set(refreshed))
            if missed:
                _log.warning("Kept cached copies of sets that failed to reload: %s", missed)
            self._sets = {**self._sets, **refreshed}
            self._card_sets = {c.id: s.id for s in self._sets.values() for c in s.cards}

            _log.info("Cached %d TCG Pocket sets, %d cards", len(self._sets), len(self._card_sets))

        # Outside the lock: mirroring to Redis must not hold up another caller.
        await self._store_cached()
        return len(self._sets)

    async def get_card(self, card_id: str) -> PokemonCard | None:
        """Fetch one card's full detail. None when TCGdex has no such card."""
        try:
            payload = await self._get(f"cards/{card_id}")
        except TcgdexError as exc:
            if "404" in str(exc):
                return None
            raise
        return _parse_card(payload)

    async def _get(self, path: str) -> dict[str, Any]:
        """Perform one GET and surface TCGdex's failure modes."""
        session = await self._get_session()

        try:
            async with session.get(f"{BASE_URL}/{path}") as r:
                status = r.status
                try:
                    payload = await r.json(content_type=None)
                except ValueError as exc:
                    raise TcgdexError(f"TCGdex sent a non-JSON response (HTTP {status}).") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TcgdexError(f"Could not reach TCGdex ({path}).") from exc

        if status != 200 or not isinstance(payload, dict):
            raise TcgdexError(f"TCGdex returned HTTP {status} for {path}.")

        return payload


def _named(raw: Any) -> list[str]:
    """Pull ``name`` out of TCGdex's ``[{id, name}]`` lists."""
    if not isinstance(raw, list):
        return []
    return [str(e["name"]) for e in raw if isinstance(e, dict) and e.get("name")]


def _set_from_cache(entry: dict[str, Any]) -> PokemonSet:
    """Rebuild a set from its Redis mirror, written by ``dataclasses.asdict``."""
    cards = [CardStub(**c) for c in entry.get("cards") or []]
    return PokemonSet(**{**entry, "cards": cards})


def _parse_set(payload: dict[str, Any], series_name: str) -> PokemonSet:
    """Build a :class:`PokemonSet` from a full set response."""
    counts = payload.get("cardCount") or {}
    serie = payload.get("serie") or {}
    return PokemonSet(
        id=str(payload.get("id") or ""),
        name=str(payload.get("name") or ""),
        logo=str(payload.get("logo") or ""),
        symbol=str(payload.get("symbol") or ""),
        release_date=str(payload.get("releaseDate") or ""),
        total=int(counts.get("total") or 0),
        official=int(counts.get("official") or 0),
        series_name=str(serie.get("name") or series_name),
        boosters=_named(payload.get("boosters")),
        cards=[
            CardStub(
                id=str(c.get("id") or ""),
                local_id=str(c.get("localId") or ""),
                name=str(c.get("name") or ""),
                image=str(c.get("image") or ""),
                set_id=str(payload.get("id") or ""),
                set_name=str(payload.get("name") or ""),
            )
            for c in (payload.get("cards") or [])
            if isinstance(c, dict) and c.get("id")
        ],
    )


def _parse_attacks(raw: Any) -> list[Attack]:
    if not isinstance(raw, list):
        return []
    return [
        Attack(
            name=str(a.get("name") or ""),
            cost=[str(c) for c in (a.get("cost") or [])],
            damage=str(a.get("damage") or ""),
            effect=str(a.get("effect") or ""),
        )
        for a in raw
        if isinstance(a, dict)
    ]


def _parse_weaknesses(raw: Any) -> list[str]:
    """Flatten ``[{type, value}]`` into ``["Fire x2"]``."""
    if not isinstance(raw, list):
        return []
    out = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        kind, value = str(w.get("type") or ""), str(w.get("value") or "")
        if kind:
            out.append(f"{kind} {value}".strip())
    return out


def _parse_card(payload: dict[str, Any]) -> PokemonCard:
    """Build a :class:`PokemonCard` from a full card response."""
    card_set = payload.get("set") or {}
    return PokemonCard(
        id=str(payload.get("id") or ""),
        local_id=str(payload.get("localId") or ""),
        name=str(payload.get("name") or ""),
        image=str(payload.get("image") or ""),
        category=str(payload.get("category") or ""),
        rarity=str(payload.get("rarity") or ""),
        illustrator=str(payload.get("illustrator") or ""),
        set_id=str(card_set.get("id") or ""),
        set_name=str(card_set.get("name") or ""),
        hp=payload.get("hp"),
        types=[str(t) for t in (payload.get("types") or [])],
        stage=str(payload.get("stage") or ""),
        description=str(payload.get("description") or ""),
        effect=str(payload.get("effect") or ""),
        trainer_type=str(payload.get("trainerType") or ""),
        retreat=payload.get("retreat"),
        dex_ids=[int(d) for d in (payload.get("dexId") or []) if isinstance(d, int)],
        attacks=_parse_attacks(payload.get("attacks")),
        weaknesses=_parse_weaknesses(payload.get("weaknesses")),
        boosters=_named(payload.get("boosters")),
    )

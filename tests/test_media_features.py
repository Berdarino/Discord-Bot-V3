"""The four changes: rerank, autocomplete, circuit breaker, details view."""

import asyncio
import types

import discord

from discord_bot_v3.cogs.media import (
    _ANILIST_DOWN_KEY,
    _PICKED,
    DetailsView,
    MediaSearch,
    _build_embed,
    _cache_key,
    _details_embed,
    _suggest,
)
from discord_bot_v3.config import Config
from discord_bot_v3.services.anilist import AniListRateLimitedError, AniListUnavailableError
from discord_bot_v3.services.cache import Cache
from discord_bot_v3.services.media import FuzzyDate, Link, Media, MediaDetails, Relation


def media(**kw):
    base = dict(
        id=1,
        site_url="https://x",
        title_romaji="R",
        title_english="E",
        title_native="N",
        description="d",
        format="TV",
        status="FINISHED",
        season="SPRING",
        season_year=2024,
        country="JP",
        source="MANGA",
        genres=["Action"],
        average_score=80,
        mean_score=80,
        popularity=100,
        favourites=1,
        is_adult=False,
        cover_url="",
        cover_color="",
        banner_url="",
        start_date=FuzzyDate(2024, 1, 1),
        end_date=FuzzyDate(2024, 3, 1),
        trailer_url="",
        provider="MyAnimeList",
    )
    base.update(kw)
    return Media(**base)


class Bot:
    def __init__(self, cache):
        self.loop = asyncio.get_event_loop()
        self.config = Config.from_env()
        self.cache = cache


async def main():
    cache = Cache(Config.from_env().redis_url)
    assert await cache.connect()
    cog = MediaSearch(Bot(cache))
    try:
        # ---------------------------------------------------------------
        print("== circuit breaker ==")
        await cache.delete(_ANILIST_DOWN_KEY)
        # Clear both key variants for every probe query: a hit left by an
        # earlier run would skip the fetch and make the call counts meaningless.
        for q in ("breaker-probe-1", "breaker-probe-2", "breaker-probe-3"):
            await cache.delete(
                *(_cache_key(anime=True, filters={"search": q}, degraded=d) for d in (False, True))
            )
        assert not await cog._anilist_is_down()

        hits = {"anilist": 0, "mal": 0}

        async def dead_anilist(**kw):
            hits["anilist"] += 1
            raise AniListUnavailableError("disabled")

        async def fake_mal(**kw):
            hits["mal"] += 1
            return [media(title_english="X")]

        cog.anilist.search = dead_anilist
        cog.mal.search = fake_mal

        f = {"search": "breaker-probe-1"}
        await cog.search(anime=True, filters=dict(f))
        assert hits == {"anilist": 1, "mal": 1}, hits
        assert await cog._anilist_is_down(), "403 must open the breaker"

        # A different query, so it is a genuine fetch and not a cache hit.
        await cog.search(anime=True, filters={"search": "breaker-probe-2"})
        assert hits["anilist"] == 1, "breaker must skip AniList entirely"
        assert hits["mal"] == 2, hits
        print(f"   403 opened breaker; AniList called {hits['anilist']}x over 2 searches")

        # A rate limit is a backoff, not an outage: it must NOT open the breaker
        # and must reach the caller rather than silently switching provider.
        await cache.delete(_ANILIST_DOWN_KEY)

        async def limited(**kw):
            raise AniListRateLimitedError(30)

        cog.anilist.search = limited
        try:
            await cog.search(anime=True, filters={"search": "breaker-probe-3"})
            raise SystemExit("rate limit should have propagated")
        except AniListRateLimitedError:
            pass
        assert not await cog._anilist_is_down(), "429 must not open the breaker"
        print("   429 propagated and left the breaker closed")

        # ---------------------------------------------------------------
        print("\n== cache key tracks the answering provider ==")
        a = _cache_key(anime=True, filters={"search": "x"}, degraded=False)
        b = _cache_key(anime=True, filters={"search": "x"}, degraded=True)
        assert a != b, "degraded results must not be served as healthy ones"
        print(f"   {a}\n   {b}")

        # ---------------------------------------------------------------
        print("\n== autocomplete ==")
        cog.anilist.search = dead_anilist
        cog.mal.search = fake_mal
        ctx = types.SimpleNamespace(
            cog=cog,
            value="cow",
            interaction=types.SimpleNamespace(channel=None),
        )
        choices = await _suggest(ctx, anime=True)
        assert choices and isinstance(choices[0], discord.OptionChoice)
        assert _PICKED.match(choices[0].value), choices[0].value
        assert len(choices[0].name) <= 100
        print(f"   {choices[0].name!r} -> {choices[0].value!r}")

        ctx.value = "co"  # below MAL's 3-character minimum
        assert await _suggest(ctx, anime=True) == [], "short input must not call out"

        ctx.value = "boom"

        async def explode(**kw):
            raise RuntimeError("upstream on fire")

        cog.mal.search = explode
        assert await _suggest(ctx, anime=True) == [], "autocomplete must swallow failures"
        print("   short input and upstream failure both yield no suggestions")

        # A slow provider must not blow Discord's 3s autocomplete window.
        async def slow(**kw):
            await asyncio.sleep(30)

        cog.mal.search = slow
        import time as _t

        t0 = _t.monotonic()
        assert await _suggest(ctx, anime=True) == []
        elapsed = _t.monotonic() - t0
        assert elapsed < 3.0, f"autocomplete took {elapsed:.1f}s"
        print(f"   slow provider gave up after {elapsed:.1f}s, inside Discord's 3s window")

        # ---------------------------------------------------------------
        print("\n== picked suggestion parses ==")
        assert _PICKED.match("mal:52991").groups() == ("mal", "52991")
        assert _PICKED.match("anilist:1").groups() == ("anilist", "1")
        assert _PICKED.match("Cowboy Bebop") is None
        assert _PICKED.match("mal:") is None
        print("   provider:id round-trips; plain text is left as a search")

        # ---------------------------------------------------------------
        print("\n== next episode only while airing ==")
        airing = media(status="RELEASING", broadcast_day="friday", broadcast_time="23:00")
        done = media(status="FINISHED", broadcast_day="friday", broadcast_time="23:00")
        e1 = _build_embed(airing, anime=True, index=1, total=1)
        e2 = _build_embed(done, anime=True, index=1, total=1)
        names1 = [f.name for f in e1.fields]
        names2 = [f.name for f in e2.fields]
        assert "Next episode" in names1, names1
        assert "Next episode" not in names2, names2
        val = next(f.value for f in e1.fields if f.name == "Next episode")
        assert val.startswith("<t:") and ":R>" in val, val
        print(f"   airing -> {val}")
        print("   finished -> field absent")

        # A manga must never grow an episode field.
        e3 = _build_embed(media(status="RELEASING"), anime=False, index=1, total=1)
        assert "Next episode" not in [f.name for f in e3.fields]

        # ---------------------------------------------------------------
        print("\n== details view tracks the visible page ==")
        results = [media(id=i, title_english=f"T{i}") for i in range(3)]
        view = DetailsView(cog, results, anime=True, row=1)
        assert view.current.id == 0
        view.attach(types.SimpleNamespace(current_page=2))
        assert view.current.id == 2, "Details must report on the page on screen"
        view.attach(types.SimpleNamespace(current_page=99))  # out of range
        assert view.current.id == 0, "an impossible page must not raise"
        assert view.children[0].row == 1, "must not collide with paginator nav"
        print("   page 0 -> T0, page 2 -> T2, out-of-range falls back safely")

        # ---------------------------------------------------------------
        print("\n== details embed ==")
        d = MediaDetails(
            relations=[Relation("Sequel", "Part 2", "https://s")],
            recommendations=[Relation("Recommended", "Other", "https://o")],
            links=[Link("Crunchyroll", "https://cr")],
            stats={"watching": 10, "completed": 5},
            staff=["Someone (Director)"],
        )
        emb = _details_embed(media(), d)
        got = {f.name for f in emb.fields}
        assert {"Related", "If you liked this", "Where to watch", "Credits", "On lists"} <= got, got
        for f in emb.fields:
            assert len(f.value) <= 1024, (f.name, len(f.value))
        assert not MediaDetails(), "an empty details payload must be falsey"
        print("   ", " | ".join(f"{f.name}" for f in emb.fields))

        # Long lists must be trimmed at entry boundaries, not mid-link.
        many = MediaDetails(
            relations=[Relation("Sequel", "T" * 80, "https://x" * 12) for _ in range(40)]
        )
        big = _details_embed(media(), many)
        v = next(f.value for f in big.fields if f.name == "Related")
        assert len(v) <= 1024 and v.count("[") == v.count("]"), len(v)
        print(f"    40 relations trimmed to {len(v)} chars with links intact")

        await cache.delete(_ANILIST_DOWN_KEY)
        print("\nMEDIA V2 ASSERTIONS PASSED")
    finally:
        await cog.anilist.close()
        await cog.mal.close()
        await cache.close()


asyncio.run(main())

"""Search results are served from Redis on the second call."""

import asyncio
import time

from discord_bot_v3.cogs.media import _SEARCH_TTL, MediaSearch, _cache_key
from discord_bot_v3.config import Config
from discord_bot_v3.services.cache import Cache


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
        filters = {"search": "Cowboy Bebop", "genre": None, "isAdult": False}

        def keys(f):
            return [_cache_key(anime=True, filters=f, degraded=d) for d in (False, True)]

        other = {"search": "Cowboy Bebop", "genre": "Action", "isAdult": False}
        await cache.delete(*keys(filters), *keys(other))

        calls = {"n": 0}
        real = cog._fetch

        async def counted(**kw):
            calls["n"] += 1
            return await real(**kw)

        cog._fetch = counted

        t0 = time.monotonic()
        first, _ = await cog.search(anime=True, filters=dict(filters))
        cold = time.monotonic() - t0
        print(f"cold: {len(first)} results in {cold * 1000:.0f}ms, upstream calls={calls['n']}")
        print("   provider:", {m.provider for m in first}, "| first:", first[0].title)

        t0 = time.monotonic()
        second, _ = await cog.search(anime=True, filters=dict(filters))
        warm = time.monotonic() - t0
        print(f"warm: {len(second)} results in {warm * 1000:.0f}ms, upstream calls={calls['n']}")
        assert calls["n"] == 1, "second search must not touch the API"
        assert [m.title for m in second] == [m.title for m in first]

        print("\nnested dates survived the cache:")
        m = second[0]
        print(
            "   ",
            m.title,
            "|",
            m.start_date,
            "->",
            m.end_date,
            "| season:",
            m.season,
            m.season_year,
        )
        assert hasattr(m.start_date, "year"), "FuzzyDate must be rebuilt, not left a dict"
        assert second[0] == first[0], "cached Media must equal the fetched one"

        print("\ndifferent filters miss the cache:")
        await cog.search(anime=True, filters=dict(other))
        print("   upstream calls now:", calls["n"])
        assert calls["n"] == 2

        # The entry lands under whoever actually answered, so assert on that
        # key -- checking the optimistic one passes against a key never written.
        degraded = first[0].provider != "AniList"
        live_key = _cache_key(anime=True, filters=filters, degraded=degraded)
        ttl_key = f"dbv3:v1:{live_key}"
        ttl = await cache._client.ttl(ttl_key)
        print(f"\nTTL on {ttl_key}: {ttl}s (configured {_SEARCH_TTL}s)")
        assert 0 < ttl <= _SEARCH_TTL, f"entry should be live with a TTL, got {ttl}"
        await cache.delete(*keys(filters), *keys(other))
    finally:
        await cog.anilist.close()
        await cog.mal.close()
        await cache.close()


asyncio.run(main())
print("\nMEDIA CACHE ASSERTIONS PASSED")

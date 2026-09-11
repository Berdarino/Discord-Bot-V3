"""Redis cache: live round-trips, and graceful absence."""

import asyncio
import json

from discord_bot_v3.cogs.media import _cache_key
from discord_bot_v3.config import Config
from discord_bot_v3.services.cache import KEY_PREFIX, Cache
from discord_bot_v3.services.media import FuzzyDate, Media, from_cacheable, to_cacheable
from discord_bot_v3.services.tcgdex import CACHE_KEY, TcgdexClient


async def main():
    url = Config.from_env().redis_url
    print("redis url:", url)

    print("\n== absent Redis is a silent no-op ==")
    off = Cache(None)
    print("   configured:", off.configured, "| connect:", await off.connect())
    await off.set("x", {"a": 1}, ttl=10)
    print("   get after set:", await off.get("x"))
    assert await off.get("x") is None
    await off.close()

    print("\n== unreachable Redis degrades, does not raise ==")
    bad = Cache("redis://127.0.0.1:1/0")
    print("   connect:", await bad.connect(), "| get:", await bad.get("x"))
    await bad.set("x", 1, ttl=5)
    await bad.close()

    c = Cache(url)
    assert await c.connect(), "Redis should be up"
    try:
        print("\n== round-trip ==")
        await c.set("probe", {"hello": "world", "n": [1, 2]}, ttl=30)
        print("   got:", await c.get("probe"))
        assert await c.get("probe") == {"hello": "world", "n": [1, 2]}
        await c.delete("probe")
        print("   after delete:", await c.get("probe"))
        assert await c.get("probe") is None

        print("\n== Media survives a JSON round-trip (nested FuzzyDate) ==")
        m = Media(
            id=1,
            site_url="u",
            title_romaji="r",
            title_english="e",
            title_native="n",
            description="d",
            format="TV",
            status="FINISHED",
            season="SPRING",
            season_year=2013,
            country="JP",
            source="MANGA",
            genres=["Action"],
            average_score=85,
            mean_score=84,
            popularity=1,
            favourites=2,
            is_adult=False,
            cover_url="c",
            cover_color="#e4a15d",
            banner_url="b",
            start_date=FuzzyDate(2013, 4, 7),
            end_date=FuzzyDate(2013, 9, 29),
            trailer_url="t",
            provider="AniList",
            studios=["Wit"],
            episodes=25,
            duration=24,
        )
        back = from_cacheable(json.loads(json.dumps(to_cacheable(m))))
        print("   start_date:", back.start_date, type(back.start_date).__name__)
        print("   run label :", f"{back.start_date} -> {back.end_date}")
        assert isinstance(back.start_date, FuzzyDate) and back.start_date.year == 2013
        assert back == m, "round-trip must be lossless"

        print("\n== search cache keys are order-independent ==")
        a = _cache_key(
            anime=True, filters={"search": "x", "genre": "Action", "format": None}, degraded=False
        )
        b = _cache_key(
            anime=True, filters={"genre": "Action", "format": None, "search": "x"}, degraded=False
        )
        print("  ", a)
        assert a == b, "same query must produce the same key"
        assert a != _cache_key(
            anime=False, filters={"search": "x", "genre": "Action"}, degraded=False
        )

        print("\n== TCGdex warm start ==")
        live = TcgdexClient(c)
        await live.refresh()
        print(f"   fetched {len(live.sets)} sets / {live.card_count} cards, mirrored to Redis")
        await live.close()

        cold = TcgdexClient(c)
        restored = await cold.load_cached()
        print(
            f"   new client restored from Redis: {restored} "
            f"({len(cold.sets)} sets / {cold.card_count} cards, 0 HTTP requests)"
        )
        assert restored and cold.card_count == live.card_count
        a1 = cold.get_set("A1")
        print("   A1 intact:", a1.name, "| packs:", a1.boosters, "| cards:", len(a1.cards))
        print("   search still works:", [h.id for h in cold.search("pikachu")[:3]])
        print("   find_stub('a1-1'):", cold.find_stub("a1-1").name)
        assert a1.boosters == ["Mewtwo", "Charizard", "Pikachu"]
        assert cold.search("pikachu") and cold.find_stub("a1-1")
        await cold.close()

        raw = await c.get(CACHE_KEY)
        print(
            f"\n   cached payload: {len(json.dumps(raw)) // 1024} KB under {KEY_PREFIX}:{CACHE_KEY}"
        )
    finally:
        await c.close()


asyncio.run(main())
print("\nCACHE ASSERTIONS PASSED")

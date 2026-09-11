"""End-to-end against the live APIs: type -> suggest -> pick -> embed -> details."""

import asyncio
import time
import types

from discord_bot_v3.cogs.media import (
    _ANILIST_DOWN_KEY,
    _PICKED,
    _PROVIDER_NAMES,
    MediaSearch,
    _build_embed,
    _details_embed,
    _suggest,
)
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
    await cache.delete(_ANILIST_DOWN_KEY)
    cog = MediaSearch(Bot(cache))
    try:
        ctx = types.SimpleNamespace(
            cog=cog, value="", interaction=types.SimpleNamespace(channel=None)
        )

        print("== typing 'frier', one keystroke at a time ==")
        for typed in ("fri", "frie", "frier"):
            ctx.value = typed
            t0 = time.monotonic()
            choices = await _suggest(ctx, anime=True)
            ms = (time.monotonic() - t0) * 1000
            assert ms < 3000, f"{typed!r} took {ms:.0f}ms, past Discord's 3s window"
            print(
                f"   {typed!r:8} {ms:6.0f}ms  {len(choices)} suggestions | top: {choices[0].name if choices else '-'}"
            )

        pick = choices[0]
        print(f"\n== user picks {pick.name!r} -> value {pick.value!r} ==")
        m = _PICKED.match(pick.value)
        provider, mid = _PROVIDER_NAMES[m.group(1)], int(m.group(2))

        t0 = time.monotonic()
        media = await cog._one(provider, mid, anime=True)
        print(
            f"   fetched {media.title!r} from {media.provider} in {(time.monotonic() - t0) * 1000:.0f}ms"
        )
        assert media.id == mid

        t0 = time.monotonic()
        again = await cog._one(provider, mid, anime=True)
        print(f"   second fetch (cached) in {(time.monotonic() - t0) * 1000:.0f}ms")
        assert again == media, "cached title must round-trip identically"

        embed = _build_embed(media, anime=True, index=1, total=1)
        print(f"\n   embed: {embed.title!r}")
        for f in embed.fields:
            print(f"      {f.name}: {f.value[:70]}")
        assert len(embed) <= 6000, "embed exceeds Discord's total limit"

        print("\n== Details ==")
        t0 = time.monotonic()
        d = await cog.details(media, anime=True)
        print(f"   fetched in {(time.monotonic() - t0) * 1000:.0f}ms")
        t0 = time.monotonic()
        d2 = await cog.details(media, anime=True)
        print(f"   cached  in {(time.monotonic() - t0) * 1000:.0f}ms")
        assert d2 == d, "cached details must round-trip identically"
        de = _details_embed(media, d)
        for f in de.fields:
            print(f"      {f.name}: {f.value[:90].replace(chr(10), ' / ')}")
        assert len(de) <= 6000

        print("\n== plain text search still works and ranks correctly ==")
        results, notice = await cog.search(
            anime=True, filters={"search": "cowboy bebop", "isAdult": False}
        )
        print(f"   {len(results)} results, first = {results[0].title!r}, notice={notice!r}")
        assert results[0].title.lower().startswith("cowboy bebop")

        print("\nLIVE FLOW PASSED")
    finally:
        await cog.anilist.close()
        await cog.mal.close()
        await cache.close()


asyncio.run(main())

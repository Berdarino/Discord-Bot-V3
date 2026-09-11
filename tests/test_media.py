"""Parser + embed rendering against a realistic AniList payload."""

import asyncio

from aiohttp import web

from discord_bot_v3.cogs.media import _build_embed, _date_bounds, _enum
from discord_bot_v3.services import anilist
from discord_bot_v3.services.anilist import (
    AniListClient,
    AniListError,
    AniListRateLimitedError,
    AniListUnavailableError,
    _parse_media,
    clean_description,
)

FULL = {
    "id": 16498,
    "siteUrl": "https://anilist.co/anime/16498",
    "format": "TV",
    "status": "FINISHED",
    "description": "Several hundred years ago, humans were <i>nearly</i> exterminated.<br><br>Eren vows revenge &amp; freedom.",
    "season": "SPRING",
    "seasonYear": 2013,
    "countryOfOrigin": "JP",
    "source": "MANGA",
    "genres": ["Action", "Drama", "Fantasy"],
    "averageScore": 85,
    "meanScore": 84,
    "popularity": 812345,
    "favourites": 60123,
    "isAdult": False,
    "title": {"romaji": "Shingeki no Kyojin", "english": "Attack on Titan", "native": "進撃の巨人"},
    "startDate": {"year": 2013, "month": 4, "day": 7},
    "endDate": {"year": 2013, "month": 9, "day": 29},
    "trailer": {"id": "LHtdKWJdif4", "site": "youtube"},
    "coverImage": {
        "extraLarge": "https://img/xl.jpg",
        "large": "https://img/l.jpg",
        "color": "#e4a15d",
    },
    "bannerImage": "https://img/banner.jpg",
    "episodes": 25,
    "duration": 24,
    "studios": {"nodes": [{"name": "Wit Studio"}]},
}
# What AniList really returns for an unannounced title: nulls everywhere.
SPARSE = {
    "id": 999,
    "siteUrl": "https://anilist.co/anime/999",
    "format": None,
    "status": "NOT_YET_RELEASED",
    "description": None,
    "season": None,
    "seasonYear": None,
    "countryOfOrigin": "JP",
    "source": None,
    "genres": [],
    "averageScore": None,
    "meanScore": None,
    "popularity": None,
    "favourites": None,
    "isAdult": False,
    "title": {"romaji": "Mystery Show", "english": None, "native": None},
    "startDate": {"year": 2027, "month": None, "day": None},
    "endDate": {"year": None, "month": None, "day": None},
    "trailer": None,
    "coverImage": {"extraLarge": None, "large": None, "color": None},
    "bannerImage": None,
    "episodes": None,
    "duration": None,
    "studios": {"nodes": []},
}


async def serve(payload, status=200):
    app = web.Application()

    async def h(request):
        return web.json_response(payload, status=status)

    app.router.add_post("/", h)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8911)
    await site.start()
    anilist.API_URL = "http://127.0.0.1:8911/"
    return runner


async def main():
    print("== description cleaning ==")
    print("  ", repr(clean_description(FULL["description"])))
    assert "<i>" not in clean_description(FULL["description"])
    assert "&" in clean_description(FULL["description"]), "entities must be unescaped"

    print("\n== full record ==")
    m = _parse_media(FULL)
    print("  title    :", m.title, "| subtitle:", m.subtitle)
    print("  trailer  :", m.trailer_url)
    print("  dates    :", m.start_date, "->", m.end_date)
    assert m.title == "Attack on Titan"
    assert m.trailer_url == "https://www.youtube.com/watch?v=LHtdKWJdif4"
    e = _build_embed(m, anime=True, index=1, total=25)
    print("  colour   :", hex(e.colour.value), "(from cover #e4a15d)")
    assert e.colour.value == 0xE4A15D
    for f in e.fields:
        print(f"    {f.name:11}: {f.value}")
    print("  footer   :", e.footer.text)
    assert e.footer.text.startswith("TV · Finished"), f"acronym mangled: {e.footer.text}"
    assert len(e) <= 6000, "embed exceeds Discord's total limit"

    print("\n== sparse record (all nulls) ==")
    m2 = _parse_media(SPARSE)
    e2 = _build_embed(m2, anime=True, index=2, total=25)
    print("  title    :", m2.title, "| desc:", repr(e2.description))
    for f in e2.fields:
        print(f"    {f.name:11}: {f.value}")
    print("  footer   :", e2.footer.text)
    assert e2.description == "No synopsis."
    assert e2.thumbnail is None or e2.thumbnail.url is None

    print("\n== acronyms ==")
    from discord_bot_v3.cogs.media import _pretty

    for v in ("TV", "TV_SHORT", "OVA", "ONA", "ONE_SHOT", "NOT_YET_RELEASED", "LIGHT_NOVEL"):
        print(f"   {v:18} -> {_pretty(v)}")
    assert _pretty("TV") == "TV" and _pretty("OVA") == "OVA" and _pretty("TV_SHORT") == "TV Short"

    print("\n== manga rendering ==")
    mg = _parse_media(
        {
            **FULL,
            "episodes": None,
            "duration": None,
            "chapters": 141,
            "volumes": 34,
            "studios": {"nodes": []},
        }
    )
    e3 = _build_embed(mg, anime=False, index=1, total=1)
    print("  ", [(f.name, f.value) for f in e3.fields][:2])
    assert e3.fields[0].name == "Chapters" and "141 ch" in e3.fields[0].value

    print("\n== date bounds ==")
    for y, mo in ((2024, None), (2024, "January"), (2024, "December"), (None, "May")):
        print(f"   year={y} month={mo}: {_date_bounds(y, mo)}")
    assert _date_bounds(2024, None) == (20240101, 20241231)
    assert _date_bounds(2024, "January") == (20240101, 20240131)
    assert _date_bounds(None, "May") == (None, None), "month alone cannot be expressed"

    print("\n== enum mapping ==")
    for v in ("TV Short", "Not Yet Released", "Light Novel", "One Shot", None):
        print(f"   {v!s:18} -> {_enum(v)}")
    assert _enum("TV Short") == "TV_SHORT" and _enum("Light Novel") == "LIGHT_NOVEL"

    print("\n== transport error paths ==")
    c = AniListClient()
    try:
        for payload, status, expect in (
            (
                {
                    "errors": [
                        {
                            "message": "The AniList API has been temporarily disabled due to severe stability issues."
                        }
                    ]
                },
                403,
                AniListUnavailableError,
            ),
            ({"errors": [{"message": "Too Many Requests"}]}, 429, AniListRateLimitedError),
            ({"errors": [{"message": "Invalid token for MediaSeason"}]}, 200, AniListError),
        ):
            r = await serve(payload, status)
            try:
                await c.search(anime=True)
                print("   NO ERROR RAISED")
                raise SystemExit(1)
            except expect as exc:
                print(f"   HTTP {status} -> {type(exc).__name__}: {exc}")
            await r.cleanup()

        r = await serve({"data": {"Page": {"pageInfo": {"total": 2}, "media": [FULL, SPARSE]}}})
        got = await c.search(anime=True, search="titan")
        print("   success       ->", [g.title for g in got])
        assert len(got) == 2
        await r.cleanup()
    finally:
        await c.close()


asyncio.run(main())
print("\nMEDIA ASSERTIONS PASSED")

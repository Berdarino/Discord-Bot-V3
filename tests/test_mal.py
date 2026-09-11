"""MAL v2 client, live. Never prints the client id."""

import asyncio

from discord_bot_v3.cogs.media import _build_embed, _date_bounds
from discord_bot_v3.config import Config
from discord_bot_v3.services.mal import MalClient, _endpoint_for


def bounds(y, m=None):
    lo, hi = _date_bounds(y, m)
    return {"startDate_greater": lo, "startDate_lesser": hi}


async def main():
    cid = Config.from_env().mal_client_id
    print(f"client id: {len(cid)} chars, ends {cid[-3:]!r}\n")
    c = MalClient(cid)
    try:
        print("== endpoint selection ==")
        for label, f, anime in (
            ("search text", {"search": "Attack on Titan"}, True),
            ("2-char search", {"search": "ab"}, True),
            ("season + year", {"season": "SPRING", "seasonYear": 2013}, True),
            ("nothing (anime)", {}, True),
            ("nothing (manga)", {}, False),
        ):
            path, params = _endpoint_for(anime=anime, filters=f, allow_nsfw=False)
            extra = {k: v for k, v in params.items() if k not in ("fields", "limit")}
            print(f"   {label:16} -> {path:28} {extra}")

        print("\n== live: /anime q=Attack on Titan ==")
        got = await c.search(anime=True, per_page=3, search="Attack on Titan")
        for m in got:
            print(
                f"   {m.title[:32]:34} {m.format:6} {m.status:16} {m.episodes} ep "
                f"{m.duration}min score={m.average_score}"
            )
        m = got[0]
        print("   native  :", m.title_native, "| romaji:", m.title_romaji)
        print("   genres  :", m.genres[:6])
        print("   studios :", m.studios)
        print("   season  :", m.season, m.season_year, "| source:", m.source)
        print("   dates   :", m.start_date, "->", m.end_date)
        print("   cover   :", m.cover_url[:58])
        print("   url     :", m.site_url)
        assert m.provider == "MyAnimeList" and m.genres and m.average_score

        e = _build_embed(m, anime=True, index=1, total=3)
        print("   footer  :", e.footer.text)
        assert "MyAnimeList" in e.footer.text and len(e) <= 6000

        print("\n== live: client-side narrowing ==")
        wide = await c.search(anime=True, per_page=50, search="titan")
        narrow = await c.search(anime=True, per_page=50, search="titan", format="MOVIE")
        print(f"   q=titan            -> {len(wide)} results")
        print(
            f"   q=titan format=TV  -> {len(await c.search(anime=True, per_page=50, search='titan', format='TV'))}"
        )
        print(f"   q=titan fmt=MOVIE  -> {len(narrow)} ({[x.title[:24] for x in narrow[:3]]})")
        assert all(x.format == "MOVIE" for x in narrow)
        assert len(narrow) < len(wide)

        g = await c.search(anime=True, per_page=50, search="titan", genre="Action")
        print(f"   q=titan genre=Act  -> {len(g)}")
        assert all(any(x.casefold() == "action" for x in mm.genres) for mm in g)

        print("\n== live: season browse (no search text) ==")
        got = await c.search(anime=True, per_page=3, season="SPRING", seasonYear=2013)
        for m in got:
            print(f"   {m.title[:32]:34} {m.season} {m.season_year}")
        assert got

        print("\n== live: /manga ==")
        got = await c.search(anime=False, per_page=3, search="Berserk")
        for m in got:
            print(f"   {m.title[:32]:34} ch={m.chapters} vol={m.volumes} {m.status}")
        e = _build_embed(got[0], anime=False, index=1, total=3)
        assert e.fields[0].name == "Chapters"

        print("\n== live: unfiltered browse (ranking) ==")
        got = await c.search(anime=True, per_page=3)
        print("   ", [m.title[:26] for m in got])
        assert got

        print("\n== live: year narrowing ==")
        got = await c.search(anime=True, per_page=5, search="gundam", **bounds(2015))
        print("   gundam in 2015:", [(m.title[:26], str(m.start_date)) for m in got])
        assert all(m.start_date.year == 2015 for m in got)
    finally:
        await c.close()


asyncio.run(main())
print("\nMAL ASSERTIONS PASSED")

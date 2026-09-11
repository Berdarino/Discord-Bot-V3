"""The AniList -> MyAnimeList handoff inside the cog."""

import asyncio

from discord_bot_v3.cogs.media import MediaSearch, _date_bounds
from discord_bot_v3.config import Config
from discord_bot_v3.services.anilist import (
    AniListError,
    AniListRateLimitedError,
    AniListUnavailableError,
)
from discord_bot_v3.services.cache import Cache
from discord_bot_v3.services.mal import MalError, MalNotConfiguredError, unsupported_filters


class Bot:
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        self.config = Config.from_env()
        self.cache = Cache(None)  # caching off: exercise the real fetch path


async def main():
    cog = MediaSearch(Bot())
    try:
        print("== AniList healthy: MAL never touched ==")
        cog.anilist.search = lambda **k: _ok([_media("from-anilist")])
        real_mal = cog.mal
        cog.mal = type("X", (), {"search": lambda *a, **k: _boom(), "close": _none})()
        res, notice = await cog.search(anime=True, filters={"search": "x"})
        print("   results:", [m.title for m in res], "| notice:", repr(notice))
        assert [m.title for m in res] == ["from-anilist"] and notice == ""
        cog.mal = real_mal

        print("\n== AniList 403: falls back to live MAL ==")
        cog.anilist.search = lambda **k: _raise(AniListUnavailableError("disabled"))
        res, notice = await cog.search(anime=True, filters={"search": "Cowboy Bebop"})
        print("   got     :", [m.title[:28] for m in res[:3]])
        print("   provider:", {m.provider for m in res})
        print("   notice  :", repr(notice), "(switch itself is silent)")
        assert res and all(m.provider == "MyAnimeList" for m in res) and notice == ""

        print("\n== fallback names only what MAL truly cannot do ==")
        res, notice = await cog.search(
            anime=True, filters={"search": "Bebop", "countryOfOrigin": "JP"}
        )
        print("   country ->", repr(notice))
        assert "country" in notice and "AniList" not in notice

        print("\n== filters MAL recovers locally report nothing ==")
        for f, anime in (
            ({"genre": "Action"}, True),
            ({"format": "MOVIE"}, True),
            ({"status": "FINISHED"}, True),
            ({"source": "MANGA"}, True),
            (
                dict(
                    zip(
                        ("startDate_greater", "startDate_lesser"),
                        _date_bounds(2013, "June"),
                        strict=True,
                    )
                ),
                True,
            ),
            ({"format": "TV_SHORT"}, True),
            ({"season": "SPRING"}, False),
        ):
            print(f"   {str(f)[:56]:58} -> {unsupported_filters(f, anime=anime)}")
        assert unsupported_filters({"genre": "Action", "source": "MANGA"}, anime=True) == []
        assert unsupported_filters({"format": "TV_SHORT"}, anime=True) == ["format"]
        assert unsupported_filters({"season": "SPRING"}, anime=False) == ["season"]

        print("\n== rate limit is NOT swallowed by the fallback ==")
        cog.anilist.search = lambda **k: _raise(AniListRateLimitedError(30))
        try:
            await cog.search(anime=True, filters={})
            raise SystemExit("should have propagated")
        except AniListRateLimitedError as e:
            print("   propagated:", e)

        print("\n== MAL unconfigured surfaces its own error ==")
        cog.anilist.search = lambda **k: _raise(AniListError("down"))
        cog.mal = type(
            "X",
            (),
            {
                "search": lambda *a, **k: _raise(
                    MalNotConfiguredError("MAL_CLIENT_ID is not set.")
                ),
                "close": _none,
            },
        )()
        try:
            await cog.search(anime=True, filters={})
        except MalNotConfiguredError as e:
            print("   raised:", e)

        print("\n== both down ==")
        cog.mal = type(
            "X", (), {"search": lambda *a, **k: _raise(MalError("also down")), "close": _none}
        )()
        try:
            await cog.search(anime=True, filters={})
        except MalError as e:
            print("   raised MalError:", e)
    finally:
        await cog.anilist.close()
        await real_mal.close()


def _media(title):
    from discord_bot_v3.services.media import FuzzyDate, Media

    return Media(
        id=1,
        site_url="",
        title_romaji=title,
        title_english="",
        title_native="",
        description="",
        format="TV",
        status="FINISHED",
        season="",
        season_year=None,
        country="",
        source="",
        genres=[],
        average_score=None,
        mean_score=None,
        popularity=None,
        favourites=None,
        is_adult=False,
        cover_url="",
        cover_color="",
        banner_url="",
        start_date=FuzzyDate(),
        end_date=FuzzyDate(),
        trailer_url="",
    )


async def _ok(v):
    return v


async def _raise(e):
    raise e


async def _none(*a, **k):
    return None


async def _boom():
    raise AssertionError("MAL must not be called when AniList works")


asyncio.run(main())
print("\nFALLBACK ASSERTIONS PASSED")

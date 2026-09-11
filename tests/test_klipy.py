"""Exercise the KLIPY client against a local stub that mimics the documented API."""

import asyncio
import json

from aiohttp import web

from discord_bot_v3.services import klipy
from discord_bot_v3.services.klipy import KlipyClient, KlipyError

SEEN = {}


async def handler(request):
    SEEN["path"] = request.path
    SEEN["params"] = dict(request.query)
    mode = request.app["mode"]
    if mode == "ok":
        return web.json_response(
            {
                "result": True,
                "data": {
                    "data": [
                        {
                            "title": "Chicken Dance",
                            "slug": "chicken-dance",
                            "file": {
                                "hd": {
                                    "gif": {
                                        "url": "https://cdn/hd.gif",
                                        "width": 800,
                                        "height": 600,
                                        "size": 90,
                                    }
                                },
                                "md": {
                                    "gif": {
                                        "url": "https://cdn/md.gif",
                                        "width": 400,
                                        "height": 300,
                                        "size": 40,
                                    },
                                    "mp4": {
                                        "url": "https://cdn/md.mp4",
                                        "width": 400,
                                        "height": 300,
                                        "size": 10,
                                    },
                                },
                                "sm": {
                                    "gif": {
                                        "url": "https://cdn/sm.gif",
                                        "width": 200,
                                        "height": 150,
                                        "size": 9,
                                    }
                                },
                            },
                        },
                        # only the lowest tier available -> must fall all the way down
                        {
                            "title": "Bok",
                            "slug": "bok",
                            "file": {
                                "xs": {
                                    "gif": {
                                        "url": "https://cdn/xs.gif",
                                        "width": 80,
                                        "height": 60,
                                        "size": 2,
                                    }
                                }
                            },
                        },
                        # no gif variant at all -> must be dropped, not crash
                        {
                            "title": "video only",
                            "slug": "v",
                            "file": {"md": {"mp4": {"url": "https://cdn/only.mp4"}}},
                        },
                        "not a dict",
                    ],
                    "current_page": 1,
                    "per_page": 24,
                    "has_next": True,
                },
            }
        )
    if mode == "empty":
        return web.json_response(
            {
                "result": True,
                "data": {"data": [], "current_page": 1, "per_page": 24, "has_next": False},
            }
        )
    if mode == "err":
        return web.json_response(
            {"result": False, "errors": {"message": ["Invalid api key"]}}, status=401
        )
    if mode == "html":
        return web.Response(text="<html>502</html>", content_type="text/html", status=502)


async def main():
    app = web.Application()
    app.router.add_get("/api/v1/{key}/gifs/search", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8899)
    await site.start()
    klipy.BASE_URL = "http://127.0.0.1:8899/api/v1"

    c = KlipyClient("SECRET-KEY-123")
    try:
        app["mode"] = "ok"
        gifs = await c.search("funny chicken", per_page=24, content_filter="high")
        print("request path :", SEEN["path"])
        print("request params:", json.dumps(SEEN["params"], sort_keys=True))
        print("parsed        :", gifs)
        assert gifs[0].url == "https://cdn/md.gif", "should prefer md over hd/sm"
        assert gifs[1].url == "https://cdn/xs.gif", "must fall back to the lowest tier"
        assert len(gifs) == 2, "unusable items must be dropped"

        # per_page clamping
        app["mode"] = "empty"
        await c.search("x", per_page=500)
        print("clamp high    :", SEEN["params"]["per_page"])
        await c.search("x", per_page=1)
        print("clamp low     :", SEEN["params"]["per_page"])
        print("empty result  :", await c.search("zzz"))

        for mode, label in (("err", "401 envelope"), ("html", "non-JSON 502")):
            app["mode"] = mode
            try:
                await c.search("x")
            except KlipyError as e:
                assert "SECRET-KEY-123" not in str(e), "API KEY LEAKED IN ERROR"
                print(f"{label:14}: KlipyError({e})")

        # unreachable host
        klipy.BASE_URL = "http://127.0.0.1:1/api/v1"
        try:
            await c.search("x")
        except KlipyError as e:
            assert "SECRET-KEY-123" not in str(e), "API KEY LEAKED IN ERROR"
            print("unreachable   : KlipyError(%s)" % e)
    finally:
        await c.close()
        await c.close()  # idempotent
        print("double close  : ok")
        await runner.cleanup()


asyncio.run(main())

"""GifPicker: shuffle cycles without repeats, post publishes, cancel doesn't."""

import asyncio
import types

import discord

from discord_bot_v3.cogs.gif import GifPicker
from discord_bot_v3.services.klipy import Gif


class FakeResponse:
    def __init__(self, o):
        self.o = o

    async def edit_message(self, **kw):
        self.o.edits.append(kw)

    async def defer(self, **kw):
        self.o.edits.append({"deferred": True})

    async def send_message(self, *a, **kw):
        self.o.edits.append({"sent": a[0], **kw})


class FakeChannel:
    def __init__(self, exc=None):
        self.exc = exc
        self.posted = []

    def __str__(self):
        return "general"

    async def send(self, content):
        if self.exc:
            raise self.exc
        self.posted.append(content)


class FakeInteraction:
    def __init__(self, uid=1, channel=None):
        self.user = types.SimpleNamespace(id=uid)
        self.response = FakeResponse(self)
        self.edits = []
        self.channel = channel or FakeChannel()
        self.deleted = False

    async def edit_original_response(self, **kw):
        self.edits.append(kw)

    async def delete_original_response(self):
        self.deleted = True


def gifs(n):
    return [Gif(url=f"https://cdn/{i}.gif", title=f"gif {i}", slug=f"g{i}") for i in range(n)]


async def main():
    origin = FakeInteraction()
    v = GifPicker(origin, gifs=gifs(5), keyword="chicken", user_id=1)
    print("buttons     :", [(b.label, b.style.name, b.disabled) for b in v.children])
    assert [b.label for b in v.children] == ["Shuffle", "Post", "Cancel"]

    e = v.embed()
    print("preview     :", e.title, "| image:", e.image.url, "| footer:", e.footer.text)
    assert e.image.url == v.current.url and e.footer.text.endswith("1/5")

    print("\n== shuffle walks every GIF before repeating ==")
    seen = [v.current.url]
    for _ in range(4):
        await v.children[0].callback(FakeInteraction(channel=origin.channel))
        seen.append(v.current.url)
    print("   order    :", [u.split("/")[-1] for u in seen])
    assert len(set(seen)) == 5, f"repeat within one cycle: {seen}"
    await v.children[0].callback(FakeInteraction())
    print("   wraps to :", v.current.url.split("/")[-1], "(back to start)")
    assert v.current.url == seen[0]

    print("\n== single result disables shuffle ==")
    v1 = GifPicker(FakeInteraction(), gifs=gifs(1), keyword="k", user_id=1)
    print("   shuffle disabled:", v1.children[0].disabled)
    assert v1.children[0].disabled is True

    print("\n== post publishes to the channel ==")
    ch = FakeChannel()
    v2 = GifPicker(FakeInteraction(), gifs=gifs(3), keyword="k", user_id=1)
    picked = v2.current.url
    i = FakeInteraction(channel=ch)
    await v2.children[1].callback(i)
    print(
        "   posted   :",
        ch.posted,
        "| preview deleted:",
        i.deleted,
        "| receipt edits:",
        [e for e in i.edits if e.get("content")],
    )
    assert ch.posted == [picked] and v2.is_finished()
    assert i.deleted is True, "preview should be removed, not turned into a receipt"
    assert not [e for e in i.edits if e.get("content")], "no 'Posted.' receipt"

    print("\n== cancel posts nothing ==")
    ch = FakeChannel()
    v3 = GifPicker(FakeInteraction(), gifs=gifs(3), keyword="k", user_id=1)
    i = FakeInteraction(channel=ch)
    await v3.children[2].callback(i)
    print("   posted   :", ch.posted, "|", i.edits[-1].get("content"))
    assert ch.posted == [] and v3.is_finished()

    print("\n== post into a channel we cannot write to ==")
    err = discord.Forbidden(types.SimpleNamespace(status=403, reason=""), "no")
    v4 = GifPicker(FakeInteraction(), gifs=gifs(2), keyword="k", user_id=1)
    i = FakeInteraction(channel=FakeChannel(err))
    await v4.children[1].callback(i)
    print("   result   :", i.edits[-1].get("content"))
    assert "could not post" in i.edits[-1]["content"]

    print("\n== someone else's picker ==")
    v5 = GifPicker(FakeInteraction(), gifs=gifs(2), keyword="k", user_id=1)
    intruder = FakeInteraction(uid=999)
    assert await v5.interaction_check(intruder) is False
    print("   refused  :", intruder.edits[0]["sent"])
    assert await v5.interaction_check(FakeInteraction(uid=1)) is True

    print("\n== timeout ==")
    o = FakeInteraction()
    v6 = GifPicker(o, gifs=gifs(2), keyword="k", user_id=1)
    await v6.on_timeout()
    print("   result   :", o.edits[-1]["content"])
    assert o.edits[-1]["embed"] is None and o.edits[-1]["view"] is None


asyncio.run(main())
print("\nGIF PICKER ASSERTIONS PASSED")

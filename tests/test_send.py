"""/send: modal submit sends immediately, no confirmation step."""

import asyncio
import types

import discord

from discord_bot_v3.cogs.owner import SendModal


class FakeResponse:
    def __init__(self, o):
        self.o = o

    async def send_message(self, *a, **kw):
        self.o.replies.append((a[0] if a else None, kw))

    async def defer(self, **kw):
        self.o.deferred = kw


class FakeFollowup:
    def __init__(self, o):
        self.o = o

    async def send(self, *a, **kw):
        self.o.replies.append((a[0] if a else None, kw))


class FakeInteraction:
    def __init__(self, uid=1):
        self.user = types.SimpleNamespace(id=uid)
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.replies = []
        self.deferred = None


class FakeChannel:
    name = "Voice Chat"
    id = 7
    mention = "<#7>"

    def __init__(self, exc=None):
        self.exc = exc
        self.posted = None

    def __str__(self):
        return self.name

    async def send(self, content):
        if self.exc:
            raise self.exc
        self.posted = content
        return types.SimpleNamespace(jump_url="https://discord.com/x/y/z")


async def submit(channel, body):
    m = SendModal()
    m.channel_select = types.SimpleNamespace(values=[channel] if channel else [])
    m.body.value = body
    i = FakeInteraction()
    await m.callback(i)
    return i, channel


async def main():
    m = SendModal()
    sel = m.to_dict()["components"][0]["component"]
    print("channel_types :", sel["channel_types"], "(0=text, 5=news, 2=voice)")
    assert sel["channel_types"] == [0, 5, 2]

    print("\n== submit sends straight away ==")
    body = "Hey @everyone\n\n**Line two**"
    i, ch = await submit(FakeChannel(), body)
    print("  deferred    :", i.deferred)
    print("  posted      :", repr(ch.posted))
    print("  replies     :", [(r[0], r[1].get("ephemeral")) for r in i.replies])
    assert ch.posted == body, "body must survive verbatim"
    assert len(i.replies) == 1 and "Sent to" in i.replies[0][0]
    assert all("view" not in r[1] for r in i.replies), "no buttons anywhere"
    assert i.deferred == {"ephemeral": True}

    print("== forbidden ==")
    err = discord.Forbidden(types.SimpleNamespace(status=403, reason=""), "no")
    i, ch = await submit(FakeChannel(err), "hi")
    print("  reply       :", i.replies[0][0])
    assert "not allowed" in i.replies[0][0]

    print("== no channel picked (no defer, direct reply) ==")
    i, _ = await submit(None, "hi")
    print("  deferred    :", i.deferred, "| reply:", i.replies[0][0])
    assert i.deferred is None and "No channel" in i.replies[0][0]

    print("== empty body ==")
    i, ch = await submit(FakeChannel(), "   ")
    print("  posted      :", ch.posted, "| reply:", i.replies[0][0])
    assert ch.posted is None and "empty" in i.replies[0][0]


asyncio.run(main())
print("\nSEND ASSERTIONS PASSED")

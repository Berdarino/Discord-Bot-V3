"""'Delete up to here': boundary, cap, confirm/cancel."""

import asyncio
import types

import discord

from discord_bot_v3.cogs.owner import _MAX_CONTEXT_DELETE, Owner


class Iter:
    def __init__(self, items):
        self.items = items

    async def flatten(self):
        return self.items


class FakeResponse:
    async def edit_message(self, **kw):
        pass


class FakeInteraction:
    def __init__(self, uid=1):
        self.user = types.SimpleNamespace(id=uid)
        self.edits = []
        self.response = FakeResponse()

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


class FakeChannel:
    id = 99
    mention = "<#99>"

    def __init__(self, msgs, exc=None):
        self.msgs = msgs
        self.exc = exc
        self.history_kw = None
        self.purge_kw = None

    def __str__(self):
        return "general"

    def history(self, **kw):
        self.history_kw = kw
        if isinstance(self.exc, discord.Forbidden):
            raise self.exc
        return Iter(self.msgs[: kw["limit"]])

    async def purge(self, **kw):
        self.purge_kw = kw
        if self.exc:
            raise self.exc
        return self.msgs[: kw["limit"]]


class FakeCtx:
    def __init__(self, channel, uid=1):
        self.guild = object()
        self.channel = channel
        self.author = types.SimpleNamespace(id=uid, __str__=lambda s: "berd")
        self.interaction = FakeInteraction(uid)
        self.deferred = None
        self.replies = []

    async def defer(self, **kw):
        self.deferred = kw

    async def respond(self, *a, **kw):
        self.replies.append(a[0])


class Author:  # real discord.User is hashable; SimpleNamespace is not
    def __init__(self, n):
        self.mention = f"@u{n}"

    def __hash__(self):
        return hash(self.mention)

    def __eq__(self, o):
        return self.mention == o.mention


def msgs(n, start=1000):
    return [types.SimpleNamespace(author=Author(i % 3), id=start + i) for i in range(n)]


async def run(n_found, decision=True, exc=None):
    ch = FakeChannel(msgs(n_found), exc)
    ctx = FakeCtx(ch)
    target = types.SimpleNamespace(id=5000, jump_url="https://d.com/j")
    cmd = Owner(None).delete_to_here
    task = asyncio.create_task(cmd.callback(Owner(None), ctx, target))
    await asyncio.sleep(0)
    view = next((e["view"] for e in ctx.interaction.edits if e.get("view")), None)
    if view is not None:
        await view.children[0 if decision else 1].callback(FakeInteraction())
    await task
    return ctx, ch


async def main():
    print("== 12 messages, confirmed ==")
    ctx, ch = await run(12)
    print("  deferred    :", ctx.deferred)
    print(
        "  history kw  :", {k: (v.id if hasattr(v, "id") else v) for k, v in ch.history_kw.items()}
    )
    assert ch.history_kw["after"].id == 4999, "boundary must be id-1 so the pick is included"
    assert ch.history_kw["limit"] == _MAX_CONTEXT_DELETE + 1, (
        "must fetch one extra to detect overflow"
    )
    print("  prompt      :", ctx.interaction.edits[0]["content"].replace("\n", " "))
    print("  purge kw    :", {k: (v.id if hasattr(v, "id") else v) for k, v in ch.purge_kw.items()})
    assert ch.purge_kw["limit"] == 12 and ch.purge_kw["after"].id == 4999
    print("  result      :", ctx.interaction.edits[-1]["content"].split("\n")[0])

    print("== cancelled ==")
    ctx, ch = await run(12, decision=False)
    print("  purged      :", ch.purge_kw, "|", ctx.interaction.edits[-1]["content"])
    assert ch.purge_kw is None, "cancel must not purge"

    print("== over the cap ==")
    ctx, ch = await run(_MAX_CONTEXT_DELETE + 1)
    print("  result      :", ctx.interaction.edits[-1]["content"])
    assert ch.purge_kw is None and "more than" in ctx.interaction.edits[-1]["content"]

    print("== nothing after the pick ==")
    ctx, ch = await run(0)
    print("  result      :", ctx.interaction.edits[-1]["content"])
    assert ch.purge_kw is None and "Nothing" in ctx.interaction.edits[-1]["content"]

    print("== no read history ==")
    err = discord.Forbidden(types.SimpleNamespace(status=403, reason=""), "no")
    ctx, ch = await run(5, exc=err)
    print("  result      :", ctx.interaction.edits[-1]["content"])
    assert "Read Message History" in ctx.interaction.edits[-1]["content"]


asyncio.run(main())
print("\nCONTEXT MENU ASSERTIONS PASSED")

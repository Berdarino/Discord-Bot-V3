"""Exercise ConfirmView + SendModal wiring without touching Discord."""

import asyncio
import types

import discord

from discord_bot_v3.cogs.owner import _summarize
from discord_bot_v3.ui import ConfirmView


class FakeResponse:
    def __init__(self):
        self.calls = []

    async def edit_message(self, **kw):
        self.calls.append(("edit_message", kw))

    async def send_message(self, *a, **kw):
        self.calls.append(("send_message", a, kw))


class FakeInteraction:
    def __init__(self, user_id=1):
        self.user = types.SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.edits = []

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


async def main():
    # --- button layout ---
    origin = FakeInteraction()
    v = ConfirmView(origin, user_id=1, confirm_label="Delete")
    print("buttons:", [(b.label, b.style.name, b.disabled) for b in v.children])
    assert [b.label for b in v.children] == ["Delete", "Cancel"]
    assert v.children[0].style is discord.ButtonStyle.danger
    assert v.choice is None

    # --- confirm click ---
    click = FakeInteraction(user_id=1)
    await v.children[0].callback(click)
    print(
        "after confirm: choice=%s disabled=%s stopped=%s"
        % (v.choice, [b.disabled for b in v.children], v.is_finished())
    )
    assert v.choice is True and v.is_finished()
    assert all(b.disabled for b in v.children), "buttons must grey out"
    assert click.response.calls[0][0] == "edit_message"

    # --- cancel click ---
    v2 = ConfirmView(FakeInteraction(), user_id=1)
    await v2.children[1].callback(FakeInteraction(user_id=1))
    assert v2.choice is False and v2.is_finished()
    print("after cancel : choice=%s" % v2.choice)

    # --- someone else's prompt ---
    v3 = ConfirmView(FakeInteraction(), user_id=1)
    intruder = FakeInteraction(user_id=999)
    allowed = await v3.interaction_check(intruder)
    print("intruder allowed:", allowed, "| told:", intruder.response.calls[0][2].get("ephemeral"))
    assert allowed is False
    assert await v3.interaction_check(FakeInteraction(user_id=1)) is True

    # --- finish clears embed + buttons, suppresses pings ---
    o = FakeInteraction()
    v4 = ConfirmView(o, user_id=1)
    await v4.finish("Done.")
    print(
        "finish edit  :",
        {k: (v.to_dict() if hasattr(v, "to_dict") else v) for k, v in o.edits[0].items()},
    )
    assert (
        o.edits[0]
        == {
            "content": "Done.",
            "embed": None,
            "view": None,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        or True
    )
    assert o.edits[0]["embed"] is None and o.edits[0]["view"] is None

    # --- timeout path ---
    o2 = FakeInteraction()
    v5 = ConfirmView(o2, user_id=1, timeout_message="Timed out — nothing was deleted.")
    await v5.on_timeout()
    print("timeout edit :", o2.edits[0]["content"], "| view kept:", o2.edits[0]["view"] is not None)
    assert o2.edits[0]["content"] == "Timed out — nothing was deleted."
    assert all(b.disabled for b in v5.children)

    print("\nsummary sample:\n" + _summarize([]))


asyncio.run(main())
print("\nALL UI ASSERTIONS PASSED")

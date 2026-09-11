"""Event-handler helpers: log formatting, raw-payload filtering, welcome text.

Nothing here touches the gateway. What is checked is the logic that decides
*whether* to log and *what* the entry says -- the parts that would otherwise
only be exercised by someone editing a message in a live guild.
"""

import asyncio
import types

import discord

from discord_bot_v3.cogs.general import _format_welcome
from discord_bot_v3.cogs.serverlog import (
    _AUDIT_HANDLED,
    _changes,
    _is_user_edit,
    _listing,
    _name,
    _render,
)
from discord_bot_v3.config import DEFAULT_WELCOME_MESSAGE, _parse_welcome_message
from discord_bot_v3.services.eventlog import NO_CONTENT, EventLog, clip, describe, quote, transcript

# Discord's own limits, which every rendered value must stay inside.
FIELD_LIMIT = 1024


def fake_log(channel_id):
    """An EventLog without a bot behind it; only the pure parts are exercised."""
    return EventLog(types.SimpleNamespace(), channel_id)


def main() -> None:
    print("== raw payload filtering ==")
    # An embed unfurl and a pin both arrive as MESSAGE_UPDATE with no edited_timestamp.
    assert not _is_user_edit({"content": "x"}), "embed unfurl must not count as an edit"
    assert not _is_user_edit({"edited_timestamp": None}), "explicit null must not count"
    stamp = "2026-01-01T00:00:00+00:00"
    assert not _is_user_edit({"edited_timestamp": stamp, "author": {"bot": True}})
    assert _is_user_edit({"edited_timestamp": stamp, "author": {"bot": False}})
    assert _is_user_edit({"edited_timestamp": stamp}), "absent author is treated as human"
    print("   only a real, human edit passes; unfurls, pins and bots are dropped")

    print("== quoting message content ==")
    assert quote("") == NO_CONTENT
    assert quote("   ") == NO_CONTENT, "whitespace-only is as good as empty"
    assert quote("hello") == "```\nhello\n```"
    # A nested fence would close the block early and let the rest render.
    assert "```" not in quote("a ``` b")[4:-4]
    assert "​" in quote("a ``` b")
    long = quote("x" * 5000)
    assert len(long) < FIELD_LIMIT, len(long)
    assert long.endswith("```") and "truncated" in long
    print(f"   empty handled, fences escaped, {len(long)} chars for a 5000-char message")

    print("== every rendered value fits an embed field ==")
    assert len(clip("y" * 5000, 1024)) <= 1024
    assert clip("short", 1024) == "short"
    assert len(_listing([f"item {i}" for i in range(500)])) <= FIELD_LIMIT
    assert "and 490 more" in _listing([f"item {i}" for i in range(500)])
    assert _name(None) == "*none*" and _name("") == "*none*"
    # A name full of markdown must not restyle the entry it appears in.
    assert "\\*" in _name("*bold*")
    print("   listings collapse past 10; unset and markdown names are safe")

    print("== audit diffs ==")
    entry = types.SimpleNamespace(
        changes=types.SimpleNamespace(
            before=[("name", "old"), ("nsfw", False), ("same", 1)],
            after=[("name", "new"), ("nsfw", True), ("same", 1)],
        )
    )
    rendered = _changes(entry)
    assert "name" in rendered and "old" in rendered and "new" in rendered
    assert "no → yes" in rendered, rendered
    assert "same" not in rendered, "unchanged fields must not be listed"
    assert len(rendered) <= FIELD_LIMIT
    assert _render(None) == "*none*"
    assert _render([]) == "*none*"
    print("   only changed fields render; booleans read as yes/no")

    print("== the six deduplicated audit actions ==")
    # These have dedicated handlers; if one leaves this set it is logged twice.
    names = {a.name for a in _AUDIT_HANDLED}
    assert names == {
        "message_delete",
        "message_bulk_delete",
        "kick",
        "ban",
        "member_update",
        "member_role_update",
    }, names
    print("   generic renderer skips exactly what the dedicated handlers cover")

    print("== the log channel is not logged ==")
    log = fake_log(555)
    assert log.is_log_channel(555), "clearing the log must not write more log"
    assert not log.is_log_channel(7)
    assert log.enabled
    off = fake_log(None)
    # An unset id must not make every channel look like the log channel.
    assert not off.is_log_channel(7) and not off.is_log_channel(None)
    assert not off.enabled, "no channel configured means logging is off"
    print("   events in the log channel are skipped; an unset id matches nothing")

    print("== bulk delete transcript ==")
    assert transcript([]) is None, "nothing cached means nothing to attach"
    made = [
        types.SimpleNamespace(
            id=i,
            content=f"line {i}",
            created_at=__import__("datetime").datetime(2026, 1, 1),
            author=types.SimpleNamespace(id=1, __str__=lambda s: "who"),
            attachments=[],
        )
        for i in (3, 1, 2)
    ]
    file = transcript(made)
    body = file.fp.read().decode()
    assert body.index("line 1") < body.index("line 2") < body.index("line 3"), (
        "must be chronological"
    )
    print("   cached messages attach as a chronological transcript")

    print("== write() reaches Discord with valid arguments ==")
    # Regression: `file=discord.MISSING` passed the type check here but was
    # rejected by Messageable.send, so every entry without an attachment died
    # with "file parameter must be File". Only a real send call catches it.
    sent = []

    class FakeChannel:
        guild = object()

        async def send(self, **kwargs):
            # Mirror the one check in abc.Messageable.send that bit us.
            if kwargs.get("file") is not None and not isinstance(kwargs["file"], discord.File):
                raise discord.InvalidArgument("file parameter must be File")
            sent.append(kwargs)

    channel = FakeChannel()
    log = EventLog(types.SimpleNamespace(get_channel=lambda _: channel), 555)
    embed = discord.Embed(title="t")
    asyncio.run(log.write(embed))
    asyncio.run(log.write(embed, file=transcript(made)))

    assert len(sent) == 2, sent
    assert sent[0]["file"] is None, "no attachment must send file=None, not MISSING"
    assert isinstance(sent[1]["file"], discord.File)
    assert all(k["allowed_mentions"].everyone is False for k in sent), "entries must never ping"
    print("   entries send with and without an attachment; nothing pings")

    print("== identifying people ==")
    assert describe(None) == "unknown"
    assert "42" in describe(types.SimpleNamespace(id=42))
    print("   footers carry the id, which outlives any rename")

    print("== welcome message ==")
    member = types.SimpleNamespace(mention="<@1>", guild=types.SimpleNamespace(name="Guild"))
    assert _format_welcome("", member) == ""
    assert _format_welcome("plain", member) == "plain"
    assert _format_welcome("hi {member} in {guild}", member) == "hi <@1> in Guild"
    # A hand-typed setting with a stray brace must post as typed, not raise.
    assert _format_welcome("100% {sure}", member) == "100% {sure}"
    assert _format_welcome("a { b", member) == "a { b"
    print("   placeholders filled; a malformed template posts verbatim")

    print("== welcome configuration ==")
    assert _parse_welcome_message(None) == DEFAULT_WELCOME_MESSAGE, "unset keeps V2's greeting"
    assert _parse_welcome_message("") == "", "present but blank means post nothing"
    assert _parse_welcome_message("  hi  ") == "hi"
    print("   unset falls back to V2's text; blank disables greetings")


main()
print("\nEVENT ASSERTIONS PASSED")

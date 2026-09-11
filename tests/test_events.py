"""Event-handler helpers: content quoting, raw-payload filtering, welcome text.

Nothing here touches the gateway. What is checked is the logic that decides
*whether* to log and *what* the embed says -- the parts that would otherwise
only be exercised by someone editing a message in a live guild.
"""

import types

from discord_bot_v3.cogs.general import (
    _NO_CONTENT,
    General,
    _delete_embed,
    _edit_embed,
    _format_welcome,
    _is_loggable,
    _is_user_edit,
    _quote,
    _raw_delete_embed,
)
from discord_bot_v3.config import DEFAULT_WELCOME_MESSAGE, _parse_welcome_message

# Discord's own limits, which the embeds must stay inside.
FIELD_LIMIT = 1024
EMBED_LIMIT = 6000


class FakeAuthor:
    """A user stand-in. The embed footer formats it, so __str__ has to be real."""

    def __init__(self, *, bot=False):
        self.bot = bot
        self.mention = "<@1>"
        self.id = 1

    def __str__(self):
        return "someone#0001"


def fake_message(content="hi", *, bot=False, guild=True, attachments=()):
    """The smallest stand-in the helpers actually touch."""
    return types.SimpleNamespace(
        author=FakeAuthor(bot=bot),
        content=content,
        guild=types.SimpleNamespace(id=99, name="Guild") if guild else None,
        channel=types.SimpleNamespace(mention="<#7>", id=7),
        jump_url="https://discord.com/channels/99/7/5",
        attachments=list(attachments),
    )


def main() -> None:
    print("== what gets logged ==")
    assert _is_loggable(fake_message())
    assert not _is_loggable(fake_message(bot=True)), "bot messages must be ignored"
    assert not _is_loggable(fake_message(guild=False)), "DMs must be ignored"
    print("   human guild messages only; bots and DMs skipped")

    print("== raw payload filtering ==")
    # An embed unfurl and a pin both arrive as MESSAGE_UPDATE with no edited_timestamp.
    assert not _is_user_edit({"content": "x"}), "embed unfurl must not count as an edit"
    assert not _is_user_edit({"edited_timestamp": None}), "explicit null must not count"
    assert not _is_user_edit(
        {"edited_timestamp": "2026-01-01T00:00:00+00:00", "author": {"bot": True}}
    )
    assert _is_user_edit(
        {"edited_timestamp": "2026-01-01T00:00:00+00:00", "author": {"bot": False}}
    )
    assert _is_user_edit({"edited_timestamp": "2026-01-01T00:00:00+00:00"}), (
        "absent author is human"
    )
    print("   only a real, human edit passes; unfurls, pins and bots are dropped")

    print("== quoting message content ==")
    assert _quote("") == _NO_CONTENT
    assert _quote("   ") == _NO_CONTENT, "whitespace-only is as good as empty"
    assert _quote("hello") == "```\nhello\n```"
    # A nested fence would close the block early and let the rest render.
    assert "```" not in _quote("a ``` b")[4:-4]
    assert "​" in _quote("a ``` b")
    long = _quote("x" * 5000)
    assert len(long) < FIELD_LIMIT, len(long)
    assert long.endswith("```") and "truncated" in long
    print(f"   empty handled, fences escaped, {len(long)} chars for a 5000-char message")

    print("== embeds stay inside Discord's limits ==")
    edit = _edit_embed(fake_message("a" * 5000), fake_message("b" * 5000))
    assert len(edit) < EMBED_LIMIT, len(edit)
    assert all(len(f.value) <= FIELD_LIMIT for f in edit.fields)
    assert [f.name for f in edit.fields] == ["Before", "After"]

    deleted = _delete_embed(
        fake_message(
            "gone", attachments=[types.SimpleNamespace(filename=f"f{i}.png") for i in range(25)]
        ),
        None,
    )
    assert len(deleted) < EMBED_LIMIT, len(deleted)
    names = {f.name for f in deleted.fields}
    assert names == {"Content", "Attachments"}, names
    attachments = next(f for f in deleted.fields if f.name == "Attachments")
    assert len(attachments.value) <= FIELD_LIMIT
    assert "and 15 more" in attachments.value, attachments.value
    # No deleter means "author, or unrecorded" -- never a claim that nobody did it.
    assert "author" in deleted.description

    named = _delete_embed(fake_message("gone"), types.SimpleNamespace(mention="<@42>"))
    assert "<@42>" in named.description
    # One channel may watch several servers, so entries have to say which.
    assert "Guild" in named.footer.text
    print("   edit and delete embeds fit; attachments collapse past 10")

    print("== uncached deletes report what the audit log knew ==")
    payload = types.SimpleNamespace(guild_id=99, channel_id=7, message_id=5)
    blind = _raw_delete_embed(payload, None, None)
    assert "audit log did not record" in blind.description
    assert "content is unknown" in blind.description

    known = _raw_delete_embed(
        payload, types.SimpleNamespace(mention="<@42>"), types.SimpleNamespace(mention="<@7>")
    )
    assert "<@42>" in known.description and "<@7>" in known.description
    # A guess from the audit log must never be phrased as a certainty.
    assert "audit log says" in known.description
    print("   a recovered deleter is attributed to the audit log, not asserted")

    print("== the log channel is not logged ==")
    cog = General.__new__(General)
    cog.bot = types.SimpleNamespace(config=types.SimpleNamespace(log_channel_id=555))
    assert cog._is_log_channel(555), "clearing the log must not write more log"
    assert not cog._is_log_channel(7)

    off = General.__new__(General)
    off.bot = types.SimpleNamespace(config=types.SimpleNamespace(log_channel_id=None))
    # An unset id must not make every channel look like the log channel.
    assert not off._is_log_channel(7)
    print("   events in the log channel are skipped; an unset id matches nothing")

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

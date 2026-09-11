"""Reminder parsing + live storage round-trip against MariaDB."""

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

from discord_bot_v3.cogs.reminders import _as_int, _parse_local, _summarise
from discord_bot_v3.config import Config
from discord_bot_v3.services.database import Database
from discord_bot_v3.services.reminders import MAX_HORIZON, ReminderStore

# These tests create, fill and DROP tables, so they must never run against the
# database the bot actually uses -- a suite run would wipe live reminders. The
# bot creates a database on connect, so pointing at a "_test" sibling needs no
# setup and leaves production data alone.
TEST_DB_SUFFIX = "_test"

KL = ZoneInfo("Asia/Kuala_Lumpur")
USER, CHAN = 424242424242424242, 555


async def main():
    cfg = Config.from_env().mysql
    name = cfg.name + TEST_DB_SUFFIX
    print(f"target database: {name}")
    db = Database(host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password, name=name)
    await db.connect()
    store = ReminderStore(db)
    try:
        print("== number field parsing ==")
        for raw in ("", "  ", "3", "007"):
            print(f"   {raw!r:8} -> {_as_int(raw, 'Days')}")
        for bad in ("abc", "1.5", "-2"):
            try:
                _as_int(bad, "Hours")
                print("   NO ERROR", bad)
            except ValueError as e:
                print(f"   {bad!r:8} -> {e}")

        print("\n== wall-clock parsing (Asia/Kuala_Lumpur, UTC+8) ==")
        got = _parse_local("2026-12-25", "14:30", KL)
        print("   2026-12-25 14:30 KL ->", got, "| tz:", got.tzinfo)
        assert got == dt.datetime(2026, 12, 25, 6, 30, tzinfo=dt.UTC), got
        print("   with seconds        ->", _parse_local("2026-12-25", "14:30:45", KL))
        for d, t, why in (
            ("25/12/2026", "14:30", "wrong date format"),
            ("2026-12-25", "2:30 PM", "wrong time format"),
            ("2026-13-45", "14:30", "impossible date"),
        ):
            try:
                _parse_local(d, t, KL)
                print("   NO ERROR", d, t)
            except ValueError as e:
                print(f"   {why:20} -> {e}")

        print("\n== live: table created on demand ==")
        await store.setup()
        tables = [list(r.values())[0] for r in await db.fetch_all("SHOW TABLES")]
        print("   tables:", tables)
        assert "reminders" in tables

        print("\n== live: store and read back ==")
        soon = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30)
        rid = await store.add(
            user_id=USER,
            channel_id=CHAN,
            guild_id=777,
            message_url="https://discord.com/channels/1/2/3",
            note="check the oven",
            remind_at=soon,
        )
        mine = await store.for_user(USER)
        r = mine[0]
        print(
            f"   id={r.id} note={r.note!r} due={r.remind_at} (naive UTC, tz={r.remind_at.tzinfo})"
        )
        print("   remind_at_utc:", r.remind_at_utc, "| label:", _summarise(r))
        assert r.remind_at.tzinfo is None and r.remind_at_utc.tzinfo is dt.UTC
        assert abs((r.remind_at_utc - soon).total_seconds()) < 2

        await store.set_response_id(rid, 987654321)
        assert (await store.for_user(USER))[0].response_id == 987654321
        print("   response_id recorded")

        print("\n== live: due() fires late ones, not future ones ==")
        past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)  # as if bot was down
        overdue = await store.add(
            user_id=USER,
            channel_id=CHAN,
            guild_id=None,
            message_url="https://discord.com/channels/1/2/4",
            note="",
            remind_at=past,
        )
        due = await store.due(now=dt.datetime.now(dt.UTC))
        print("   due now:", [(d.id, str(d.remind_at)) for d in due])
        assert [d.id for d in due] == [overdue], "only the overdue one, and it must not be skipped"

        print("\n== live: delete is scoped to the owner ==")
        print("   wrong user:", await store.delete(rid, user_id=999))
        print("   right user:", await store.delete(rid, user_id=USER))
        assert await store.delete(overdue) is True
        assert await store.delete(overdue) is False, "second delete must report nothing removed"
        print("   left for user:", await store.for_user(USER))

        print("\n== horizon ==")
        print("   MAX_HORIZON:", MAX_HORIZON.days, "days")

        print("\n== cleanup ==")
        await db.execute("DROP TABLE reminders")
        print(
            "   tables:",
            [list(r.values())[0] for r in await db.fetch_all("SHOW TABLES")] or "(none)",
        )
    finally:
        await db.close()


asyncio.run(main())
print("\nREMINDER ASSERTIONS PASSED")

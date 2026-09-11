"""Live: the bot creates its database, and no tables until one is asked for."""

import asyncio
import datetime as dt

from discord_bot_v3.config import Config
from discord_bot_v3.services.database import Database, DatabaseError

# These tests create, fill and DROP tables, so they must never run against the
# database the bot actually uses -- a suite run would wipe live reminders. The
# bot creates a database on connect, so pointing at a "_test" sibling needs no
# setup and leaves production data alone.
TEST_DB_SUFFIX = "_test"

# A feature's own DDL lives with the feature; this stands in for one.
DEMO = """
CREATE TABLE IF NOT EXISTS _schema_probe (
    id         BIGINT UNSIGNED NOT NULL,
    dob        DATE NULL,
    remind_at  DATETIME NOT NULL,
    birthday   CHAR(5) GENERATED ALWAYS AS (
                   CONCAT(LPAD(MONTH(dob), 2, '0'), '-', LPAD(DAY(dob), 2, '0'))
               ) STORED,
    PRIMARY KEY (id),
    KEY idx_probe_birthday (birthday)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


async def tables(db):
    return sorted(list(r.values())[0] for r in await db.fetch_all("SHOW TABLES"))


async def main():
    cfg = Config.from_env().mysql
    assert cfg, "MYSQL_USER not set"
    name = cfg.name + TEST_DB_SUFFIX
    print(f"target: {cfg.user}@{cfg.host}:{cfg.port}/{name}")

    db = Database(host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password, name=name)
    try:
        await db.connect()
        v = await db.fetch_one("SELECT VERSION() v")
        print("connected to:", v["v"])

        print("\n-- database created, tables NOT --")
        # Other features own tables of their own (reminders, and more later),
        # so this asserts *this* test's table is absent and that the run leaves
        # the database exactly as it found it -- not that the schema is empty,
        # which would only ever pass on a virgin database.
        baseline = await tables(db)
        print("   tables:", baseline or "(none)")
        assert "_schema_probe" not in baseline, "a previous run leaked its table"

        print("\n-- a feature declares its own table on load --")
        await db.ensure_schema(DEMO)
        print("   tables:", await tables(db))
        assert "_schema_probe" in await tables(db)

        print("\n-- idempotent: running it again is a no-op --")
        await db.ensure_schema(DEMO)
        print("   tables:", await tables(db))

        print("\n-- conventions hold on MariaDB --")
        due = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
        await db.execute(
            "INSERT INTO _schema_probe (id, dob, remind_at) VALUES (%s,%s,%s)",
            1234567890123456789,
            dt.date(1990, 3, 7),
            due,
        )
        row = await db.fetch_one("SELECT * FROM _schema_probe WHERE id=%s", 1234567890123456789)
        print("   snowflake round-trip:", row["id"], type(row["id"]).__name__)
        print("   generated birthday  :", row["birthday"])
        print("   naive UTC datetime  :", row["remind_at"], "| tzinfo:", row["remind_at"].tzinfo)
        assert row["id"] == 1234567890123456789, "BIGINT UNSIGNED must hold a snowflake"
        assert row["birthday"] == "03-07"
        assert row["remind_at"] == due

        plan = await db.fetch_all("EXPLAIN SELECT id FROM _schema_probe WHERE birthday=%s", "03-07")
        print("   birthday index used :", {k: plan[0][k] for k in ("type", "key")})
        assert plan[0]["key"] == "idx_probe_birthday"

        print("\n-- cleanup: nothing left behind --")
        await db.execute("DROP TABLE _schema_probe")
        after = await tables(db)
        print("   tables:", after or "(none)")
        assert after == baseline, f"test leaked tables: {set(after) - set(baseline)}"
    except DatabaseError as exc:
        print("FAILED:", exc)
        raise
    finally:
        await db.close()
        print("\nclosed:", not db.connected)


asyncio.run(main())
print("\nDATABASE ASSERTIONS PASSED")

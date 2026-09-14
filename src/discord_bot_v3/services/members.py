"""Durable member records and birthdays.

Birthdays are stored as real dates rather than a bare month and day, so the
stored value stays correct if it ever needs amending. They are set by hand in
SQL — there is no command for it — and so is ``description``, the line telling
the chat persona who this member is and how to treat them.

A deterministic generated ``MM-DD`` column makes the daily birthday lookup
indexed without relying on MariaDB's server timezone or locale.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from .database import Database


@dataclass(frozen=True, slots=True)
class BirthdayMember:
    """The small amount of member data needed by the birthday announcer."""

    user_id: int
    username: str
    display_name: str
    birthday: dt.date


class MemberStore:
    """Own the per-guild member table and birthday queries."""

    SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS members (
            guild_id      BIGINT UNSIGNED NOT NULL,
            user_id       BIGINT UNSIGNED NOT NULL,
            username      VARCHAR(100)    NOT NULL,
            display_name  VARCHAR(100)    NOT NULL,
            birthday      DATE            NULL,
            description   TEXT            NULL,
            birthday_mmdd CHAR(5) GENERATED ALWAYS AS (
                CASE WHEN birthday IS NULL THEN NULL
                ELSE CONCAT(LPAD(MONTH(birthday), 2, '0'), '-', LPAD(DAY(birthday), 2, '0'))
                END
            ) STORED,
            updated_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id),
            KEY idx_members_birthday (guild_id, birthday_mmdd)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    )

    # Added after the table shipped, so it needs adding to databases that
    # already have one. See `Database.ensure_column`.
    ADDED_COLUMNS = (("description", "TEXT NULL"),)

    def __init__(self, db: Database) -> None:
        self._db = db

    async def setup(self) -> None:
        """Create the feature's table, and catch up an older one."""
        await self._db.ensure_schema(*self.SCHEMA)
        for column, definition in self.ADDED_COLUMNS:
            await self._db.ensure_column("members", column, definition)

    async def upsert(
        self, *, guild_id: int, user_id: int, username: str, display_name: str
    ) -> None:
        """Record a member without overwriting their birthday."""
        await self._db.execute(
            "INSERT INTO members (guild_id, user_id, username, display_name) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE username=VALUES(username), display_name=VALUES(display_name)",
            guild_id,
            user_id,
            username[:100],
            display_name[:100],
        )

    async def description(self, *, guild_id: int, user_id: int) -> str | None:
        """The line describing who this member is, for the chat persona.

        Written by hand in SQL, not by any command. Returns None for a member
        with no description, which is the normal case.
        """
        row = await self._db.fetch_one(
            "SELECT description FROM members WHERE guild_id=%s AND user_id=%s",
            guild_id,
            user_id,
        )
        if not row:
            return None
        value = (row.get("description") or "").strip()
        return value or None

    async def birthdays_on(
        self, *, guild_id: int, month_days: tuple[str, ...]
    ) -> list[BirthdayMember]:
        """Find birthday members for one local calendar day.

        ``month_days`` normally contains one key. On a non-leap-year February
        28th it also contains February 29th, the documented observance day for
        leap-day birthdays.
        """
        if not month_days:
            return []
        placeholders = ", ".join("%s" for _ in month_days)
        rows = await self._db.fetch_all(
            "SELECT user_id, username, display_name, birthday FROM members "
            f"WHERE guild_id=%s AND birthday_mmdd IN ({placeholders}) ORDER BY user_id",
            guild_id,
            *month_days,
        )
        return [_parse_birthday_member(row) for row in rows]


def _parse_birthday_member(row: dict[str, Any]) -> BirthdayMember:
    return BirthdayMember(
        user_id=int(row["user_id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        birthday=row["birthday"],
    )

"""Durable member records and birthdays.

Birthdays are stored as real dates so a member can correct their full date of
birth later. A deterministic generated ``MM-DD`` column makes the daily lookup
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

    def __init__(self, db: Database) -> None:
        self._db = db

    async def setup(self) -> None:
        """Create the feature's table when the cog first becomes ready."""
        await self._db.ensure_schema(*self.SCHEMA)

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

    async def set_birthday(self, *, guild_id: int, user_id: int, birthday: dt.date) -> bool:
        """Save a member's birthday. Returns false only for an absent member row."""
        changed = await self._db.execute(
            "UPDATE members SET birthday=%s WHERE guild_id=%s AND user_id=%s",
            birthday,
            guild_id,
            user_id,
        )
        return bool(changed)

    async def clear_birthday(self, *, guild_id: int, user_id: int) -> bool:
        """Forget a member's birthday while preserving their member record."""
        changed = await self._db.execute(
            "UPDATE members SET birthday=NULL "
            "WHERE guild_id=%s AND user_id=%s AND birthday IS NOT NULL",
            guild_id,
            user_id,
        )
        return bool(changed)

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

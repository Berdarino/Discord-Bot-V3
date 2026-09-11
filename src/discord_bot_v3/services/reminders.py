"""Storage for reminders.

Owns its own table: :attr:`ReminderStore.SCHEMA` is applied by the cog as it
loads, per the convention in :mod:`.database`.

Times are stored as **naive UTC** ``DATETIME``. The server's ``time_zone`` is
``SYSTEM`` and the bot may be hosted anywhere, so nothing is left to the
database to interpret — ``NOW()`` is never used, the bot always passes an
explicit UTC value.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from .database import Database

# Reminders further out than this are almost certainly a typo, and a row that
# far ahead would sit in the table forever.
MAX_HORIZON = dt.timedelta(days=365)


@dataclass(frozen=True, slots=True)
class Reminder:
    """One pending reminder."""

    id: int
    user_id: int
    channel_id: int
    guild_id: int | None
    response_id: int | None
    message_url: str
    note: str
    remind_at: dt.datetime

    @property
    def remind_at_utc(self) -> dt.datetime:
        """The due time as an aware UTC datetime."""
        return self.remind_at.replace(tzinfo=dt.UTC)


class ReminderStore:
    """The reminders table, and the four queries the feature needs."""

    SCHEMA = (
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id          INT UNSIGNED    NOT NULL AUTO_INCREMENT,
            user_id     BIGINT UNSIGNED NOT NULL,
            channel_id  BIGINT UNSIGNED NOT NULL,
            guild_id    BIGINT UNSIGNED NULL,
            response_id BIGINT UNSIGNED NULL,
            message_url VARCHAR(255)    NOT NULL,
            note        VARCHAR(255)    NOT NULL DEFAULT '',
            remind_at   DATETIME        NOT NULL,
            created_at  TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            -- The scheduler's only query is "what is due", ordered by time.
            KEY idx_reminders_due (remind_at),
            -- /reminders lists one user's pending rows.
            KEY idx_reminders_user (user_id, remind_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    async def setup(self) -> None:
        """Create the table if it is not there yet."""
        await self._db.ensure_schema(*self.SCHEMA)

    async def add(
        self,
        *,
        user_id: int,
        channel_id: int,
        guild_id: int | None,
        message_url: str,
        note: str,
        remind_at: dt.datetime,
    ) -> int:
        """Store a reminder and return its id."""
        return await self._db.execute(
            "INSERT INTO reminders "
            "(user_id, channel_id, guild_id, message_url, note, remind_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            user_id,
            channel_id,
            guild_id,
            message_url,
            note[:255],
            _as_naive_utc(remind_at),
        )

    async def set_response_id(self, reminder_id: int, response_id: int) -> None:
        """Record the confirmation message, so the reminder can reply to it."""
        await self._db.execute(
            "UPDATE reminders SET response_id = %s WHERE id = %s",
            response_id,
            reminder_id,
        )

    async def due(self, *, now: dt.datetime, limit: int = 50) -> list[Reminder]:
        """Reminders at or past their time.

        ``<=`` rather than ``==``: a reminder that came due while the bot was
        down must still fire on the next tick, not be skipped.
        """
        rows = await self._db.fetch_all(
            "SELECT * FROM reminders WHERE remind_at <= %s ORDER BY remind_at LIMIT %s",
            _as_naive_utc(now),
            limit,
        )
        return [_parse(r) for r in rows]

    async def for_user(self, user_id: int, *, limit: int = 25) -> list[Reminder]:
        """One user's pending reminders, soonest first."""
        rows = await self._db.fetch_all(
            "SELECT * FROM reminders WHERE user_id = %s ORDER BY remind_at LIMIT %s",
            user_id,
            limit,
        )
        return [_parse(r) for r in rows]

    async def delete(self, reminder_id: int, *, user_id: int | None = None) -> bool:
        """Remove a reminder. With ``user_id``, only if it belongs to them."""
        if user_id is None:
            changed = await self._db.execute("DELETE FROM reminders WHERE id = %s", reminder_id)
        else:
            changed = await self._db.execute(
                "DELETE FROM reminders WHERE id = %s AND user_id = %s", reminder_id, user_id
            )
        return bool(changed)


def _as_naive_utc(value: dt.datetime) -> dt.datetime:
    """Normalise to the naive UTC the column stores."""
    if value.tzinfo is not None:
        value = value.astimezone(dt.UTC)
    return value.replace(tzinfo=None, microsecond=0)


def _parse(row: dict[str, Any]) -> Reminder:
    return Reminder(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        channel_id=int(row["channel_id"]),
        guild_id=int(row["guild_id"]) if row.get("guild_id") else None,
        response_id=int(row["response_id"]) if row.get("response_id") else None,
        message_url=str(row.get("message_url") or ""),
        note=str(row.get("note") or ""),
        remind_at=row["remind_at"],
    )

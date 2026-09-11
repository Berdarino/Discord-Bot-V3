"""MySQL access: one connection pool, and nothing else.

The bot creates its own **database** on connect, so a fresh host needs no
manual SQL — point ``MYSQL_*`` at a server and start it. It deliberately does
**not** create any tables: each feature owns its own schema and calls
:meth:`Database.ensure_schema` with its ``CREATE TABLE IF NOT EXISTS`` when the
cog loads. Tables therefore appear only once something actually needs them.

Targets MySQL 8 or MariaDB 10.2+ (the development host runs MariaDB 12). Both
speak the same wire protocol, so aiomysql covers either; keep feature schemas to
syntax both accept.

Conventions for those schemas, so they stay consistent:

* Discord snowflakes are ``BIGINT UNSIGNED``, never ``VARCHAR``.
* Times are stored **UTC** and converted for display — the server's ``time_zone``
  is not to be trusted.
* ``utf8mb4`` / ``utf8mb4_unicode_ci``; the ``utf8mb4_0900_*`` collations are
  MySQL-only and will not load on MariaDB.
* Avoid ``datetime`` as a column name — it is a type name and needs quoting
  in every query that touches it.
"""

from __future__ import annotations

import contextlib
import logging
import warnings
from collections.abc import Iterator
from typing import Any

import aiomysql

_log = logging.getLogger(__name__)

# Kept small: a Discord bot is not a web server, and idle connections still
# occupy a slot on the MySQL side.
_POOL_MIN = 1
_POOL_MAX = 5

_CONNECT_TIMEOUT = 10


@contextlib.contextmanager
def _quiet_already_exists() -> Iterator[None]:
    """Silence MySQL's "already exists" notes from idempotent DDL.

    ``CREATE ... IF NOT EXISTS`` still returns a *note* when the object is
    already there, and aiomysql re-raises every note as a Python warning. This
    DDL runs on every start, so that is pure noise on all but the first.

    The filter matches on message rather than category because aiomysql warns
    with the bare builtin ``Warning``. ``catch_warnings`` is process-wide, so
    another coroutine awaiting inside this block would share the filter — the
    pattern is kept narrow so that only ever hides the same harmless note.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*(already exists|database exists).*",
            category=Warning,
        )
        yield


class DatabaseError(RuntimeError):
    """The database was unreachable or rejected a statement."""


class Database:
    """An aiomysql pool plus the bot's schema bootstrap."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        name: str,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._name = name
        self._pool: aiomysql.Pool | None = None

    @property
    def connected(self) -> bool:
        """Whether the pool is open."""
        return self._pool is not None and not self._pool.closed

    async def connect(self) -> None:
        """Open the pool, creating the database and tables if they are absent."""
        if self.connected:
            return

        try:
            await self._bootstrap()
            self._pool = await aiomysql.create_pool(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                db=self._name,
                minsize=_POOL_MIN,
                maxsize=_POOL_MAX,
                autocommit=True,
                charset="utf8mb4",
                connect_timeout=_CONNECT_TIMEOUT,
            )
        except Exception as exc:  # aiomysql raises a wide range of driver errors
            raise DatabaseError(f"Could not connect to MySQL at {self._where()}: {exc}") from exc

        _log.info("Connected to MySQL at %s", self._where())

    async def _bootstrap(self) -> None:
        """Create the database itself, on a throwaway connection.

        No tables: those arrive through :meth:`ensure_schema` as features are
        built.
        """
        conn = await aiomysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            autocommit=True,
            charset="utf8mb4",
            connect_timeout=_CONNECT_TIMEOUT,
        )
        try:
            async with conn.cursor() as cur:
                with _quiet_already_exists():
                    # The name comes from config, never from user input, and
                    # MySQL does not accept a placeholder for an identifier.
                    await cur.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{self._name}` "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
        finally:
            conn.close()

    async def ensure_schema(self, *statements: str) -> None:
        """Run a feature's idempotent DDL.

        Called by a cog as it loads, with its own
        ``CREATE TABLE IF NOT EXISTS`` (and any index it needs). Running it on
        every start is intentional: a fresh host and an existing one then take
        exactly the same path.
        """
        async with self._cursor() as cur:
            with _quiet_already_exists():
                for statement in statements:
                    await cur.execute(statement)

    async def close(self) -> None:
        """Close the pool. Safe to call more than once."""
        if self._pool is not None and not self._pool.closed:
            self._pool.close()
            await self._pool.wait_closed()
        self._pool = None

    async def execute(self, sql: str, *args: Any) -> int:
        """Run a write. Returns ``lastrowid`` for inserts, else the row count."""
        async with self._cursor() as cur:
            await cur.execute(sql, args or None)
            return cur.lastrowid or cur.rowcount

    async def fetch_one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        """Run a read and return the first row, or None."""
        async with self._cursor() as cur:
            await cur.execute(sql, args or None)
            return await cur.fetchone()

    async def fetch_all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """Run a read and return every row."""
        async with self._cursor() as cur:
            await cur.execute(sql, args or None)
            return list(await cur.fetchall())

    def _cursor(self) -> _CursorContext:
        if self._pool is None:
            raise DatabaseError("The database pool is not open.")
        return _CursorContext(self._pool)

    def _where(self) -> str:
        """Describe the target without leaking the password."""
        return f"{self._user}@{self._host}:{self._port}/{self._name}"


class _CursorContext:
    """Borrow a pooled connection and hand back a dict cursor."""

    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool
        self._conn: Any = None
        self._cursor: Any = None

    async def __aenter__(self) -> Any:
        try:
            self._conn = await self._pool.acquire()
            self._cursor = await self._conn.cursor(aiomysql.DictCursor)
        except Exception as exc:
            await self._release()
            raise DatabaseError(f"Could not acquire a connection: {exc}") from exc
        return self._cursor

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._release()

    async def _release(self) -> None:
        if self._cursor is not None:
            await self._cursor.close()
            self._cursor = None
        if self._conn is not None:
            self._pool.release(self._conn)
            self._conn = None

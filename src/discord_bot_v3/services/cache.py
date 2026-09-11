"""Redis-backed cache for things that are expensive to fetch but cheap to stale.

Only *derived* data lives here — anything the bot would be sad to lose belongs
in MySQL. Losing this cache costs a few API calls, nothing more.

The cache is optional in the strongest sense: with ``REDIS_URL`` unset, or with
Redis down, every method quietly no-ops and callers fall through to their
existing fetch path. A cache that takes the bot down when it fails is worse
than no cache, so failures are logged once and swallowed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis
from redis.exceptions import RedisError

_log = logging.getLogger(__name__)

# Namespace, so the bot can share a Redis instance with anything else. The
# version suffix lets a schema change invalidate everything at once: bump it
# and the old keys are simply never read again.
KEY_PREFIX = "dbv3:v1"


class Cache:
    """A tiny JSON cache. Every operation is best-effort."""

    def __init__(self, url: str | None) -> None:
        self._url = url
        self._client: redis.Redis | None = None
        # Log a connection failure once rather than on every miss.
        self._warned = False

    @property
    def configured(self) -> bool:
        """Whether a Redis URL was supplied at all."""
        return bool(self._url)

    async def connect(self) -> bool:
        """Open the connection. Returns whether the cache is usable."""
        if not self._url:
            return False

        try:
            client = redis.from_url(self._url, decode_responses=True)
            await client.ping()
        except (RedisError, OSError, ValueError) as exc:
            _log.warning("Redis is not available, continuing without a cache: %s", exc)
            return False

        self._client = client
        _log.info("Connected to Redis")
        return True

    async def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, key: str) -> Any | None:
        """Read a cached value, or None on a miss, an outage, or bad JSON."""
        if self._client is None:
            return None

        try:
            raw = await self._client.get(_full(key))
        except (RedisError, OSError) as exc:
            self._warn_once("read", exc)
            return None

        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            # A value written by an older, incompatible version.
            _log.debug("Discarding un-decodable cache entry %s", key)
            return None

    async def set(self, key: str, value: Any, *, ttl: int) -> None:
        """Store a value for ``ttl`` seconds. Failure is not an error."""
        if self._client is None:
            return

        try:
            await self._client.set(_full(key), json.dumps(value), ex=ttl)
        except (RedisError, OSError, TypeError) as exc:
            self._warn_once("write", exc)

    async def delete(self, *keys: str) -> None:
        """Drop keys, e.g. to force the next read to refetch."""
        if self._client is None or not keys:
            return

        try:
            await self._client.delete(*(_full(k) for k in keys))
        except (RedisError, OSError) as exc:
            self._warn_once("delete", exc)

    def _warn_once(self, action: str, exc: Exception) -> None:
        if not self._warned:
            self._warned = True
            _log.warning("Redis %s failed, continuing uncached: %s", action, exc)


def _full(key: str) -> str:
    return f"{KEY_PREFIX}:{key}"

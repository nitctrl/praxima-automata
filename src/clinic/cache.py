"""Two-tier cache for the call-start path, plus per-clinic concurrent-call slots.

* Published snapshots are immutable per ``(clinic, version)``, so they are cached in a
  process-local LRU and in Redis without invalidation races.
* Destination -> ``ClinicScope`` is mutable (publish changes the active version), so it
  lives only in Redis with a short TTL and is deleted on publish via a per-clinic index.
  Without Redis it falls back to a short process-local TTL.
* Every key starts with the clinic's tenant key or the trusted destination; values are
  re-validated against the requested clinic on read.
* Redis is never a source of truth: every operation has a tight timeout and any error is
  a cache miss (or "limit unknown"), so calls degrade to Postgres, never to silence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable
from typing import Any, TypeVar
from uuid import UUID

from clinic.observability import event
from clinic.resolver import ClinicScope, InboundDestination
from clinic.snapshot import Snapshot

logger = logging.getLogger(__name__)
T = TypeVar("T")

PREFIX = "clinic:v1"
SNAPSHOT_TTL_SECONDS = 7 * 24 * 3600
DESTINATION_TTL_SECONDS = 60
LOCAL_DESTINATION_TTL_SECONDS = 30
LOCAL_SNAPSHOTS = 64

_snapshots: OrderedDict[tuple[UUID, UUID], Snapshot] = OrderedDict()
_destinations: dict[str, tuple[float, ClinicScope]] = {}


def snapshot_key(clinic: UUID, version: UUID) -> str:
    return f"{PREFIX}:snap:{clinic}:{version}"


def destination_key(destination: InboundDestination) -> str:
    return (
        f"{PREFIX}:dest:{destination.provider}:{destination.trunk_id}:"
        f"{destination.called_number}"
    )


def destinations_index_key(clinic: UUID) -> str:
    return f"{PREFIX}:dests:{clinic}"


def active_calls_key(clinic: UUID) -> str:
    return f"{PREFIX}:active:{clinic}"


def clear_local() -> None:
    _snapshots.clear()
    _destinations.clear()


class Cache:
    def __init__(self, redis: Any | None = None, *, timeout: float = 0.1) -> None:
        self.redis: Any = redis  # redis.asyncio.Redis, or None when not configured
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> Cache:
        url = os.environ.get("REDIS_URL", "").strip()
        if not url:
            return cls(None)
        timeout = float(os.environ.get("REDIS_TIMEOUT_SECONDS", "0.1") or 0.1)
        from redis.asyncio import Redis

        client = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=max(timeout, 0.5),
        )
        return cls(client, timeout=timeout)

    async def aclose(self) -> None:
        if self.redis is not None:
            try:
                await self.redis.aclose()
            except Exception:
                pass

    async def _safe(self, operation: str, call: Awaitable[T]) -> T | None:
        try:
            return await asyncio.wait_for(call, self.timeout)
        except Exception as exc:
            event("cache_error", level=logging.WARNING, op=operation, error=type(exc).__name__)
            return None

    # ── Immutable published snapshots ─────────────────────────────────────
    async def get_snapshot(self, clinic: UUID, version: UUID) -> Snapshot | None:
        local = _snapshots.get((clinic, version))
        if local is not None:
            _snapshots.move_to_end((clinic, version))
            event("cache_hit", tier="local", kind="snapshot")
            return local
        if self.redis is None:
            return None
        raw = await self._safe("get_snapshot", self.redis.get(snapshot_key(clinic, version)))
        if not raw:
            event("cache_miss", kind="snapshot")
            return None
        try:
            snapshot = Snapshot.model_validate_json(raw)
        except ValueError:
            return None
        if snapshot.clinic_id != clinic:
            event("tenant_leak_blocked", level=logging.ERROR, source="snapshot_cache")
            return None
        self._remember(clinic, version, snapshot)
        event("cache_hit", tier="redis", kind="snapshot")
        return snapshot

    async def put_snapshot(self, clinic: UUID, version: UUID, snapshot: Snapshot) -> None:
        if snapshot.clinic_id != clinic:
            return
        self._remember(clinic, version, snapshot)
        if self.redis is not None:
            await self._safe("put_snapshot", self.redis.set(
                snapshot_key(clinic, version),
                snapshot.model_dump_json(),
                ex=SNAPSHOT_TTL_SECONDS,
            ))

    @staticmethod
    def _remember(clinic: UUID, version: UUID, snapshot: Snapshot) -> None:
        _snapshots[(clinic, version)] = snapshot
        _snapshots.move_to_end((clinic, version))
        while len(_snapshots) > LOCAL_SNAPSHOTS:
            _snapshots.popitem(last=False)

    # ── Mutable destination resolution ────────────────────────────────────
    async def get_destination(self, destination: InboundDestination) -> ClinicScope | None:
        key = destination_key(destination)
        if self.redis is None:
            entry = _destinations.get(key)
            if entry and entry[0] > time.monotonic():
                event("cache_hit", tier="local", kind="destination")
                return entry[1]
            return None
        raw = await self._safe("get_destination", self.redis.get(key))
        if not raw:
            event("cache_miss", kind="destination")
            return None
        try:
            value = json.loads(raw)
            scope = ClinicScope(
                clinic_id=UUID(value["clinic_id"]),
                phone_number_id=UUID(value["phone_number_id"]),
                configuration_version_id=UUID(value["configuration_version_id"]),
                timezone=str(value["timezone"]),
                supported_languages=tuple(value["supported_languages"]),
            )
        except (ValueError, KeyError, TypeError):
            return None
        event("cache_hit", tier="redis", kind="destination")
        return scope

    async def put_destination(self, destination: InboundDestination, scope: ClinicScope) -> None:
        key = destination_key(destination)
        if self.redis is None:
            _destinations[key] = (time.monotonic() + LOCAL_DESTINATION_TTL_SECONDS, scope)
            return
        value = json.dumps({
            "clinic_id": str(scope.clinic_id),
            "phone_number_id": str(scope.phone_number_id),
            "configuration_version_id": str(scope.configuration_version_id),
            "timezone": scope.timezone,
            "supported_languages": list(scope.supported_languages),
        })
        index = destinations_index_key(scope.clinic_id)

        async def write() -> None:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(key, value, ex=DESTINATION_TTL_SECONDS)
                pipe.sadd(index, key)
                pipe.expire(index, DESTINATION_TTL_SECONDS * 2)
                await pipe.execute()

        await self._safe("put_destination", write())

    async def invalidate_clinic(self, clinic: UUID) -> None:
        """After publish/rollback: new calls must resolve the new active version."""
        for key, (_, scope) in list(_destinations.items()):
            if scope.clinic_id == clinic:
                _destinations.pop(key, None)
        if self.redis is None:
            return
        index = destinations_index_key(clinic)

        async def drop() -> None:
            keys = await self.redis.smembers(index)
            await self.redis.delete(index, *keys)

        await self._safe("invalidate_clinic", drop())
        event("cache_invalidated", clinic_id=clinic)

    # ── Per-clinic concurrent-call slots ─────────────────────────────────
    async def acquire_call_slot(
        self, clinic: UUID, member: str, *, limit: int, ttl_seconds: int
    ) -> bool | None:
        """True if admitted, False if over the limit, None if the limit cannot be checked.

        A sorted set scored by start time: entries of crashed calls age out after
        ``ttl_seconds`` (longer than the maximum call duration), so no slot leaks forever.
        """
        if self.redis is None or limit <= 0:
            return None
        key, now = active_calls_key(clinic), time.time()

        async def claim() -> int:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, "-inf", now - ttl_seconds)
                pipe.zadd(key, {member: now})
                pipe.zcard(key)
                pipe.expire(key, ttl_seconds)
                results = await pipe.execute()
            return int(results[2])

        active = await self._safe("acquire_call_slot", claim())
        if active is None:
            return None
        if active > limit:
            await self._safe("release_call_slot", self.redis.zrem(key, member))
            return False
        return True

    async def release_call_slot(self, clinic: UUID, member: str) -> None:
        if self.redis is not None:
            await self._safe("release_call_slot", self.redis.zrem(active_calls_key(clinic), member))

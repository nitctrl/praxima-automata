"""Shared cache: tenant-keyed, fail-open to Postgres, publish invalidation, call slots."""

import asyncio
import logging
import shutil
import socket
import subprocess
import time
from uuid import UUID

import pytest
from test_structured_knowledge import content  # noqa: F401

from clinic import observability
from clinic.cache import Cache, clear_local, snapshot_key
from clinic.resolver import ClinicScope, InboundDestination
from clinic.snapshot import Snapshot

CLINIC_A, CLINIC_B = UUID(int=0xA), UUID(int=0xB)
VERSION = UUID(int=0xA1)
DEST = InboundDestination("+918000000001", "ST_a")
SCOPE = ClinicScope(CLINIC_A, UUID(int=1), VERSION, "Asia/Kolkata", ("en-IN",))


@pytest.fixture(scope="module")
def redis_url():
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server not installed")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [binary, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"redis://127.0.0.1:{port}/0"
    process.terminate()
    process.wait(5)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    clear_local()
    monkeypatch.setenv("REDIS_TIMEOUT_SECONDS", "0.5")


def snapshot_for(content, clinic):  # noqa: F811
    return Snapshot.model_validate({**content, "clinic_id": str(clinic)})


def run(coro):
    return asyncio.run(coro)


def test_snapshot_round_trips_through_redis(monkeypatch, redis_url, content):  # noqa: F811
    monkeypatch.setenv("REDIS_URL", redis_url)
    snapshot = snapshot_for(content, CLINIC_A)

    async def scenario():
        cache = Cache.from_environment()
        await cache.put_snapshot(CLINIC_A, VERSION, snapshot)
        clear_local()  # another worker process
        found = await cache.get_snapshot(CLINIC_A, VERSION)
        await cache.aclose()
        return found

    assert run(scenario()) == snapshot


def test_snapshot_of_another_clinic_is_never_served(monkeypatch, redis_url, content):  # noqa: F811
    monkeypatch.setenv("REDIS_URL", redis_url)

    async def scenario():
        cache = Cache.from_environment()
        # Poisoned/misfiled entry under clinic A's key carrying clinic B's facts.
        await cache.redis.set(
            snapshot_key(CLINIC_A, VERSION), snapshot_for(content, CLINIC_B).model_dump_json()
        )
        found = await cache.get_snapshot(CLINIC_A, VERSION)
        # Writes for a mismatched clinic are refused too.
        await cache.put_snapshot(CLINIC_A, UUID(int=9), snapshot_for(content, CLINIC_B))
        other = await cache.get_snapshot(CLINIC_A, UUID(int=9))
        await cache.aclose()
        return found, other

    assert run(scenario()) == (None, None)


def test_publish_invalidates_only_that_clinics_destinations(monkeypatch, redis_url):
    monkeypatch.setenv("REDIS_URL", redis_url)
    other = InboundDestination("+918000000002", "ST_b")
    other_scope = ClinicScope(CLINIC_B, UUID(int=2), UUID(int=0xB1), "Asia/Kolkata", ("en-IN",))

    async def scenario():
        cache = Cache.from_environment()
        await cache.put_destination(DEST, SCOPE)
        await cache.put_destination(other, other_scope)
        assert await cache.get_destination(DEST) == SCOPE
        await cache.invalidate_clinic(CLINIC_A)
        result = await cache.get_destination(DEST), await cache.get_destination(other)
        await cache.aclose()
        return result

    assert run(scenario()) == (None, other_scope)


def test_concurrent_call_slots_enforce_the_per_clinic_limit(monkeypatch, redis_url):
    monkeypatch.setenv("REDIS_URL", redis_url)

    async def scenario():
        cache = Cache.from_environment()
        first = await cache.acquire_call_slot(CLINIC_A, "c1", limit=2, ttl_seconds=60)
        second = await cache.acquire_call_slot(CLINIC_A, "c2", limit=2, ttl_seconds=60)
        third = await cache.acquire_call_slot(CLINIC_A, "c3", limit=2, ttl_seconds=60)
        other_clinic = await cache.acquire_call_slot(CLINIC_B, "c4", limit=2, ttl_seconds=60)
        await cache.release_call_slot(CLINIC_A, "c1")
        after_release = await cache.acquire_call_slot(CLINIC_A, "c5", limit=2, ttl_seconds=60)
        await cache.aclose()
        return first, second, third, other_clinic, after_release

    assert run(scenario()) == (True, True, False, True, True)


def test_crashed_call_slots_age_out(monkeypatch, redis_url):
    monkeypatch.setenv("REDIS_URL", redis_url)

    async def scenario():
        cache = Cache.from_environment()
        await cache.acquire_call_slot(UUID(int=0xC), "old", limit=1, ttl_seconds=1)
        await asyncio.sleep(1.1)
        admitted = await cache.acquire_call_slot(UUID(int=0xC), "new", limit=1, ttl_seconds=1)
        await cache.aclose()
        return admitted

    assert run(scenario()) is True


def test_redis_down_is_a_cache_miss_not_a_failure(monkeypatch, caplog):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
    monkeypatch.setenv("REDIS_TIMEOUT_SECONDS", "0.1")

    async def scenario():
        cache = Cache.from_environment()
        await cache.put_destination(DEST, SCOPE)
        found = await cache.get_destination(DEST)
        slot = await cache.acquire_call_slot(CLINIC_A, "x", limit=1, ttl_seconds=60)
        await cache.invalidate_clinic(CLINIC_A)
        await cache.aclose()
        return found, slot

    with caplog.at_level(logging.WARNING, logger="clinic.metrics"):
        assert run(scenario()) == (None, None)
    assert any(getattr(r, "event", "") == "cache_error" for r in caplog.records)


def test_without_redis_destinations_use_a_short_local_ttl(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)

    async def scenario():
        cache = Cache.from_environment()
        await cache.put_destination(DEST, SCOPE)
        hit = await cache.get_destination(DEST)
        await cache.invalidate_clinic(CLINIC_A)
        return hit, await cache.get_destination(DEST)

    assert run(scenario()) == (SCOPE, None)


def test_logs_mask_phone_numbers_and_carry_call_context():
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "caller %s", ("+919876543210",), None
    )
    observability.bind(correlation_id="room-1", clinic_id=CLINIC_A)
    observability.ContextFilter().filter(record)
    assert record.getMessage() == "caller ***3210"
    assert record.correlation_id == "room-1"
    assert record.clinic_id == str(CLINIC_A)


def test_mask_keeps_dates_and_uuids():
    text = f"date=2026-09-17 clinic={CLINIC_A} count=42"
    assert observability.mask(text) == text


def test_json_logs_omit_exception_messages():
    try:
        raise ValueError("patient said +919876543210")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    output = observability.JsonFormatter().format(record)
    assert "ValueError" in output and "patient" not in output

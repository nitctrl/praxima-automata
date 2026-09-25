import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from test_dev_voice import metric

from praxima.dev.development import fixture_id
from praxima.modules.engagement.application.sessions import CallSessionService
from praxima.runtime.usage import UsageCollector


class ConnectionDatabase:
    def __init__(self, connection):
        self.conn = connection

    @asynccontextmanager
    async def connection(self, clinic):
        yield self.conn


@pytest.mark.parametrize("seconds", [0, 0.0, 1.5])
def test_usage_duration_is_bound_as_postgres_numeric(seconds):
    connection = SimpleNamespace(execute=AsyncMock())
    service = CallSessionService(ConnectionDatabase(connection))
    context = SimpleNamespace(session_id=uuid4(), scope=SimpleNamespace(clinic_id=uuid4()))
    asyncio.run(service.usage(context, uuid4(), stt_seconds=seconds))
    args = connection.execute.await_args.args[1]
    assert isinstance(args[4], Decimal)
    assert args[4] == Decimal(str(seconds))


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1.0])
def test_invalid_usage_never_reaches_database(seconds):
    connection = SimpleNamespace(execute=AsyncMock())
    service = CallSessionService(ConnectionDatabase(connection))
    with pytest.raises(ValueError):
        asyncio.run(service.usage(None, uuid4(), stt_seconds=seconds))
    connection.execute.assert_not_awaited()


@pytest.mark.integration
def test_actual_service_and_collector_persist_speech_usage_without_stopping_call(db):
    clinic = fixture_id("A")
    version = db.execute(
        "SELECT active_configuration_version_id FROM clinics WHERE id=%s", (clinic,),
    ).fetchone()[0]
    phone = db.execute(
        "SELECT id FROM phone_numbers WHERE clinic_id=%s AND provider='test'", (clinic,),
    ).fetchone()[0]
    db.execute("SET LOCAL ROLE clinic_runtime")
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(clinic),))
    value = db.execute(
        "SELECT clinic_private.start_call(%s,%s,'test','rollback-usage',%s,%s,true)",
        (phone, version, str(uuid4()), str(uuid4())),
    ).fetchone()[0]

    # Adapt the rollback fixture's synchronous connection to the service interface.
    # All SQL and parameter adaptation still execute against real PostgreSQL.
    async def execute(sql, params):
        return db.execute(sql, params)

    context = SimpleNamespace(
        session_id=UUID(value["id"]), scope=SimpleNamespace(clinic_id=clinic),
    )
    service = CallSessionService(ConnectionDatabase(SimpleNamespace(execute=execute)))

    async def exercise():
        call = SimpleNamespace(context=context, service=service, stop_event=asyncio.Event())
        collector = UsageCollector(call)
        for event in [metric("tts"), metric("stt", audio_duration=1.5)]:
            collector.submit(event)
            collector.submit(event)  # Duplicate SDK delivery remains idempotent.
        await collector.close()
        assert not call.stop_event.is_set(), "Speech metrics must not terminate the call"

    asyncio.run(exercise())
    db.execute("RESET ROLE")
    rows = db.execute(
        "SELECT stt_seconds,tts_characters FROM usage_records WHERE call_session_id=%s",
        (context.session_id,),
    ).fetchall()
    assert len(rows) == 2
    assert sum(row[0] for row in rows) == Decimal("1.5")
    assert sum(row[1] for row in rows) > 0
"""Call record adapter: idempotent lifecycle, plan-limit mapping and usage keys."""

import asyncio
import contextlib
from datetime import datetime, timezone
from uuid import UUID

import psycopg
import pytest
from livekit.agents.metrics import EOUMetrics, LLMMetrics

from clinic.calls import CallLimitReached, CallRecord, CallRef, UsageRecorder, usage_event
from clinic.resolver import ClinicScope

CLINIC = UUID(int=0xA)
SCOPE = ClinicScope(CLINIC, UUID(int=1), UUID(int=2), "Asia/Kolkata", ("en-IN",))
REF = CallRef("plivo", "ST_a", "SCL_1", "room")


class Limit(psycopg.Error):
    sqlstate = "54000"


class FakeDatabase:
    def __init__(self, row=None, error=None):
        self.row, self.error, self.calls = row, error, []

    @contextlib.asynccontextmanager
    async def connection(self, clinic=None):
        database = self

        class Cursor:
            async def fetchone(self):
                return database.row

        class Connection:
            async def execute(self, sql, params=()):
                if database.error:
                    raise database.error
                database.calls.append((sql, params))
                return Cursor()

        assert clinic == CLINIC
        yield Connection()


def test_start_maps_plan_limits_to_call_limit():
    with pytest.raises(CallLimitReached):
        asyncio.run(CallRecord.start(FakeDatabase(error=Limit()), SCOPE, REF))


def test_start_returns_session_and_deadline_and_close_is_idempotent():
    session = UUID(int=9)
    database = FakeDatabase(
        {"context": {"id": str(session), "deadline_at": "2026-09-21T10:00:00+00:00"},
         "live": True}
    )

    async def exercise():
        record = await CallRecord.start(database, SCOPE, REF)
        assert record.session_id == session
        assert record.deadline == datetime(2026, 9, 21, 10, tzinfo=timezone.utc)
        await record.close("timeout")
        await record.close("ended")

    asyncio.run(exercise())
    start, close = database.calls
    assert start[1][2:] == ("plivo", "ST_a", "SCL_1", "room")
    assert close[1][:2] == (session, "timeout")


def llm_metric(**overrides):
    values = dict(
        label="llm", request_id="r1", timestamp=1.0, duration=0.1, ttft=0.1, cancelled=False,
        completion_tokens=5, prompt_tokens=10, prompt_cached_tokens=0, total_tokens=15,
        tokens_per_second=1.0,
    )
    values.update(overrides)
    return LLMMetrics(**values)


def test_usage_event_is_stable_for_replayed_metrics():
    session = UUID(int=3)
    first = usage_event(session, llm_metric())
    assert first is not None and first[1:] == (10, 5, 0.0, 0)
    assert usage_event(session, llm_metric()) == first
    assert usage_event(session, llm_metric(request_id="r2")) != first
    eou = EOUMetrics(
        timestamp=1.0, end_of_utterance_delay=0.1, transcription_delay=0.1,
        on_user_turn_completed_delay=0.0,
    )
    assert usage_event(session, eou) is None


def test_usage_recorder_writes_in_the_background():
    database = FakeDatabase({"live": True})
    record = CallRecord(database, CLINIC, UUID(int=4), datetime.now(timezone.utc))

    async def exercise():
        recorder = UsageRecorder(record)
        recorder.submit(llm_metric())
        await recorder.close()

    asyncio.run(exercise())
    assert "record_usage" in database.calls[0][0]

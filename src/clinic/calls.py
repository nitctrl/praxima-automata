"""Durable call record: lifecycle, fixed-vocabulary outcomes, callbacks and usage.

Thin adapter over the platform's ``clinic_private`` SQL functions (see
docs/schema-contract.md). Nothing here stores transcripts or free text from the caller
or the model; names and numbers are encrypted before they leave the process.
"""

import asyncio
import contextlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

import psycopg
from livekit.agents.metrics import AgentMetrics, LLMMetrics, STTMetrics, TTSMetrics
from psycopg.types.json import Jsonb

from clinic.db import RuntimeDatabase
from clinic.resolver import ClinicScope, ClinicUnavailable

logger = logging.getLogger(__name__)

Action = Literal["heartbeat", "ended", "timeout", "tool_failed", "transfer_failed", "safety_routed"]
Topic = Literal[
    "connected", "availability", "doctors", "hours", "fees", "location", "faq", "clarify", "voice"
]
Outcome = Literal["success", "ambiguous", "not_found", "unavailable", "forbidden", "failed"]
CallbackReason = Literal[
    "appointment", "hours", "fees", "registration", "human_requested", "other_admin"
]
CALL_TIMEOUT_SECONDS = 5


class CallLimitReached(ClinicUnavailable):
    """The clinic's concurrent-call or monthly-minute limit refuses this call."""


@dataclass(frozen=True)
class CallRef:
    """Trusted LiveKit/SIP identifiers for one call; never taken from the caller."""

    provider: str
    account: str
    call_id: str
    room: str


@dataclass
class CallRecord:
    database: RuntimeDatabase
    clinic_id: UUID
    session_id: UUID
    deadline: datetime
    _closed: bool = field(default=False, repr=False)

    @classmethod
    async def start(
        cls, database: RuntimeDatabase, scope: ClinicScope, ref: CallRef
    ) -> "CallRecord":
        """Idempotent on the provider call id; raises CallLimitReached on plan limits."""
        try:
            async with database.connection(scope.clinic_id) as conn:
                row = await (
                    await conn.execute(
                        "SELECT clinic_private.start_call(%s,%s,%s,%s,%s,%s,false) AS context",
                        (
                            scope.phone_number_id,
                            scope.configuration_version_id,
                            ref.provider,
                            ref.account,
                            ref.call_id,
                            ref.room,
                        ),
                    )
                ).fetchone()
        except psycopg.Error as exc:
            if exc.sqlstate == "54000":
                raise CallLimitReached("Clinic call limit reached") from None
            raise
        if row is None:
            raise ClinicUnavailable("Call initialization failed")
        value = row["context"]
        return cls(
            database,
            scope.clinic_id,
            UUID(value["id"]),
            datetime.fromisoformat(value["deadline_at"]),
        )

    async def event(self, action: Action, event_id: UUID | None = None) -> bool:
        """Record a lifecycle event. Returns False once the database considers it ended."""
        async with self.database.connection(self.clinic_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT clinic_private.update_call(%s,%s,%s) AS live",
                    (self.session_id, action, event_id or uuid4()),
                )
            ).fetchone()
        return bool(row and row["live"])

    async def note(self, topic: Topic, outcome: Outcome) -> None:
        async with self.database.connection(self.clinic_id) as conn:
            await conn.execute(
                "SELECT clinic_private.note_call(%s,%s,%s)", (self.session_id, topic, outcome)
            )

    async def create_callback(
        self,
        request_id: UUID,
        name_ciphertext: bytes,
        phone_ciphertext: bytes,
        key_version: str,
        details: dict[str, Any],
    ) -> UUID:
        """Idempotent on ``request_id``; staff see it as a callback request."""
        async with self.database.connection(self.clinic_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT clinic_private.create_request(%s,%s,'callback',%s,%s,%s,%s) AS id",
                    (
                        self.session_id,
                        request_id,
                        name_ciphertext,
                        phone_ciphertext,
                        key_version,
                        Jsonb(details),
                    ),
                )
            ).fetchone()
        if not row:
            raise ClinicUnavailable("Request persistence unavailable")
        return UUID(str(row["id"]))

    async def usage(
        self,
        event_id: UUID,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        stt_seconds: float = 0,
        tts_characters: int = 0,
    ) -> None:
        async with self.database.connection(self.clinic_id) as conn:
            await conn.execute(
                "SELECT clinic_private.record_usage(%s,%s,%s,%s,%s,%s)",
                (
                    self.session_id,
                    event_id,
                    input_tokens,
                    output_tokens,
                    # psycopg binds float as float8, not the function's numeric argument.
                    Decimal(str(stt_seconds)),
                    tts_characters,
                ),
            )

    async def close(self, reason: Literal["ended", "timeout"] = "ended") -> None:
        """Idempotent and bounded; the platform reconciles calls whose close was lost."""
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.wait_for(self.event(reason), CALL_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("Call record close failed (%s)", type(exc).__name__)


def usage_event(session: UUID, metric: AgentMetrics) -> tuple[UUID, int, int, float, int] | None:
    """Billable units of one SDK metric, keyed so a replayed metric is counted once."""
    if not isinstance(metric, (LLMMetrics, STTMetrics, TTSMetrics)):
        return None
    input_tokens = metric.prompt_tokens if isinstance(metric, LLMMetrics) else 0
    output_tokens = metric.completion_tokens if isinstance(metric, LLMMetrics) else 0
    seconds = metric.audio_duration if isinstance(metric, STTMetrics) else 0.0
    chars = metric.characters_count if isinstance(metric, TTSMetrics) else 0
    values = (input_tokens, output_tokens, seconds, chars)
    if any(not math.isfinite(value) or value < 0 for value in values):
        return None
    identity = json.dumps(
        [
            metric.type,
            metric.label,
            metric.request_id,
            metric.timestamp,
            getattr(metric, "segment_id", None),
            values,
        ]
    )
    return uuid5(session, identity), input_tokens, output_tokens, seconds, chars


class UsageRecorder:
    """Bounded background writer; usage loss never affects the conversation."""

    def __init__(self, record: CallRecord) -> None:
        self.record = record
        self.queue: asyncio.Queue[tuple[UUID, int, int, float, int]] = asyncio.Queue(maxsize=256)
        self.worker = asyncio.create_task(self._consume(), name="clinic-usage")

    def submit(self, metric: AgentMetrics) -> None:
        item = usage_event(self.record.session_id, metric)
        if item is None:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("Usage queue full; dropping one usage event")

    async def _consume(self) -> None:
        while True:
            event_id, inputs, outputs, seconds, chars = await self.queue.get()
            try:
                await asyncio.wait_for(
                    self.record.usage(
                        event_id,
                        input_tokens=inputs,
                        output_tokens=outputs,
                        stt_seconds=seconds,
                        tts_characters=chars,
                    ),
                    CALL_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logger.warning("Usage write failed (%s)", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.queue.join(), CALL_TIMEOUT_SECONDS)
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)

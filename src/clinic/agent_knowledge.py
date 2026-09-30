"""Published knowledge, appointment slots, booking and callback tools for one call."""

import asyncio
import base64
import contextlib
import logging
import os
import re
from collections.abc import Coroutine
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

from dotenv import dotenv_values
from livekit.agents import function_tool
from livekit.agents.metrics import AgentMetrics
from psycopg.types.json import Jsonb

from clinic.cache import Cache
from clinic.calls import (
    CallbackReason,
    CallLimitReached,
    CallRecord,
    CallRef,
    Outcome,
    Topic,
    UsageRecorder,
)
from clinic.db import RuntimeDatabase
from clinic.knowledge import Query, Result, StructuredKnowledge
from clinic.observability import bind, event, timed
from clinic.privacy import PiiCipher
from clinic.prompt import render_prompt
from clinic.rag import HybridRetriever
from clinic.resolver import (
    ClinicResolver,
    ClinicScope,
    ClinicUnavailable,
    ConfigurationRepository,
    InboundDestination,
    PostgresResolutionRepository,
)
from clinic.settings import DatabaseSettings
from clinic.snapshot import Snapshot
from clinic.vectors import VectorSearch

logger = logging.getLogger(__name__)
KNOWLEDGE_LOAD_TIMEOUT_SECONDS = 20
KNOWLEDGE_CLOSE_TIMEOUT_SECONDS = 2
CALLER_NUMBER = re.compile(r"^\+?[1-9][0-9]{7,14}$")
# Longer than any call, so slots of crashed workers age out on their own.
SLOT_TTL_SECONDS = 2 * 3600
# Why a call has no clinic knowledge; the entrypoint speaks a fixed message and hangs up.
Failure = Literal["", "not_configured", "busy", "unavailable"]


def max_concurrent_calls() -> int:
    """Per-clinic concurrent-call ceiling; 0 disables it. Enforced only with Redis.

    The platform's start_call currently also enforces 4; keep the two aligned.
    """
    try:
        return int(os.environ.get("CLINIC_MAX_CONCURRENT_CALLS", "4"))
    except ValueError:
        return 4


def console_clinic() -> UUID | None:
    """Clinic for local console sessions, which have no dialled number.

    Never used for SIP calls: those resolve their clinic from the trusted destination.
    Unset in production, so a non-SIP participant gets no clinic knowledge.
    """
    value = os.environ.get("CONSOLE_CLINIC_ID", "").strip()
    return UUID(value) if value else None


def database_settings(root: Path) -> DatabaseSettings:
    """Process environment first (deployments); local ignored dotenv files as fallback."""
    dsn = os.environ.get("DATABASE_URL") or dotenv_values(root / ".env.runtime").get(
        "DATABASE_URL"
    )
    project = os.environ.get("SUPABASE_PROJECT_REF") or dotenv_values(root / ".env").get(
        "SUPABASE_PROJECT_REF"
    )
    return DatabaseSettings.validate(dsn or "", project or "")


class AgentKnowledge:
    """Pinned public snapshot exposed to the model through typed tools."""

    def __init__(
        self,
        snapshot: Snapshot | None,
        version: UUID | None = None,
        database: RuntimeDatabase | None = None,
        *,
        clinic: UUID | None = None,
        failure: Failure = "",
    ) -> None:
        # The backend-resolved tenant must own the snapshot; there is no default clinic.
        if snapshot is not None and (clinic is None or snapshot.clinic_id != clinic):
            raise ClinicUnavailable("Wrong clinic")
        self.snapshot = snapshot
        self.version = version
        self.database = database
        self.failure: Failure = "" if snapshot is not None else (failure or "unavailable")
        self.structured = StructuredKnowledge(snapshot) if snapshot else None
        # Backend-derived caller ID; the model may not set or override it.
        self.caller_number = ""
        vectors = VectorSearch.from_environment() if snapshot is not None else None
        self.retriever = HybridRetriever(snapshot, version, vectors) if snapshot else None
        self.vectors = vectors
        self._warm: asyncio.Task[None] | None = None
        # Concurrent-call slot held for this call, released in aclose.
        self.cache: Cache | None = None
        self.slot = ""
        self.scope: ClinicScope | None = None
        self._record: asyncio.Task[CallRecord | None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self.usage: UsageRecorder | None = None
        self._pending_usage: list[AgentMetrics] = []
        self.close_reason: Literal["ended", "timeout"] = "ended"
        try:
            self.cipher: PiiCipher | None = PiiCipher.from_environment() if snapshot else None
        except ValueError:
            logger.warning("Clinic encryption keys missing; booking is disabled")
            self.cipher = None

    # ── Call record ────────────────────────────────────────────────────

    def start_call_record(self, ref: CallRef) -> None:
        """Create the durable call record in the background; the greeting never waits."""
        if self.scope is None or self.database is None or self._record is not None:
            return
        database, scope = self.database, self.scope

        async def start() -> CallRecord | None:
            try:
                with timed("call_record"):
                    record = await CallRecord.start(database, scope, ref)
                self.usage = UsageRecorder(record)
                for metric in self._pending_usage:
                    self.usage.submit(metric)
                self._pending_usage.clear()
                return record
            except CallLimitReached:
                event("quota_exceeded", level=logging.WARNING, clinic_id=scope.clinic_id,
                      kind="call_limit")
                raise
            except Exception as exc:
                # Degraded: the conversation continues; callbacks are unavailable.
                logger.warning("Call record unavailable (%s)", type(exc).__name__)
                return None

        self._record = asyncio.create_task(start(), name="clinic-call-record")

    async def call_record(self) -> CallRecord | None:
        """The call record, or None if it could not be created. Raises CallLimitReached."""
        if self._record is None:
            return None
        return await asyncio.shield(self._record)

    def submit_usage(self, metric: AgentMetrics) -> None:
        """Billable usage; buffered (bounded) until the call record exists."""
        if self.usage is not None:
            self.usage.submit(metric)
        elif self._record is not None and not self._record.done():
            if len(self._pending_usage) < 256:
                self._pending_usage.append(metric)

    def _later(self, work: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def note(self, topic: Topic, outcome: Outcome) -> None:
        """Fire-and-forget fixed-vocabulary outcome for staff; never blocks a reply."""

        async def write() -> None:
            with contextlib.suppress(Exception):
                record = await self.call_record()
                if record is not None:
                    await asyncio.wait_for(record.note(topic, outcome), 5)

        if self._record is not None:
            self._later(write())

    def safety_routed(self) -> None:
        async def write() -> None:
            with contextlib.suppress(Exception):
                record = await self.call_record()
                if record is not None:
                    await asyncio.wait_for(record.event("safety_routed"), 5)

        if self._record is not None:
            self._later(write())

    async def aclose(self) -> None:
        if self._background:
            await asyncio.wait(list(self._background), timeout=3)
        if self._record is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                record = await asyncio.wait_for(asyncio.shield(self._record), 5)
                if self.usage is not None:
                    await self.usage.close()
                if record is not None:
                    await record.close(self.close_reason)
        if self.vectors is not None:
            await self.vectors.aclose()
        if self.database is not None:
            await self.database.close()
        if self.cache is not None:
            if self.slot and self.snapshot is not None:
                await self.cache.release_call_slot(self.snapshot.clinic_id, self.slot)
            await self.cache.aclose()

    # ── Prompt and tools ───────────────────────────────────────────────

    @property
    def instructions(self) -> str:
        if self.snapshot is None:
            return (
                "Clinic knowledge is unavailable. Do not invent clinic facts. "
                "Explain briefly that published information cannot be reached right now."
            )
        return render_prompt(self.snapshot)

    def function_tools(self) -> list[Any]:
        # Without a pinned snapshot there are no hours to slice, so offer no booking tools.
        if self.snapshot is None:
            return [self.search_clinic_knowledge]
        return [
            self.search_clinic_knowledge,
            self.list_available_slots,
            self.book_appointment_slot,
            self.request_callback,
        ]

    @function_tool()
    async def search_clinic_knowledge(self, question: str) -> dict[str, object]:
        """Hybrid-search reviewed documents and current or scheduled live updates.

        Call this for clinic-information questions such as doctors, opening hours, fees,
        locations, services, qualifications and daily changes. It cannot answer which
        appointment slots are free: use `list_available_slots` for that.
        Write `question` in English, translating the caller faithfully and keeping every
        name, date, time and range they said.
        """
        if self.retriever is None:
            return {"status": "unavailable", "data": {"passages": []}}
        result = await self.retriever.result(question)
        self.note("faq", "success" if result.get("status") == "success" else "unavailable")
        return result

    async def _open_slots(
        self, requested_date: str, doctor: str, service: str, location: str
    ) -> tuple[Result, Any, UUID | None, list[str]]:
        """Published hours for one doctor and date, minus slots already booked."""
        if self.structured is None or self.database is None or self.snapshot is None:
            logger.warning("Slot lookup skipped: clinic knowledge unavailable")
            return Result(status="unavailable", next_action="contact_reception"), None, None, []
        try:
            day = self.structured.day(requested_date.strip().lower())
            # Keep today's windows whole so the slot grid matches every other call.
            found = self.structured.availability(
                Query(
                    requested_date=day.isoformat(),
                    doctor=doctor,
                    service=service,
                    location=location,
                ),
                trim_past=False,
            )
        except ValueError:
            logger.info("Slot lookup rejected unusable input")
            return Result(status="unavailable", next_action="ask_for_clarification"), None, None, []
        if found.status != "success":
            logger.info("Slot lookup on %s: %s", day, found.status)
            self.note("availability", found.status)
            return found, day, None, []
        reference = UUID(found.data["doctor"]["reference"])
        try:
            with timed("slots_db"):
                async with self.database.connection(self.snapshot.clinic_id) as conn:
                    row = await (
                        await conn.execute(
                            "SELECT clinic_private.booked_slots(%s,%s) AS taken",
                            (day, reference),
                        )
                    ).fetchone()
        except Exception as exc:
            logger.warning("Slot lookup failed (%s)", type(exc).__name__)
            self.note("availability", "failed")
            return Result(status="failed", next_action="offer_callback"), None, None, []
        taken = set(row["taken"]) if row else set()
        free = self.structured.slot_times(
            found.data["hours"], taken, not_before=self.structured.now()
        )
        logger.info("Slot lookup on %s: %d free, %d taken", day, len(free), len(taken))
        self.note("availability", "success" if free else "unavailable")
        return found, day, reference, free

    @function_tool()
    async def list_available_slots(
        self,
        requested_date: str = "today",
        doctor: str = "",
        service: str = "",
        location: str = "",
    ) -> dict[str, object]:
        """List appointment slots that are still free on one date.

        Call this before offering a time or booking. `requested_date` accepts "today",
        "tomorrow" or YYYY-MM-DD. Use doctor, service and location names exactly as
        published. Leave `location` empty unless the clinic has several and the caller
        chose one. Offer only the returned times; never invent one.
        """
        found, day, _, free = await self._open_slots(requested_date, doctor, service, location)
        if found.status != "success" or day is None or self.snapshot is None:
            return found.model_dump(mode="json")
        return {
            "status": "success" if free else "unavailable",
            "data": {
                "date": day.isoformat(),
                "weekday": day.strftime("%A"),
                "doctor": found.data["doctor"]["name"],
                "location": found.data["location"]["name"],
                "slot_minutes": self.snapshot.slot_minutes,
                "free_slots": free,
                "notices": found.data["notices"],
            },
            "next_action": "offer_slots" if free else "offer_other_date_or_callback",
        }

    @function_tool()
    async def book_appointment_slot(
        self,
        requested_date: str,
        start_time: str,
        patient_name: str,
        doctor: str = "",
        service: str = "",
        location: str = "",
        callback_number: str = "",
    ) -> dict[str, object]:
        """Book one free slot returned by list_available_slots.

        Read the doctor, date, time and name back to the caller and get agreement first.
        `start_time` must be exactly one of the free slots (HH:MM). Leave
        `callback_number` empty to use the number the caller is dialling from; fill it
        only after reading a different number back digit by digit.
        """
        found, day, reference, free = await self._open_slots(
            requested_date, doctor, service, location
        )
        if found.status != "success" or day is None or reference is None:
            return found.model_dump(mode="json")
        label = start_time.strip()[:5]
        if label not in free:
            return {
                "status": "unavailable",
                "data": {"free_slots": free},
                "next_action": "offer_slots",
            }
        number = (callback_number or self.caller_number).strip().replace(" ", "")
        name = " ".join(patient_name.split())[:120]
        if not name or not CALLER_NUMBER.fullmatch(number):
            return {"status": "unavailable", "data": {}, "next_action": "ask_name_and_number"}
        if self.cipher is None or self.snapshot is None or self.database is None:
            return {"status": "unavailable", "data": {}, "next_action": "offer_callback"}

        clinic, minutes = self.snapshot.clinic_id, self.snapshot.slot_minutes
        # Same caller, same slot, same day always resolves to the same booking.
        key = uuid5(clinic, f"{clinic}:{day}:{reference}:{label}:{number}")
        start = time.fromisoformat(label)
        end = (datetime.combine(day, start) + timedelta(minutes=minutes)).time()
        try:
            return await self._book(
                found, day, reference, label, free, name, number, key, start, end
            )
        except Exception as exc:
            logger.warning("Booking failed (%s)", type(exc).__name__)
            return {"status": "failed", "data": {}, "next_action": "offer_callback"}

    async def _book(
        self,
        found: Result,
        day: Any,
        reference: UUID,
        label: str,
        free: list[str],
        name: str,
        number: str,
        key: UUID,
        start: time,
        end: time,
    ) -> dict[str, object]:
        assert self.cipher is not None and self.snapshot is not None and self.database
        clinic, minutes = self.snapshot.clinic_id, self.snapshot.slot_minutes
        encrypt = self.cipher.encrypt
        async with self.database.connection(clinic) as conn:
            row = await (
                await conn.execute(
                    "SELECT clinic_private.book_calendar_slot"
                    "(%s,%s,%s,%s,%s,NULL,%s,%s,%s,'agent') AS booking",
                    (
                        key,
                        day,
                        start,
                        end,
                        reference,
                        encrypt(name, clinic, key, "patient_name"),
                        encrypt(number, clinic, key, "callback_number"),
                        self.cipher.current,
                    ),
                )
            ).fetchone()
            booking = dict(row["booking"]) if row else {}
            if booking.get("status") != "booked":
                return {
                    "status": "unavailable",
                    "data": {"free_slots": [slot for slot in free if slot != label]},
                    "next_action": "offer_slots",
                }
            who = found.data["doctor"]["name"]
            when = f"{day.isoformat()} at {label}"
            messages = [
                {
                    "audience": "caller",
                    "recipient": base64.b64encode(
                        encrypt(number, clinic, key, "whatsapp_recipient")
                    ).decode(),
                    "key_version": self.cipher.current,
                    "body": f"{booking['clinic_name']}: your appointment with {who} is booked "
                    f"for {when} ({minutes} minutes). Reply to this message to change it.",
                }
            ]
            if booking.get("clinic_whatsapp"):
                messages.append(
                    {
                        "audience": "clinic",
                        "recipient": base64.b64encode(
                            encrypt(
                                booking["clinic_whatsapp"], clinic, key, "whatsapp_recipient"
                            )
                        ).decode(),
                        "key_version": self.cipher.current,
                        "body": f"New booking: {who}, {when}. Patient {name}, {number}.",
                    }
                )
            # Outbox only: the platform sends and retries notifications.
            await conn.execute(
                "SELECT clinic_private.queue_whatsapp(%s,%s)", (key, Jsonb(messages))
            )
        return {
            "status": "success",
            "data": {
                "reference": str(key)[:8],
                "doctor": who,
                "date": day.isoformat(),
                "weekday": day.strftime("%A"),
                "start_time": label,
                "slot_minutes": minutes,
                "confirmation_message": "queued",
            },
            "next_action": "read_back_confirmation",
        }

    @function_tool()
    async def request_callback(
        self,
        caller_name: str,
        reason: CallbackReason = "human_requested",
        callback_number: str = "",
    ) -> dict[str, object]:
        """Ask clinic staff to call the caller back.

        Use when the caller wants a person, when a tool failed, or when their request
        cannot be handled here. Read the name back and get agreement first. Leave
        `callback_number` empty to use the number the caller is dialling from.
        """
        name = " ".join(caller_name.split())[:100]
        number = (callback_number or self.caller_number).strip().replace(" ", "")
        if not name or not CALLER_NUMBER.fullmatch(number):
            return {"status": "unavailable", "data": {}, "next_action": "ask_name_and_number"}
        if not number.startswith("+"):
            return {"status": "unavailable", "data": {}, "next_action": "ask_full_number"}
        try:
            record = await self.call_record()
        except Exception:
            record = None
        if record is None or self.cipher is None:
            return {"status": "unavailable", "data": {}, "next_action": "ask_to_call_again"}
        # One request per caller intent within a call, even if the tool is retried.
        request_id = uuid5(record.session_id, f"callback:{name}:{number}")
        encrypt, tenant = self.cipher.encrypt, record.clinic_id
        try:
            with timed("callback_db"):
                await asyncio.wait_for(
                    record.create_callback(
                        request_id,
                        encrypt(name, tenant, request_id, "name"),
                        encrypt(number, tenant, request_id, "phone"),
                        self.cipher.current,
                        {"reason_category": reason},
                    ),
                    8,
                )
        except Exception as exc:
            logger.warning("Callback request failed (%s)", type(exc).__name__)
            return {"status": "failed", "data": {}, "next_action": "ask_to_call_again"}
        return {
            "status": "success",
            "data": {"reference": str(request_id)[:8], "confirmed_by_staff": False},
            "next_action": "tell_caller_staff_will_call_back",
        }


async def load_agent_knowledge(
    root: Path,
    destination: InboundDestination | None = None,
    call: CallRef | None = None,
) -> AgentKnowledge:
    """Resolve the tenant and load and pin its one published snapshot for this call.

    SIP calls pass the trusted destination; the database maps it to exactly one active
    clinic and its active version, or the call gets no clinic knowledge (fail closed).
    Only a local console session (``destination is None``) uses ``console_clinic``.

    The pool stays open for the lifetime of the call so a booking tool never pays
    connection setup mid-conversation. Close it with `AgentKnowledge.aclose`.
    """

    async def load() -> AgentKnowledge:
        database = RuntimeDatabase(database_settings(root))
        cache = Cache.from_environment()
        slot = ""
        scope: ClinicScope | None = None
        clinic: UUID | None = None
        try:
            # Connect in parallel with cache lookups; hits never wait for Postgres.
            database.open_in_background()
            if destination is not None:
                with timed("resolve"):
                    scope = await cache.get_destination(destination)
                    if scope is None:
                        await database.ready()
                        scope = await ClinicResolver(
                            PostgresResolutionRepository(database)
                        ).resolve(destination)
                        await cache.put_destination(destination, scope)
                clinic, version = scope.clinic_id, scope.configuration_version_id
                slot = str(uuid4())
                admitted = await cache.acquire_call_slot(
                    clinic, slot, limit=max_concurrent_calls(), ttl_seconds=SLOT_TTL_SECONDS
                )
                if admitted is False:
                    event("quota_exceeded", level=logging.WARNING, clinic_id=clinic,
                          kind="concurrent_calls")
                    raise CallLimitReached("Concurrent call limit reached")
                with timed("snapshot"):
                    snapshot = await cache.get_snapshot(clinic, version)
                    if snapshot is None:
                        await database.ready()
                        snapshot = Snapshot.model_validate(
                            await ConfigurationRepository(database, scope).load()
                        )
                        await cache.put_snapshot(clinic, version, snapshot)
                payload: Any = snapshot
            else:
                clinic = console_clinic()
                if clinic is None:
                    raise ClinicUnavailable("Console clinic not configured")
                await database.ready()
                async with database.connection(clinic) as conn:
                    await conn.execute("SET TRANSACTION READ ONLY")
                    rows = await (await conn.execute(
                        "SELECT id,snapshot FROM public.configuration_versions "
                        "WHERE clinic_id=%s AND status='published'", (clinic,),
                    )).fetchall()
                if len(rows) != 1:
                    raise ClinicUnavailable("One published configuration required")
                version, payload = rows[0]["id"], rows[0]["snapshot"]
            knowledge = AgentKnowledge(
                payload if isinstance(payload, Snapshot) else Snapshot.model_validate(payload),
                version,
                database,
                clinic=clinic,
            )
            knowledge.cache, knowledge.slot, knowledge.scope = cache, slot, scope
            bind(clinic_id=clinic, configuration_version_id=version)
            logger.info("Clinic resolved clinic=%s version=%s", clinic, version)
        except BaseException:
            try:
                await asyncio.wait_for(database.close(), KNOWLEDGE_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("Clinic knowledge database cleanup did not finish")
            if slot and clinic is not None:
                with contextlib.suppress(Exception):
                    await cache.release_call_slot(clinic, slot)
            await cache.aclose()
            raise
        if knowledge.vectors is not None:
            knowledge._warm = asyncio.create_task(knowledge.vectors.warm())
        if call is not None:
            knowledge.start_call_record(call)
        return knowledge

    try:
        return await asyncio.wait_for(load(), KNOWLEDGE_LOAD_TIMEOUT_SECONDS)
    except CallLimitReached:
        return AgentKnowledge(None, failure="busy")
    except ClinicUnavailable as exc:
        logger.warning("Clinic unavailable for this call (%s)", exc)
        return AgentKnowledge(None, failure="not_configured")
    except Exception as exc:
        logger.warning("Clinic knowledge load failed (%s)", type(exc).__name__)
        return AgentKnowledge(None, failure="unavailable")

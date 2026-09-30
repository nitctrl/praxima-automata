"""Published knowledge, appointment slots and booking tools for the voice receptionist."""

import asyncio
import base64
import contextlib
import logging
import os
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from dotenv import dotenv_values
from livekit.agents import function_tool
from psycopg.types.json import Jsonb

from clinic.cache import Cache
from clinic.db import RuntimeDatabase
from clinic.development import fixture_id
from clinic.knowledge import Query, Result, StructuredKnowledge
from clinic.observability import bind, event, timed
from clinic.privacy import PiiCipher
from clinic.prompt import render_prompt
from clinic.rag import HybridRetriever
from clinic.resolver import (
    ClinicResolver,
    ClinicUnavailable,
    ConfigurationRepository,
    InboundDestination,
    PostgresResolutionRepository,
)
from clinic.snapshot import Snapshot
from clinic.vectors import VectorSearch
from clinic.whatsapp import deliver

logger = logging.getLogger(__name__)
KNOWLEDGE_LOAD_TIMEOUT_SECONDS = 20
KNOWLEDGE_CLOSE_TIMEOUT_SECONDS = 2
CALLER_NUMBER = re.compile(r"^\+?[1-9][0-9]{7,14}$")
# Longer than any call, so slots of crashed workers age out on their own.
SLOT_TTL_SECONDS = 2 * 3600


def max_concurrent_calls() -> int:
    """Per-clinic concurrent-call ceiling; 0 disables it. Enforced only with Redis."""
    try:
        return int(os.environ.get("CLINIC_MAX_CONCURRENT_CALLS", "10"))
    except ValueError:
        return 10


def console_clinic() -> UUID:
    """Clinic for local console sessions, which have no dialled number.

    Never used for SIP calls: those resolve their clinic from the trusted destination.
    """
    value = os.environ.get("CONSOLE_CLINIC_ID", "").strip()
    return UUID(value) if value else fixture_id("A")


class AgentKnowledge:
    """Pinned public snapshot exposed to the model through typed tools."""

    def __init__(
        self,
        snapshot: Snapshot | None,
        version: UUID | None = None,
        database: RuntimeDatabase | None = None,
        *,
        clinic: UUID | None = None,
    ) -> None:
        # The backend-resolved tenant must own the snapshot; there is no default clinic.
        if snapshot is not None and (clinic is None or snapshot.clinic_id != clinic):
            raise ClinicUnavailable("Wrong clinic")
        self.snapshot = snapshot
        self.version = version
        self.database = database
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
        self._sending: set[asyncio.Task[int]] = set()
        try:
            self.cipher: PiiCipher | None = PiiCipher.from_environment() if snapshot else None
        except ValueError:
            logger.warning("Clinic encryption keys missing; booking is disabled")
            self.cipher = None

    async def aclose(self) -> None:
        for task in list(self._sending):
            task.cancel()
        if self.vectors is not None:
            await self.vectors.aclose()
        if self.database is not None:
            await self.database.close()
        if self.cache is not None:
            if self.slot and self.snapshot is not None:
                await self.cache.release_call_slot(self.snapshot.clinic_id, self.slot)
            await self.cache.aclose()

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
        return [self.search_clinic_knowledge, self.list_available_slots, self.book_appointment_slot]

    @function_tool()
    async def search_clinic_knowledge(self, question: str) -> dict[str, object]:
        """Hybrid-search reviewed documents and current or scheduled live updates.

        Call this for clinic-information questions such as doctors, opening hours, fees,
        locations, services, qualifications and daily changes. It cannot answer which
        appointment slots are free: use `list_available_slots` for that.
        """
        if self.retriever is None:
            return {"status": "unavailable", "data": {"passages": []}}
        return await self.retriever.result(question)

    async def _open_slots(
        self, requested_date: str, doctor: str, service: str
    ) -> tuple[Result, Any, UUID | None, list[str]]:
        """Published hours for one doctor and date, minus slots already booked."""
        if self.structured is None or self.database is None or self.snapshot is None:
            logger.warning("Slot lookup skipped: clinic knowledge unavailable")
            return Result(status="unavailable", next_action="contact_reception"), None, None, []
        try:
            day = self.structured.day(requested_date)
        except ValueError:
            logger.info("Slot lookup rejected unusable date %r", requested_date[:32])
            return Result(status="unavailable", next_action="ask_for_clarification"), None, None, []
        found = self.structured.availability(
            Query(requested_date=day.isoformat(), doctor=doctor, service=service)
        )
        if found.status != "success":
            logger.info(
                "Slot lookup for %r service=%r on %s: %s",
                doctor[:40], service[:40], day, found.status,
            )
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
            return Result(status="failed", next_action="contact_reception"), None, None, []
        taken = set(row["taken"]) if row else set()
        free = self.structured.slot_times(found.data["hours"], taken)
        logger.info(
            "Slot lookup for %s on %s: %d free, %d taken", doctor[:40], day, len(free), len(taken)
        )
        return found, day, reference, free

    @function_tool()
    async def list_available_slots(
        self, requested_date: str = "today", doctor: str = "", service: str = ""
    ) -> dict[str, object]:
        """List appointment slots that are still free on one date.

        Call this before offering a time or booking. `requested_date` accepts "today",
        "tomorrow" or YYYY-MM-DD. Offer only the returned times; never invent one.
        """
        found, day, _, free = await self._open_slots(requested_date, doctor, service)
        if found.status != "success" or day is None or self.snapshot is None:
            return found.model_dump(mode="json")
        return {
            "status": "success" if free else "unavailable",
            "data": {
                "date": day.isoformat(),
                "doctor": found.data["doctor"],
                "slot_minutes": self.snapshot.slot_minutes,
                "free_slots": free,
                "notices": found.data["notices"],
            },
            "next_action": "offer_slots" if free else "offer_callback",
        }

    @function_tool()
    async def book_appointment_slot(
        self,
        requested_date: str,
        start_time: str,
        patient_name: str,
        doctor: str = "",
        service: str = "",
        callback_number: str = "",
    ) -> dict[str, object]:
        """Book one free slot returned by list_available_slots and notify by WhatsApp.

        Read the doctor, date, time and name back to the caller and get agreement first.
        `start_time` must be exactly one of the free slots. Leave `callback_number` empty
        to use the number the caller is dialling from.
        """
        found, day, reference, free = await self._open_slots(requested_date, doctor, service)
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
            return {"status": "unavailable", "data": {}, "next_action": "contact_reception"}

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
            return {"status": "failed", "data": {}, "next_action": "contact_reception"}

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
            await conn.execute(
                "SELECT clinic_private.queue_whatsapp(%s,%s)", (key, Jsonb(messages))
            )

        task = asyncio.create_task(deliver(self.database, clinic, self.cipher))
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)
        return {
            "status": "success",
            "data": {
                "reference": str(key)[:8],
                "doctor": who,
                "date": day.isoformat(),
                "start_time": label,
                "slot_minutes": minutes,
                "whatsapp_notified": number,
            },
            "next_action": "read_back_confirmation",
        }


async def load_agent_knowledge(
    root: Path, destination: InboundDestination | None = None
) -> AgentKnowledge:
    """Resolve the tenant and load and pin its one published snapshot for this call.

    SIP calls pass the trusted destination; the database maps it to exactly one active
    clinic and its active version, or the call gets no clinic knowledge (fail closed).
    Only a local console session (``destination is None``) uses ``console_clinic``.

    The pool stays open for the lifetime of the call so a booking tool never pays
    connection setup mid-conversation. Close it with `AgentKnowledge.aclose`.
    """
    from clinic.settings import DatabaseSettings

    async def load() -> AgentKnowledge:
        project = dotenv_values(root / ".env").get("SUPABASE_PROJECT_REF") or ""
        runtime = dotenv_values(root / ".env.runtime")
        settings = DatabaseSettings.validate(runtime.get("DATABASE_URL") or "", project)
        database = RuntimeDatabase(settings)
        cache = Cache.from_environment()
        slot = ""
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
                    raise ClinicUnavailable("Concurrent call limit reached")
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
            knowledge.cache, knowledge.slot = cache, slot
            bind(clinic_id=clinic, configuration_version_id=version)
            logger.info("Clinic resolved clinic=%s version=%s", clinic, version)
        except BaseException:
            try:
                await asyncio.wait_for(database.close(), KNOWLEDGE_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("Clinic knowledge database cleanup did not finish")
            if slot and destination is not None:
                with contextlib.suppress(Exception):
                    await cache.release_call_slot(clinic, slot)
            await cache.aclose()
            raise
        if knowledge.vectors is not None:
            knowledge._warm = asyncio.create_task(knowledge.vectors.warm())
        return knowledge

    try:
        return await asyncio.wait_for(load(), KNOWLEDGE_LOAD_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Clinic knowledge load failed (%s)", type(exc).__name__)
        return AgentKnowledge(None)

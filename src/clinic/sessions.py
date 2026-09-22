"""Bounded, tenant-scoped session persistence and trusted-event orchestration."""

import asyncio
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from clinic.db import RuntimeDatabase
from clinic.privacy import PiiCipher
from clinic.requests import ConfirmationState, RequestDetails
from clinic.resolver import ClinicScope, ConfigurationRepository
from clinic.safety import SafetyDecision, classify, response
from clinic.snapshot import Snapshot


@dataclass(frozen=True)
class CallContext:
    session_id: UUID
    scope: ClinicScope
    deadline: datetime


class CallSessionService:
    def __init__(self, database: RuntimeDatabase) -> None:
        self.database = database

    async def note(self, context: CallContext, topic: str, outcome: str) -> None:
        """Host-only fixed-vocabulary summary; SQL rejects raw caller/model text."""
        async with self.database.connection(context.scope.clinic_id) as conn:
            await conn.execute(
                "SELECT clinic_private.note_call(%s,%s,%s)",
                (context.session_id, topic, outcome),
            )

    async def start(
        self,
        scope: ClinicScope,
        *,
        provider: str,
        account: str,
        call_id: str,
        room_id: str,
        is_test: bool = False,
    ) -> CallContext:
        async with self.database.connection(scope.clinic_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT clinic_private.start_call(%s,%s,%s,%s,%s,%s,%s) AS context",
                    (
                        scope.phone_number_id,
                        scope.configuration_version_id,
                        provider,
                        account,
                        call_id,
                        room_id,
                        is_test,
                    ),
                )
            ).fetchone()
        if row is None:
            raise ValueError("Call initialization failed")
        value = row["context"]
        return CallContext(
            UUID(value["id"]),
            replace(scope, configuration_version_id=UUID(value["configuration_version_id"])),
            datetime.fromisoformat(value["deadline_at"]),
        )

    async def event(self, context: CallContext, action: str, event_id: UUID | None = None) -> bool:
        async with self.database.connection(context.scope.clinic_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT clinic_private.update_call(%s,%s,%s) AS live",
                    (context.session_id, action, event_id or uuid4()),
                )
            ).fetchone()
        return bool(row and row["live"])

    async def usage(
        self,
        context: CallContext,
        event_id: UUID,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        stt_seconds: float = 0,
        tts_characters: int = 0,
    ) -> None:
        seconds = Decimal(str(stt_seconds))
        if not seconds.is_finite():
            raise ValueError("Usage must be finite")
        if min(input_tokens, output_tokens, stt_seconds, tts_characters) < 0:
            raise ValueError("Usage cannot be negative")
        async with self.database.connection(context.scope.clinic_id) as conn:
            await conn.execute(
                "SELECT clinic_private.record_usage(%s,%s,%s,%s,%s,%s)",
                (
                    context.session_id,
                    event_id,
                    input_tokens,
                    output_tokens,
                    seconds,  # psycopg float binds as float8, not the function's numeric argument.
                    tts_characters,
                ),
            )


class CallOrchestrator:
    """Own one call. Host adapter must close media on stop_event and confirm playback.

    Does not accept model-declared confirmation. No automatic caller-profile reuse.
    Transfer remains disabled until actual carrier answer/recovery semantics pass.
    """

    def __init__(
        self,
        service: CallSessionService,
        context: CallContext,
        snapshot: Snapshot,
        cipher: PiiCipher,
    ) -> None:
        if snapshot.clinic_id != context.scope.clinic_id:
            raise ValueError("Snapshot scope mismatch")
        self.service, self.context, self.snapshot, self.cipher = service, context, snapshot, cipher
        self.confirmation = ConfirmationState()
        self.language = snapshot.default_language
        self.stop_event = asyncio.Event()
        self.tools_allowed = False
        self.last_activity = time.monotonic()
        self._watcher: asyncio.Task[None] | None = None
        self._closed = False
        self._persist_lock = asyncio.Lock()

    @classmethod
    async def load(
        cls, service: CallSessionService, context: CallContext, cipher: PiiCipher
    ) -> "CallOrchestrator":
        snapshot = Snapshot.model_validate(
            await ConfigurationRepository(service.database, context.scope).load()
        )
        return cls(service, context, snapshot, cipher)

    def start_watchdog(self) -> None:
        if self._watcher is None:
            self._watcher = asyncio.create_task(self._watch(), name="clinic-call-watchdog")

    async def _watch(self) -> None:
        try:
            while not self.stop_event.is_set():
                remaining = (self.context.deadline - datetime.now(timezone.utc)).total_seconds()
                silence_remaining = 60 - (time.monotonic() - self.last_activity)
                if remaining <= 0 or silence_remaining <= 0:
                    await self.close("timeout")
                    return
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=min(20, remaining, silence_remaining)
                    )
                except asyncio.TimeoutError:
                    if not await self.service.event(self.context, "heartbeat"):
                        await self.close("timeout")
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Dependency outage stops the call; durable reconciliation covers failed finalization.
            self.stop_event.set()
            self.confirmation.clear()

    async def close(self, reason: Literal["ended", "timeout"] = "ended") -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        self.confirmation.clear()
        if self._watcher is not None and self._watcher is not asyncio.current_task():
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
        try:
            await asyncio.wait_for(self.service.event(self.context, reason), 5)
        except Exception:
            # No raw transcript/exception logging; maintenance closes abandoned sessions.
            pass

    def _live(self) -> None:
        if self.stop_event.is_set() or datetime.now(timezone.utc) >= self.context.deadline:
            raise ValueError("Call is no longer active")

    async def user_turn(self, text: str) -> tuple[SafetyDecision, str | None]:
        self._live()
        self.activity()
        decision = classify(text)
        self.tools_allowed = decision.allow_tools
        if not decision.allow_tools:
            self.confirmation.clear()
            if decision.route in {"emergency", "medical", "injection"}:
                await self.service.event(self.context, "safety_routed")
            return decision, response(decision, self.snapshot.emergency_message, self.language)
        self.confirmation.user_turn(text)
        return decision, None

    def activity(self) -> None:
        """Trusted host reports caller activity or completed assistant playback."""
        self.last_activity = time.monotonic()

    def set_language(self, language: str) -> None:
        if language not in self.snapshot.supported_languages:
            raise ValueError("Language not supported")
        self.language = language

    def prepare_request(self, details: RequestDetails) -> dict[str, str]:
        self._live()
        if not self.tools_allowed:
            raise ValueError("Administrative caller turn required")
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(self.snapshot.timezone)).date()
        day = details.preferred_date or today
        if day < today or (day - today).days > 366:
            raise ValueError("Requested date unavailable")
        doctors = {d.id: d for d in self.snapshot.doctors if d.effective(day)}
        services = {s.id: s for s in self.snapshot.services if s.effective(day)}
        if details.doctor_id and details.doctor_id not in doctors:
            raise ValueError("Doctor outside the pinned clinic configuration")
        if details.service_id and details.service_id not in services:
            raise ValueError("Service outside the pinned clinic configuration")
        if (
            details.doctor_id
            and details.is_new_patient
            and not doctors[details.doctor_id].accepts_new_patients
        ):
            raise ValueError("Selected doctor does not accept new patients")
        if (
            details.doctor_id
            and details.service_id
            and not any(
                fee.doctor_id == details.doctor_id
                and fee.service_id == details.service_id
                and fee.effective(day)
                for fee in self.snapshot.doctor_services
            )
        ):
            raise ValueError("Doctor and service have no effective published association")
        description = "Callback requested."
        if details.kind == "appointment":
            names = [doctors[details.doctor_id].display_name] if details.doctor_id else []
            if details.service_id:
                names.append(services[details.service_id].name)
            description = f"{', '.join(names)} on {day.isoformat()}."
            if details.preferred_time_start:
                description += f" After {details.preferred_time_start.isoformat()}."
            if details.preferred_time_end:
                description += f" Before {details.preferred_time_end.isoformat()}."
            description += " New patient." if details.is_new_patient else " Existing patient."
        elif details.requested_time:
            description += f" At {details.requested_time.isoformat()}."
        if self.language == "hi-IN":
            description = "वापस कॉल करने का अनुरोध।"
            if details.kind == "appointment":
                names = [doctors[details.doctor_id].display_name] if details.doctor_id else []
                if details.service_id:
                    names.append(services[details.service_id].name)
                description = f"{', '.join(names)}, तारीख {day.isoformat()}।"
                if details.preferred_time_start:
                    description += f" {details.preferred_time_start.isoformat()} के बाद।"
                if details.preferred_time_end:
                    description += f" {details.preferred_time_end.isoformat()} से पहले।"
                description += " नए मरीज।" if details.is_new_patient else " मौजूदा मरीज।"
            elif details.requested_time:
                description += f" समय {details.requested_time.isoformat()}।"
        pending = self.confirmation.prepare(details, description, self.language)
        return {"revision": str(pending.revision), "readback": pending.text}

    async def persist_confirmed(self) -> UUID:
        self._live()
        if not self.tools_allowed:
            raise ValueError("Administrative caller confirmation required")
        async with self._persist_lock:
            pending = self.confirmation.confirmed()
            details = pending.details
            data = details.model_dump(mode="json", exclude={"kind", "name", "phone"})
            tenant = self.context.scope.clinic_id
            name = self.cipher.encrypt(details.name, tenant, pending.request_id, "name")
            phone = self.cipher.encrypt(details.phone, tenant, pending.request_id, "phone")
            async with self.service.database.connection(tenant) as conn:
                row = await (
                    await conn.execute(
                        "SELECT clinic_private.create_request(%s,%s,%s,%s,%s,%s,%s) AS id",
                        (
                            self.context.session_id,
                            pending.request_id,
                            details.kind,
                            name,
                            phone,
                            self.cipher.current,
                            Jsonb(data),
                        ),
                    )
                ).fetchone()
            if not row:
                raise ValueError("Request persistence unavailable")
            # Preserve confirmed id for safe duplicate delivery/retry until next user turn.
            return UUID(str(row["id"]))

    async def transfer(self) -> dict[str, Any]:
        self._live()
        await self.service.event(self.context, "transfer_failed")
        return {
            "status": "unavailable",
            "data": {"transferred": False},
            "next_action": "offer_callback",
            "reason": "verified_handoff_not_configured",
        }

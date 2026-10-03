"""Record a call and what came of it (step 4c): the conversation, its events, its requests.

Writes go only through the `engagement.runtime_*` SECURITY DEFINER functions, scoped to the
workspace the call was resolved to. Nothing the caller said is stored: events carry which
tool ran and whether it worked; requests carry the pack's structured fields, plus a name and
callback number encrypted here (bound to workspace, record and field) before they leave.
Recording never breaks a call: failures are logged by type and the call goes on.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

import psycopg

from praxima.ai.release.loader import LoadedRelease, runtime_database_url
from praxima.shared.kernel.ids import new_id
from praxima.shared.security.privacy import PiiCipher

logger = logging.getLogger(__name__)
WRITE_TIMEOUT_SECONDS = 5


class RecordingUnavailable(Exception):
    """The request couldn't be stored (database down, keys missing, or refused)."""


class CallRecorder:
    def __init__(
        self,
        release: LoadedRelease,
        *,
        called_number: str,
        provider: str,
        provider_call_id: str,
        is_test: bool,
        caller_number: str = "",
    ) -> None:
        self.release = release
        self.called_number = called_number
        self.provider = provider
        self.provider_call_id = provider_call_id
        self.is_test = is_test
        # Held only to fill in "call me back on this number"; never sent to the model.
        self._caller_number = caller_number
        self.conversation_id: uuid.UUID | None = None
        self.requests: list[str] = []
        self.tools_used = 0
        self._events = 0
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.release.workspace_id

    @property
    def caller_number(self) -> str:
        return self._caller_number

    async def _call(self, sql: str, params: tuple[Any, ...]) -> Any:
        url = runtime_database_url()
        if url is None:
            raise RecordingUnavailable("runtime_database_not_configured")

        async def run() -> Any:
            async with await psycopg.AsyncConnection.connect(url, connect_timeout=3) as conn:
                row = await (await conn.execute(sql, params)).fetchone()
                await conn.commit()
                return row[0] if row else None

        try:
            return await asyncio.wait_for(run(), WRITE_TIMEOUT_SECONDS)
        except Exception as exc:  # never log values, numbers or the URL
            raise RecordingUnavailable(type(exc).__name__) from None

    async def start(self) -> None:
        try:
            self.conversation_id = await self._call(
                "SELECT engagement.runtime_start_conversation(%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.workspace_id,
                    self.release.agent_id,
                    self.release.release_id,
                    self.called_number,
                    self.provider,
                    self.provider_call_id,
                    self.is_test,
                ),
            )
        except RecordingUnavailable as exc:
            logger.warning("Call recording unavailable (%s)", exc)
            return
        self.event("call_started", {"release_version": self.release.version_no})

    def event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Append a content-free event without delaying the conversation."""
        if self.conversation_id is None:
            return
        self._events += 1
        key = f"{event_type}.{self._events}"

        async def write() -> None:
            try:
                await self._call(
                    "SELECT engagement.runtime_record_event(%s,%s,%s,%s,%s)",
                    (
                        self.workspace_id,
                        self.conversation_id,
                        key,
                        event_type,
                        json.dumps(payload) if payload is not None else None,
                    ),
                )
            except RecordingUnavailable as exc:
                logger.warning("Call event not recorded (%s)", exc)

        task = asyncio.create_task(write())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def tool_used(self, tool: str, status: str) -> None:
        self.tools_used += 1
        self.event("tool_called", {"tool": tool, "status": status})

    async def create_request(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        entity_id: str | None,
        name: str | None,
        callback_number: str | None,
    ) -> tuple[uuid.UUID, bool]:
        """Store a request for staff. Idempotent per call and kind; returns (id, created)."""
        if self.conversation_id is None:
            raise RecordingUnavailable("no_conversation")
        item_id = new_id()
        subject = number = version = None
        if name or callback_number:
            try:
                cipher = PiiCipher.from_environment()
            except ValueError:
                raise RecordingUnavailable("pii_keys_missing") from None
            if name:
                subject = cipher.encrypt(name, self.workspace_id, item_id, "subject_name")
            if callback_number:
                number = cipher.encrypt(
                    callback_number, self.workspace_id, item_id, "callback_number"
                )
            version = cipher.current
        result = await self._call(
            "SELECT engagement.runtime_create_work_item(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                self.workspace_id,
                self.conversation_id,
                item_id,
                kind,
                f"voice:{self.conversation_id}:{kind}",
                json.dumps(payload),
                entity_id,
                subject,
                number,
                version,
            ),
        )
        created = bool(result and result.get("created"))
        if created:
            self.requests.append(kind)
            self.event("request_created", {"kind": kind})
        return uuid.UUID(str(result["id"])), created

    async def slot_context(
        self, entity_id: str, starts: datetime, ends: datetime
    ) -> dict[str, Any]:
        """Booking rules and busy ranges of one entry (scheduling.runtime_slot_context)."""
        result = await self._call(
            "SELECT scheduling.runtime_slot_context(%s,%s,%s,%s)",
            (self.workspace_id, entity_id, starts, ends),
        )
        return result if isinstance(result, dict) else {"enabled": False, "reason": "unknown"}

    async def book_slot(
        self,
        *,
        entity_id: str,
        subject_id: str | None,
        starts_at: datetime,
        name: str,
        phone: str | None,
    ) -> dict[str, Any]:
        """Book one slot for the caller (held or confirmed per the workspace's setting).

        Idempotent per call, entry and start time: a retried tool call never books twice.
        """
        if self.conversation_id is None:
            raise RecordingUnavailable("no_conversation")
        booking_id = new_id()
        try:
            cipher = PiiCipher.from_environment()
        except ValueError:
            raise RecordingUnavailable("pii_keys_missing") from None
        subject = cipher.encrypt(name, self.workspace_id, booking_id, "subject_name")
        number = cipher.encrypt(phone, self.workspace_id, booking_id, "phone") if phone else None
        result = await self._call(
            "SELECT scheduling.runtime_book_slot(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                self.workspace_id,
                self.conversation_id,
                self.release.agent_id,
                booking_id,
                entity_id,
                subject_id,
                starts_at,
                f"voice:{self.conversation_id}:{entity_id}:{starts_at.isoformat()}",
                subject,
                number,
                phone[-4:] if phone else None,
                cipher.current,
            ),
        )
        if not isinstance(result, dict):
            raise RecordingUnavailable("no_result")
        if result.get("created") and result.get("status") in ("held", "confirmed"):
            self.requests.append("booking")
            # An allowed event type (0009): a booking is the request this call produced.
            self.event("request_created", {"kind": "booking", "status": result["status"]})
        return result

    async def finish(self, reason: str = "") -> None:
        """At hang-up: close the conversation with its outcome (content-free)."""
        if self.conversation_id is None:
            return
        self.event("call_ended", {"requests": len(self.requests)})
        if self._pending:
            await asyncio.wait(self._pending, timeout=WRITE_TIMEOUT_SECONDS)
        if self.requests:
            intent, disposition = self.requests[0], "request_created"
        elif self.tools_used:
            intent, disposition = "information", "answered"
        else:
            intent, disposition = None, "no_action"
        try:
            await self._call(
                "SELECT engagement.runtime_finish_conversation(%s,%s,%s,%s,%s,%s)",
                (self.workspace_id, self.conversation_id, "completed", intent, disposition, None),
            )
        except RecordingUnavailable as exc:
            logger.warning("Call end not recorded (%s)", exc)

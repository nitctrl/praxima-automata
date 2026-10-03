"""The voice agent's knowledge for one call, from its pinned agent release (steps 4b, 4c).

Exposes only the tools the release enables. Answers come from the snapshot loaded at call
start (a release published mid-call never changes what this call knows); the call itself and
any requests the caller leaves are recorded through `CallRecorder`.
"""

import json
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from livekit.agents import function_tool

from praxima.runtime.prompting import render_release_prompt
from praxima.runtime.release import lookup
from praxima.runtime.release.loader import LoadedRelease, NoRelease, load_release
from praxima.runtime.release.recorder import CallRecorder, RecordingUnavailable
from praxima.shared.kernel.slots import open_slots

logger = logging.getLogger(__name__)
READ_TOOLS = (
    "find_entities",
    "get_entity",
    "get_availability",
    "search_knowledge",
    "get_announcements",
    "find_open_slots",
)
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TIME = re.compile(r"^[0-2][0-9]:[0-5][0-9]$")
_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
UNAVAILABLE = (
    "Published information is unavailable right now. Do not invent any facts. Explain "
    "briefly that the information cannot be reached at the moment and suggest calling back."
)


class ReleaseKnowledge:
    def __init__(self, loaded: LoadedRelease | NoRelease) -> None:
        self.loaded = loaded if isinstance(loaded, LoadedRelease) else None
        self.reason = loaded.reason if isinstance(loaded, NoRelease) else None
        # The worker logs "loaded"/"unavailable" from this, as for the legacy knowledge.
        self.snapshot = self.loaded.snapshot if self.loaded else None
        self.recorder: CallRecorder | None = None
        # Bookings made on this call, so a retried tool call answers the same way.
        self._booked: dict[tuple[str, datetime], dict[str, Any]] = {}

    async def start_call(
        self, *, called_number: str, is_sip: bool, attributes: Mapping[str, str]
    ) -> None:
        """Open the call record (only when a release is loaded, so the tenant is known)."""
        if self.loaded is None:
            return
        call_id = attributes.get("sip.callID", "") if is_sip else ""
        caller = attributes.get("sip.phoneNumber", "") if is_sip else ""
        self.recorder = CallRecorder(
            self.loaded,
            called_number=called_number,
            provider="livekit-sip" if is_sip and _CALL_ID.fullmatch(call_id) else "console",
            provider_call_id=call_id
            if is_sip and _CALL_ID.fullmatch(call_id)
            else f"console-{uuid.uuid4().hex}",
            is_test=not is_sip,
            caller_number=caller if lookup.E164.fullmatch(caller) else "",
        )
        await self.recorder.start()

    async def finish_call(self, reason: str = "") -> None:
        if self.recorder is not None:
            await self.recorder.finish(reason)

    def _track(self, tool: str, result: dict[str, Any]) -> dict[str, Any]:
        if self.recorder is not None:
            self.recorder.tool_used(tool, str(result.get("status", "success")))
        return result

    def _request_kinds(self) -> set[str] | None:
        """None = every request type; a set = only these (or none)."""
        assert self.snapshot is not None
        enabled = {t.key for t in self.snapshot.tools}
        if "create_work_item" in enabled:
            return None
        callback = self.snapshot.pack.callback_kind  # the pack's callback request type
        return {callback} if "request_callback" in enabled and callback else set()

    def describe(self) -> str:
        if self.loaded is None:
            return f"unavailable ({self.reason})"
        return f"release v{self.loaded.version_no} loaded"

    def _now(self) -> datetime:
        assert self.snapshot is not None
        return datetime.now(ZoneInfo(self.snapshot.workspace.timezone))

    @property
    def instructions(self) -> str:
        return render_release_prompt(self.snapshot) if self.snapshot else UNAVAILABLE

    @property
    def greeting(self) -> str | None:
        """Said word for word when the caller connects (the agent's published greeting)."""
        return self.snapshot.agent.greeting if self.snapshot else None

    @property
    def greeting_instruction(self) -> str:
        return (
            "Greet the caller in one short sentence and explain that information is "
            "unavailable right now. Use Hindi unless the caller speaks English."
        )

    def function_tools(self) -> list[Any]:
        if self.snapshot is None:
            return []
        enabled = {t.key for t in self.snapshot.tools}
        tools = {
            "find_entities": self.find_entities,
            "get_entity": self.get_entity,
            "get_availability": self.get_availability,
            "search_knowledge": self.search_knowledge,
            "get_announcements": self.get_announcements,
            "find_open_slots": self.find_open_slots,
        }
        if self.snapshot.entity_types:
            # Name this release's entry types (doctor, property …) instead of hard-coding them.
            types = ", ".join(f"{t.key} ({t.name})" for t in self.snapshot.entity_types)
            tools["find_entities"] = function_tool(
                self._find_entities,
                name="find_entities",
                description=f"Find published directory entries. Entry types: {types}.",
            )
        chosen = [tools[key] for key in READ_TOOLS if key in enabled]
        if "book_slot" in enabled:
            chosen.append(self.book_slot)
        if self._request_kinds() != set():
            chosen.append(self.create_request)
        return chosen

    def _lookup_entities(self, query: str, entity_type: str) -> dict[str, Any]:
        assert self.snapshot is not None
        return self._track(
            "find_entities", lookup.find_entities(self.snapshot, query, entity_type or None)
        )

    async def _find_entities(self, query: str, entity_type: str = "") -> dict[str, Any]:
        """
        Args:
            query: What the caller asked about, in their words.
            entity_type: Optional entry type key, from the list in the description.
        """
        return self._lookup_entities(query, entity_type)

    @function_tool()
    async def find_entities(self, query: str, entity_type: str = "") -> dict[str, Any]:
        """Find published directory entries by name or details.

        Args:
            query: What the caller asked about, in their words.
            entity_type: Optional entry type key, from the list in the description.
        """
        return self._lookup_entities(query, entity_type)

    @function_tool()
    async def get_entity(self, name: str) -> dict[str, Any]:
        """Details of one directory entry, and what it is linked to (with fees or other details).

        Args:
            name: The name as the caller said it.
        """
        assert self.snapshot is not None
        return self._track("get_entity", lookup.get_entity(self.snapshot, name))

    @function_tool()
    async def get_availability(self, name: str, date: str = "") -> dict[str, Any]:
        """Published hours of one directory entry, with leave and holidays applied.

        Args:
            name: The entry, as the caller said it.
            date: Optional date as YYYY-MM-DD in the business timezone; empty for the next 7 days.
        """
        assert self.snapshot is not None
        return self._track(
            "get_availability",
            lookup.get_availability(self.snapshot, name, self._now(), date or None),
        )

    @function_tool()
    async def search_knowledge(self, question: str) -> dict[str, Any]:
        """Search reviewed documents, approved answers, live updates and directory entries.

        Args:
            question: The caller's complete question.
        """
        assert self.snapshot is not None
        return self._track(
            "search_knowledge", lookup.search_knowledge(self.snapshot, question, self._now())
        )

    @function_tool()
    async def get_announcements(self) -> dict[str, Any]:
        """Live updates in force now or scheduled (closures, someone on leave)."""
        assert self.snapshot is not None
        return self._track(
            "get_announcements", lookup.get_announcements(self.snapshot, self._now())
        )

    @function_tool()
    async def create_request(
        self,
        kind: str,
        details_json: str = "{}",
        about: str = "",
        name: str = "",
        callback_number: str = "",
        use_calling_number: bool = False,
    ) -> dict[str, Any]:
        """Leave a request for staff to follow up (never a confirmed booking).

        Confirm the details with the caller first, and ask before using their number.

        Args:
            kind: The request type key, from the request types in your instructions.
            details_json: The request type's fields as a JSON object, e.g.
                {"preferred_date": "2026-10-20", "preferred_time_start": "10:00"}.
            about: Optional directory entry the request is about, as the caller said it.
            name: The caller's name for staff, if they gave it.
            callback_number: A number the caller dictated, with country code (+91…).
            use_calling_number: True if the caller agreed to be called back on the number
                they are calling from.
        """
        assert self.snapshot is not None
        try:
            details = json.loads(details_json or "{}")
        except json.JSONDecodeError:
            details = None
        if not isinstance(details, dict):
            return self._track(
                "create_request", {"status": "invalid", "fix": {"details_json": "A JSON object."}}
            )
        number = callback_number.replace(" ", "").replace("-", "")
        if use_calling_number and self.recorder is not None and self.recorder.caller_number:
            number = self.recorder.caller_number
        checked = lookup.prepare_request(
            self.snapshot, kind, details, about, number, self._request_kinds()
        )
        if checked["status"] != "ok":
            return self._track("create_request", checked)
        if self.recorder is None:
            return self._track("create_request", {"status": "unavailable"})
        try:
            item_id, created = await self.recorder.create_request(
                kind=kind,
                payload=checked["payload"],
                entity_id=checked["entity_id"],
                name=name.strip()[:200] or None,
                callback_number=number or None,
            )
        except RecordingUnavailable as exc:
            logger.warning("Request not recorded (%s)", exc)
            return self._track(
                "create_request",
                {"status": "unavailable", "say": self.snapshot.agent.fallback_message},
            )
        return self._track(
            "create_request",
            {
                "status": "recorded" if created else "already_recorded",
                "reference": str(item_id)[:8],
                "note": "Staff will contact the caller to confirm. This is not a booking.",
            },
        )

    # ---------------------------------------------------------- slot booking

    async def _open_slots(
        self, entity_id: str, days: list[date]
    ) -> tuple[dict[str, Any], list[datetime]]:
        """Open slots from the release's published hours minus live bookings (shared maths)."""
        assert self.snapshot is not None and self.recorder is not None
        zone = ZoneInfo(self.snapshot.workspace.timezone)
        starts = datetime.combine(days[0], time(), zone)
        ends = datetime.combine(days[-1] + timedelta(days=1), time(), zone)
        context = await self.recorder.slot_context(entity_id, starts, ends)
        if not context.get("enabled"):
            return context, []
        rules, exceptions = lookup.entity_hours(self.snapshot, entity_id)
        busy = [
            (datetime.fromisoformat(a), datetime.fromisoformat(b))
            for a, b in context.get("busy", [])
        ]
        return context, open_slots(
            rules=rules,
            exceptions=exceptions,
            timezone=self.snapshot.workspace.timezone,
            slot_minutes=int(context["slot_minutes"]),
            busy=busy,
            now=self._now(),
            first_day=days[0],
            days=len(days),
            notice_minutes=int(context["min_notice_minutes"]),
            horizon_days=int(context["horizon_days"]),
        )

    def _days(self, on: str) -> list[date] | None:
        today = self._now().date()
        if not on:
            return [today + timedelta(days=i) for i in range(7)]
        if not _DATE.fullmatch(on):
            return None
        try:
            return [date.fromisoformat(on)]
        except ValueError:
            return None

    @function_tool()
    async def find_open_slots(self, name: str, date: str = "") -> dict[str, Any]:
        """Open booking slots of one bookable directory entry, for a date or the next week.

        Only these times can be booked; never offer any other time.

        Args:
            name: The entry, as the caller said it.
            date: Optional date as YYYY-MM-DD in the business timezone; empty for the next 7 days.
        """
        assert self.snapshot is not None
        entity = lookup.resolve_entity(self.snapshot, name)
        if entity is None:
            return self._track(
                "find_open_slots",
                {
                    "status": "not_found",
                    "hint": "Ask which one the caller means, or use find_entities.",
                },
            )
        days = self._days(date)
        if days is None:
            return self._track(
                "find_open_slots",
                {"status": "invalid_date", "hint": "Use YYYY-MM-DD in the business timezone."},
            )
        if self.recorder is None:
            return self._track("find_open_slots", {"status": "unavailable"})
        try:
            context, starts = await self._open_slots(entity.id, days)
        except RecordingUnavailable as exc:
            logger.warning("Open slots unavailable (%s)", exc)
            return self._track("find_open_slots", {"status": "unavailable"})
        if not context.get("enabled"):
            return self._track(
                "find_open_slots",
                {"status": "not_bookable", "entity": entity.name, "hint": "Offer create_request."},
            )
        if not starts:
            return self._track(
                "find_open_slots",
                {
                    "status": "no_open_slots",
                    "entity": entity.name,
                    "hint": "Offer another day, or leave a request with create_request.",
                },
            )
        return self._track(
            "find_open_slots",
            {
                "status": "success",
                "entity": entity.name,
                "timezone": self.snapshot.workspace.timezone,
                "slots": lookup.describe_slots(starts),
                "more_available": len(starts) > 12,
                "requires_staff_confirmation": bool(context.get("requires_confirmation", True)),
                "note": "Offer two or three of these times. Only these can be booked.",
            },
        )

    @function_tool()
    async def book_slot(
        self,
        name: str,
        date: str,
        time: str,
        caller_name: str,
        use_calling_number: bool = False,
        callback_number: str = "",
        about: str = "",
    ) -> dict[str, Any]:
        """Book one open slot for the caller. Read back the entry, date, time, name and number
        first, and call this only after the caller clearly says yes.

        Args:
            name: The entry being booked, as the caller said it.
            date: The slot's date, YYYY-MM-DD, from find_open_slots.
            time: The slot's start time, HH:MM (24-hour), from find_open_slots.
            caller_name: The caller's name.
            use_calling_number: True if the caller agreed to be reached on the number they are
                calling from.
            callback_number: Otherwise a number the caller dictated, with country code (+91…).
            about: Optional service or project the booking is for, as the caller said it.
        """
        assert self.snapshot is not None
        fix: dict[str, str] = {}
        if not caller_name.strip():
            fix["caller_name"] = "Ask for the caller's name."
        if not _DATE.fullmatch(date):
            fix["date"] = "Use YYYY-MM-DD from find_open_slots."
        if not _TIME.fullmatch(time):
            fix["time"] = "Use HH:MM (24-hour) from find_open_slots."
        number = callback_number.replace(" ", "").replace("-", "")
        if use_calling_number and self.recorder is not None and self.recorder.caller_number:
            number = self.recorder.caller_number
        if not number:
            fix["callback_number"] = (
                "Ask whether to use the number they're calling from, or take one with country code."
            )
        elif not lookup.E164.fullmatch(number):
            fix["callback_number"] = "Use the full number with country code, e.g. +91…"
        if fix:
            return self._track("book_slot", {"status": "invalid", "fix": fix})
        entity = lookup.resolve_entity(self.snapshot, name)
        subject = lookup.resolve_entity(self.snapshot, about) if about.strip() else None
        if entity is None or (about.strip() and subject is None):
            return self._track(
                "book_slot",
                {"status": "not_found", "hint": "Confirm which one the caller means first."},
            )
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
            hour, minute = (int(part) for part in time.split(":"))
            starts_at = datetime.combine(
                day,
                datetime.min.time().replace(hour=hour, minute=minute),
                ZoneInfo(self.snapshot.workspace.timezone),
            )
        except ValueError:
            return self._track("book_slot", {"status": "invalid", "fix": {"date": "Not a date."}})
        if self.recorder is None:
            return self._track("book_slot", {"status": "unavailable"})
        fallback = {"status": "unavailable", "say": self.snapshot.agent.fallback_message}
        if (entity.id, starts_at) in self._booked:
            return self._track("book_slot", self._booked[(entity.id, starts_at)])
        try:
            context, starts = await self._open_slots(entity.id, [day])
            if not context.get("enabled"):
                return self._track(
                    "book_slot", {"status": "not_bookable", "hint": "Offer create_request."}
                )
            if starts_at not in starts:
                return self._track(
                    "book_slot",
                    {
                        "status": "not_open",
                        "alternatives": lookup.describe_slots(starts, 4),
                        "hint": "That time isn't open. Offer one of the alternatives.",
                    },
                )
            result = await self.recorder.book_slot(
                entity_id=entity.id,
                subject_id=subject.id if subject else None,
                starts_at=starts_at,
                name=caller_name.strip()[:200],
                phone=number,
            )
        except RecordingUnavailable as exc:
            logger.warning("Booking not recorded (%s)", exc)
            return self._track("book_slot", fallback)
        status = result.get("status")
        if status in ("held", "confirmed"):
            answer = {
                "status": status,
                "reference": str(result.get("id", ""))[:8],
                "say": "The slot is reserved for the caller. The team will call to confirm it."
                if status == "held"
                else "It is booked for that date and time.",
            }
            self._booked[(entity.id, starts_at)] = answer
            return self._track("book_slot", answer)
        if status == "taken":
            _, again = await self._open_slots(entity.id, [day])
            return self._track(
                "book_slot",
                {
                    "status": "taken",
                    "alternatives": lookup.describe_slots(again, 4),
                    "hint": "Someone just took that time. Offer one of the alternatives.",
                },
            )
        return self._track("book_slot", {"status": status or "unavailable", **fallback})


async def load_release_knowledge(called_number: str) -> ReleaseKnowledge:
    """Pin this call to the live release of the agent the called number is routed to."""
    if not called_number:
        return ReleaseKnowledge(NoRelease("no_called_number"))
    return ReleaseKnowledge(await load_release(called_number))

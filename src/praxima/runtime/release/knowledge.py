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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from livekit.agents import function_tool

from praxima.runtime.prompting import render_release_prompt
from praxima.runtime.release import lookup
from praxima.runtime.release.loader import LoadedRelease, NoRelease, load_release
from praxima.runtime.release.recorder import CallRecorder, RecordingUnavailable

logger = logging.getLogger(__name__)
READ_TOOLS = (
    "find_entities",
    "get_entity",
    "get_availability",
    "search_knowledge",
    "get_announcements",
)
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


async def load_release_knowledge(called_number: str) -> ReleaseKnowledge:
    """Pin this call to the live release of the agent the called number is routed to."""
    if not called_number:
        return ReleaseKnowledge(NoRelease("no_called_number"))
    return ReleaseKnowledge(await load_release(called_number))

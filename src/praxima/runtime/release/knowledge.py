"""The voice agent's knowledge for one call, from its pinned agent release (step 4b).

Exposes only the tools the release enables, all answering in memory from the snapshot loaded
at call start: a release published mid-call never changes what this call knows.
"""

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from livekit.agents import function_tool

from praxima.runtime.prompting import render_release_prompt
from praxima.runtime.release import lookup
from praxima.runtime.release.loader import LoadedRelease, NoRelease, load_release

logger = logging.getLogger(__name__)
# Read-only tools this step supports. Tools that write (requests, callbacks) come later.
SUPPORTED_TOOLS = (
    "find_entities",
    "get_entity",
    "get_availability",
    "search_knowledge",
    "get_announcements",
)
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
        return [tools[key] for key in SUPPORTED_TOOLS if key in enabled]

    @function_tool()
    async def find_entities(self, query: str, entity_type: str = "") -> dict[str, Any]:
        """Find published doctors, services or locations.

        Args:
            query: What the caller asked about, e.g. "heart specialist", "Sharma", "ECG".
            entity_type: Optional: doctor, service or location.
        """
        assert self.snapshot is not None
        return lookup.find_entities(self.snapshot, query, entity_type or None)

    @function_tool()
    async def get_entity(self, name: str) -> dict[str, Any]:
        """Details of one doctor, service or location, and what it is linked to (fees, places).

        Args:
            name: The name as the caller said it, e.g. "Dr Sharma" or "ECG".
        """
        assert self.snapshot is not None
        return lookup.get_entity(self.snapshot, name)

    @function_tool()
    async def get_availability(self, name: str, date: str = "") -> dict[str, Any]:
        """Published hours of a doctor or location, with leave and holidays applied.

        Args:
            name: The doctor or location, as the caller said it.
            date: Optional date as YYYY-MM-DD in the business timezone; empty for the next 7 days.
        """
        assert self.snapshot is not None
        return lookup.get_availability(self.snapshot, name, self._now(), date or None)

    @function_tool()
    async def search_knowledge(self, question: str) -> dict[str, Any]:
        """Search reviewed documents, approved answers and live updates.

        Args:
            question: The caller's complete question.
        """
        assert self.snapshot is not None
        return lookup.search_knowledge(self.snapshot, question, self._now())

    @function_tool()
    async def get_announcements(self) -> dict[str, Any]:
        """Live updates in force now or scheduled (closures, doctors on leave)."""
        assert self.snapshot is not None
        return lookup.get_announcements(self.snapshot, self._now())


async def load_release_knowledge(called_number: str) -> ReleaseKnowledge:
    """Pin this call to the live release of the agent the called number is routed to."""
    if not called_number:
        return ReleaseKnowledge(NoRelease("no_called_number"))
    return ReleaseKnowledge(await load_release(called_number))

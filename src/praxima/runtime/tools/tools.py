"""Typed LiveKit tools bound by backend construction, never caller-selected tenants.

Not attached to the live voice agent until Phase 3's session/safety gate passes.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from livekit.agents import function_tool
from pydantic import ValidationError

from praxima.modules.agents.application.resolver import ClinicScope, ClinicUnavailable
from praxima.modules.catalog.domain.knowledge import Query, Result, StructuredKnowledge
from praxima.modules.releases.domain.snapshot import Snapshot


class SnapshotLoader(Protocol):
    async def load(self) -> dict[str, Any]: ...


@runtime_checkable
class PinnedSnapshot(Protocol):
    """Loader that already holds a validated snapshot, so lookups add no parsing latency."""

    def validated(self) -> Snapshot: ...


class ClinicTools:
    def __init__(
        self,
        repository: SnapshotLoader,
        scope: ClinicScope,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._scope = scope
        self._clock = clock

    async def _run(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        async def execute() -> Result:
            repository = self._repository
            snapshot = (
                repository.validated()
                if isinstance(repository, PinnedSnapshot)
                else Snapshot.model_validate(await repository.load())
            )
            if (
                snapshot.clinic_id != self._scope.clinic_id
                or snapshot.timezone != self._scope.timezone
                or snapshot.supported_languages != self._scope.supported_languages
            ):
                raise ClinicUnavailable("Scope mismatch")
            service = StructuredKnowledge(snapshot, clock=self._clock)
            if operation == "status":
                query = Query(location=args["location"])
                requested = args["requested_datetime"]
                if not isinstance(requested, str) or len(requested) > 40:
                    raise ValueError("Invalid requested datetime")
                return service.current_status(query.location, requested)
            if operation == "faq":
                question = args["question"]
                category = args["category"]
                if not isinstance(question, str) or not isinstance(category, str):
                    raise ValueError("Invalid administrative question")
                if len(question) > 500 or len(category) > 100:
                    raise ValueError("Question too long")
                return service.faq(question, category)
            query = Query.model_validate(args)
            actions: dict[str, Callable[[Query], Result]] = {
                "doctors": service.find_doctors,
                "availability": service.availability,
                "fee": service.fee,
                "service": service.service_information,
                "location": service.location,
            }
            return actions[operation](query)

        try:
            result = await asyncio.wait_for(execute(), timeout=5)
        except (ValueError, ValidationError, ClinicUnavailable):
            result = Result(status="unavailable", next_action="clarify_or_contact_reception")
        except Exception:
            # Never serialize DB errors, raw provider messages, queries or tracebacks.
            result = Result(status="failed", next_action="contact_reception")
        return result.model_dump(mode="json")

    @function_tool()
    async def find_doctors(self, name: str = "") -> dict[str, Any]:
        """Find published doctors by name. Clarify if a surname matches more than one doctor."""
        return await self._run(
            "doctors",
            {
                "name": name,
                "speciality": "",
                "service": "",
                "requested_date": "today",
            },
        )

    @function_tool()
    async def get_current_clinic_status(
        self, location: str = "", requested_datetime: str = ""
    ) -> dict[str, Any]:
        """Get open/closed status now, or at an ISO datetime with explicit UTC offset."""
        return await self._run(
            "status", {"location": location, "requested_datetime": requested_datetime}
        )

    @function_tool()
    async def get_doctor_availability(
        self,
        doctor: str,
        requested_date: str = "today",
        location: str = "",
    ) -> dict[str, Any]:
        """Get published working hours, NOT bookable slots. Use today/tomorrow or ISO date.

        Resolve ambiguous names first. Never confirm a booking.
        """
        return await self._run(
            "availability",
            {
                "doctor": doctor,
                "requested_date": requested_date,
                "location": location,
                "time_preference": "any",
                "after": "",
                "before": "",
                "service": "",
            },
        )

    @function_tool()
    async def get_consultation_fee(
        self, doctor: str = "", service: str = "", requested_date: str = "today"
    ) -> dict[str, Any]:
        """Get an effective structured fee. Specify doctor and/or service; clarify ambiguity."""
        return await self._run(
            "fee", {"doctor": doctor, "service": service, "requested_date": requested_date}
        )

    @function_tool()
    async def get_service_information(
        self, service: str, requested_date: str = "today"
    ) -> dict[str, Any]:
        """Get an approved service description, associated doctors and effective structured fees."""
        return await self._run("service", {"service": service, "requested_date": requested_date})

    @function_tool()
    async def get_clinic_location(self, location: str = "") -> dict[str, Any]:
        """Get an effective public address, directions and parking details; clarify locations."""
        return await self._run("location", {"location": location})

    @function_tool()
    async def search_approved_knowledge(self, question: str, category: str = "") -> dict[str, Any]:
        """Match approved administrative FAQ phrasing only; no medical advice.

        Use structured tools for doctors, fees and schedules, and the clinic document
        search for open-ended background questions. Unknown questions require reception.
        """
        return await self._run("faq", {"question": question, "category": category})

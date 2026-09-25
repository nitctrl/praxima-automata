"""Per-call tools for a trusted adapter. No tool can mark a readback as heard."""

from typing import Any

from livekit.agents import function_tool

from praxima.modules.agents.application.resolver import ConfigurationRepository
from praxima.modules.engagement.application.sessions import CallOrchestrator
from praxima.modules.engagement.domain.requests import RequestDetails
from praxima.runtime.policy.safety import classify
from praxima.runtime.tools.tools import ClinicTools


class SessionTools(ClinicTools):
    def __init__(self, call: CallOrchestrator) -> None:
        super().__init__(
            ConfigurationRepository(call.service.database, call.context.scope), call.context.scope
        )
        self.call = call
        self.failures = 0

    async def _run(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        if (
            not self.call.tools_allowed
            or self.call.stop_event.is_set()
            or any(
                classify(value).route in {"medical", "emergency", "injection"}
                for value in args.values()
                if isinstance(value, str)
            )
        ):
            return {"status": "forbidden", "data": {}, "next_action": "safety_response"}
        result = await super()._run(operation, args)
        if result["status"] == "failed":
            self.failures += 1
            try:
                await self.call.service.event(self.call.context, "tool_failed")
            finally:
                if self.failures >= 3:
                    await self.call.close()
        return result

    @function_tool()
    async def prepare_callback(self, name: str, phone: str) -> dict[str, Any]:
        """Prepare a callback request with explicitly supplied name and E.164 number.

        Returns an exact readback. The trusted host must speak it and observe a
        subsequent caller confirmation before saving. Caller ID is not identity.
        """
        try:
            return {
                "status": "success",
                "data": self.call.prepare_request(
                    RequestDetails(kind="callback", name=name, phone=phone)
                ),
                "next_action": "trusted_host_readback",
            }
        except ValueError:
            return {"status": "unavailable", "data": {}, "next_action": "clarify"}

    @function_tool()
    async def prepare_appointment(
        self,
        name: str,
        phone: str,
        preferred_date: str,
        doctor_id: str = "",
        service_id: str = "",
        is_new_patient: bool = True,
    ) -> dict[str, Any]:
        """Prepare a REQUEST, never a booking. References must come from clinic tools."""
        try:
            details = RequestDetails.model_validate(
                {
                    "kind": "appointment",
                    "name": name,
                    "phone": phone,
                    "preferred_date": preferred_date,
                    "doctor_id": doctor_id or None,
                    "service_id": service_id or None,
                    "is_new_patient": is_new_patient,
                }
            )
            return {
                "status": "success",
                "data": self.call.prepare_request(details),
                "next_action": "trusted_host_readback",
            }
        except ValueError:
            return {"status": "unavailable", "data": {}, "next_action": "clarify"}

    @function_tool()
    async def save_confirmed_request(self) -> dict[str, Any]:
        """Save after trusted playback and caller confirmation. No confirmation flag."""
        try:
            request_id = await self.call.persist_confirmed()
            return {
                "status": "success",
                "data": {"request_id": str(request_id), "appointment_confirmed": False},
                "next_action": "staff_will_follow_up",
            }
        except ValueError:
            return {"status": "forbidden", "data": {}, "next_action": "readback_and_confirm"}
        except Exception:
            return {"status": "failed", "data": {}, "next_action": "contact_reception"}

    @function_tool()
    async def transfer_to_reception(self) -> dict[str, Any]:
        """Offer callback instead of pretending an unverified transfer connected."""
        return await self.call.transfer()

    @function_tool()
    async def set_conversation_language(self, language: str) -> dict[str, Any]:
        """Select a clinic-supported language without altering authoritative configuration."""
        try:
            self.call.set_language(language)
            return {"status": "success", "data": {"language": language}}
        except ValueError:
            return {"status": "unavailable", "data": {}, "next_action": "supported_language_only"}

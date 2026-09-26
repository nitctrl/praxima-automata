"""Agent configuration and ingress routing: agents, phone numbers, tools.

Public interface for other modules and entrypoints.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.agents.application.selectors import (
        AgentView,
        IngressTarget,
        PhoneNumberView,
        ToolView,
        get_agent,
        list_agents,
        resolve_ingress,
    )
    from praxima.modules.agents.application.services import (
        AgentChanges,
        AgentDraft,
        assign_phone_number,
        create_agent,
        release_phone_number,
        set_tool,
        update_agent,
    )

_EXPORTS = {
    "AgentView": "praxima.modules.agents.application.selectors",
    "IngressTarget": "praxima.modules.agents.application.selectors",
    "PhoneNumberView": "praxima.modules.agents.application.selectors",
    "ToolView": "praxima.modules.agents.application.selectors",
    "get_agent": "praxima.modules.agents.application.selectors",
    "list_agents": "praxima.modules.agents.application.selectors",
    "resolve_ingress": "praxima.modules.agents.application.selectors",
    "AgentChanges": "praxima.modules.agents.application.services",
    "AgentDraft": "praxima.modules.agents.application.services",
    "assign_phone_number": "praxima.modules.agents.application.services",
    "create_agent": "praxima.modules.agents.application.services",
    "release_phone_number": "praxima.modules.agents.application.services",
    "set_tool": "praxima.modules.agents.application.services",
    "update_agent": "praxima.modules.agents.application.services",
}
__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)
__all__ = [
    "AgentChanges",
    "AgentDraft",
    "AgentView",
    "IngressTarget",
    "PhoneNumberView",
    "ToolView",
    "assign_phone_number",
    "create_agent",
    "get_agent",
    "list_agents",
    "release_phone_number",
    "resolve_ingress",
    "set_tool",
    "update_agent",
]

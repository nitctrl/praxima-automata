"""Agent configuration and ingress routing: agents, phone numbers, tools.

Public interface for other modules and entrypoints.
"""

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

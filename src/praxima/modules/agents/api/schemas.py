"""Request and response models for agent endpoints."""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from praxima.modules import agents

Message = Field(min_length=1, max_length=2000)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolOut(BaseModel):
    key: str
    enabled: bool


class PhoneNumberOut(BaseModel):
    id: uuid.UUID
    phone_number: str
    provider: str
    direction: str
    status: str


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    persona: str | None
    greeting_message: str
    emergency_message: str
    fallback_message: str
    transfer_enabled: bool
    row_version: int
    tools: list[ToolOut]
    phone_numbers: list[PhoneNumberOut]

    @classmethod
    def of(cls, view: agents.AgentView) -> "AgentOut":
        values = {k: v for k, v in view.__dict__.items() if k not in ("workspace_id", "tools")}
        values["phone_numbers"] = [PhoneNumberOut(**n.__dict__) for n in view.phone_numbers]
        return cls(tools=[ToolOut(**t.__dict__) for t in view.tools], **values)


class AgentIn(Strict):
    name: str = Field(min_length=1, max_length=150)
    slug: str = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    greeting_message: str = Message
    emergency_message: str = Message
    fallback_message: str = Message
    persona: str | None = Field(default=None, max_length=4000)

    def draft(self) -> agents.AgentDraft:
        return agents.AgentDraft(**self.model_dump())


class AgentPatch(Strict):
    """Partial update. `row_version` must be the value last read (409 if it changed)."""

    row_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=150)
    status: Literal["active", "disabled"] | None = None
    persona: str | None = Field(default=None, max_length=4000)
    greeting_message: str | None = Field(default=None, min_length=1, max_length=2000)
    emergency_message: str | None = Field(default=None, min_length=1, max_length=2000)
    fallback_message: str | None = Field(default=None, min_length=1, max_length=2000)

    def changes(self) -> agents.AgentChanges:
        return agents.AgentChanges(**self.model_dump(exclude={"row_version"}))


class ToolIn(Strict):
    enabled: bool
    config: dict[str, Any] | None = None


class PhoneNumberIn(Strict):
    phone_number: str = Field(min_length=8, max_length=16)
    provider: str = Field(min_length=1, max_length=50)
    trusted_trunk_id: str | None = Field(default=None, max_length=200)

"""Request and response models for CRM endpoints.

Lists and details carry no personal data; only the explicit reveal responses do.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from praxima.modules import engagement

E164 = r"^\+[1-9][0-9]{7,14}$"
Phone = Annotated[str, Field(pattern=E164, description="E.164, e.g. +9180...")]
TaskStatus = Literal["open", "done", "cancelled"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkItemKindOut(BaseModel):
    key: str
    name: str
    schema_version: int
    payload_schema: dict[str, Any]
    stages: list[str]
    initial_stage: str
    terminal_stages: list[str]
    subject_types: list[str]

    @classmethod
    def of(cls, view: engagement.WorkItemKindView) -> "WorkItemKindOut":
        return cls(**view.__dict__)


# ---------------------------------------------------------------- work items


class WorkItemOut(BaseModel):
    id: uuid.UUID
    kind: str
    stage: str
    open: bool
    payload: dict[str, Any]
    entity_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    assignee_user_id: uuid.UUID | None
    has_personal_details: bool
    personal_details_erased: bool
    sla_due_at: datetime | None
    row_version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: engagement.WorkItemView) -> "WorkItemOut":
        return cls(**view.__dict__)


class WorkItemEventOut(BaseModel):
    from_stage: str | None
    to_stage: str | None
    actor_type: str
    actor_user_id: uuid.UUID | None
    occurred_at: datetime


class WorkItemDetailOut(BaseModel):
    work_item: WorkItemOut
    history: list[WorkItemEventOut]

    @classmethod
    def of(
        cls, view: engagement.WorkItemView, events: list[engagement.WorkItemEventView]
    ) -> "WorkItemDetailOut":
        return cls(
            work_item=WorkItemOut.of(view),
            history=[WorkItemEventOut(**e.__dict__) for e in events],
        )


class WorkItemIn(Strict):
    kind: str = Field(min_length=2, max_length=63)
    payload: dict[str, Any] = {}
    entity_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    subject_name: str | None = Field(default=None, min_length=1, max_length=200)
    callback_number: Phone | None = None
    staff_note: str | None = Field(default=None, min_length=1, max_length=2000)

    def draft(self, idempotency_key: str) -> engagement.WorkItemDraft:
        return engagement.WorkItemDraft(idempotency_key=idempotency_key, **self.model_dump())


class WorkItemPatch(Strict):
    """Either a stage move, or field changes; one kind of change per request."""

    row_version: int = Field(ge=1)
    stage: str | None = Field(default=None, min_length=1, max_length=63)
    payload: dict[str, Any] | None = None
    staff_note: str | None = Field(default=None, min_length=1, max_length=2000)
    sla_due_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _one_kind_of_change(self) -> "WorkItemPatch":
        fields = (self.payload, self.staff_note, self.sla_due_at)
        changes = any(v is not None for v in fields)
        if self.stage is not None and changes:
            raise ValueError("Change the stage on its own, without other fields.")
        if self.stage is None and not changes:
            raise ValueError("Nothing to change.")
        return self

    def changes(self) -> engagement.WorkItemChanges:
        return engagement.WorkItemChanges(self.payload, self.staff_note, self.sla_due_at)


class AssigneeIn(Strict):
    row_version: int = Field(ge=1)
    assignee_user_id: uuid.UUID | None


class RevealedWorkItemOut(BaseModel):
    subject_name: str | None
    callback_number: str | None
    staff_note: str | None
    erased: bool

    @classmethod
    def of(cls, revealed: engagement.RevealedWorkItem) -> "RevealedWorkItemOut":
        return cls(**revealed.__dict__)


# ---------------------------------------------------------------- contacts


class ContactOut(BaseModel):
    id: uuid.UUID
    preferred_language: str | None
    consent_status: str
    has_personal_details: bool
    personal_details_erased: bool
    created_at: datetime

    @classmethod
    def of(cls, view: engagement.ContactView) -> "ContactOut":
        return cls(**view.__dict__)


class ContactIn(Strict):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: Phone | None = None
    preferred_language: str | None = Field(default=None, min_length=2, max_length=35)

    def draft(self) -> engagement.ContactDraft:
        return engagement.ContactDraft(**self.model_dump())


class ContactSearchIn(Strict):
    """Sent in a body, never a URL, so phone numbers stay out of logs and history."""

    phone: Phone


class ConsentIn(Strict):
    consent_type: str = Field(min_length=2, max_length=63)
    action: Literal["granted", "withdrawn"]
    notice_version: str = Field(min_length=1, max_length=60)


class RevealedContactOut(BaseModel):
    display_name: str | None
    phone: str | None
    erased: bool

    @classmethod
    def of(cls, revealed: engagement.RevealedContact) -> "RevealedContactOut":
        return cls(**revealed.__dict__)


# ---------------------------------------------------------------- conversations


class ConversationOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    contact_id: uuid.UUID | None
    channel: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    language: str | None
    primary_intent: str | None
    disposition: str | None
    safety_flag: bool
    failure_code: str | None
    summary: str | None
    is_test: bool

    @classmethod
    def of(cls, view: engagement.ConversationView) -> "ConversationOut":
        return cls(**view.__dict__)


class CallEventOut(BaseModel):
    event_type: str
    occurred_at: datetime
    sanitized_payload: dict[str, Any] | None


class ConversationDetailOut(BaseModel):
    conversation: ConversationOut
    events: list[CallEventOut]

    @classmethod
    def of(
        cls, view: engagement.ConversationView, events: list[engagement.CallEventView]
    ) -> "ConversationDetailOut":
        return cls(
            conversation=ConversationOut.of(view),
            events=[CallEventOut(**e.__dict__) for e in events],
        )


# ---------------------------------------------------------------- tasks


class TaskOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    status: str
    work_item_id: uuid.UUID | None
    assignee_user_id: uuid.UUID | None
    due_at: datetime | None
    completed_at: datetime | None
    row_version: int
    created_at: datetime

    @classmethod
    def of(cls, view: engagement.TaskView) -> "TaskOut":
        return cls(**view.__dict__)


class TaskIn(Strict):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    work_item_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None
    due_at: AwareDatetime | None = None


class TaskPatch(Strict):
    row_version: int = Field(ge=1)
    status: TaskStatus

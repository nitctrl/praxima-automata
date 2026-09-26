"""Reads for the CRM. Lists and details never contain personal data (see reveal_*)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules.engagement.application.vault import Vault
from praxima.modules.engagement.infrastructure.models import (
    CallEvent,
    Contact,
    Conversation,
    Task,
    WorkItem,
    WorkItemEvent,
    WorkItemKind,
)
from praxima.shared.db.pagination import PageRequest, PageResult, fetch_page
from praxima.shared.errors import NotFound, ValidationFailed


@dataclass(frozen=True)
class WorkItemKindView:
    key: str
    name: str
    schema_version: int
    payload_schema: dict[str, Any]
    stages: list[str]
    initial_stage: str
    terminal_stages: list[str]
    subject_types: list[str]


@dataclass(frozen=True)
class WorkItemView:
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


@dataclass(frozen=True)
class WorkItemEventView:
    from_stage: str | None
    to_stage: str | None
    actor_type: str
    actor_user_id: uuid.UUID | None
    occurred_at: datetime


@dataclass(frozen=True)
class ContactView:
    id: uuid.UUID
    preferred_language: str | None
    consent_status: str
    has_personal_details: bool
    personal_details_erased: bool
    created_at: datetime


@dataclass(frozen=True)
class ConversationView:
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


@dataclass(frozen=True)
class CallEventView:
    event_type: str
    occurred_at: datetime
    sanitized_payload: dict[str, Any] | None


@dataclass(frozen=True)
class TaskView:
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


async def work_item_kinds(session: AsyncSession) -> list[WorkItemKindView]:
    """The newest active version of every kind installed in the workspace."""
    rows = await session.scalars(
        select(WorkItemKind)
        .where(WorkItemKind.status == "active")
        .distinct(WorkItemKind.key)
        .order_by(WorkItemKind.key, WorkItemKind.schema_version.desc())
    )
    return [
        WorkItemKindView(
            k.key,
            k.name,
            k.schema_version,
            k.payload_schema,
            list(k.stages),
            k.initial_stage,
            list(k.terminal_stages),
            list(k.subject_types),
        )
        for k in rows
    ]


def _item_view(item: WorkItem, kind: str, terminal: list[str]) -> WorkItemView:
    has_details = any(
        v is not None
        for v in (
            item.subject_name_ciphertext,
            item.callback_number_ciphertext,
            item.staff_note_ciphertext,
        )
    )
    return WorkItemView(
        item.id,
        kind,
        item.stage,
        item.stage not in terminal,
        item.payload,
        item.entity_id,
        item.contact_id,
        item.conversation_id,
        item.assignee_user_id,
        has_details,
        item.pii_erased_at is not None,
        item.sla_due_at,
        item.row_version,
        item.created_at,
        item.updated_at,
    )


async def work_items_page(
    session: AsyncSession,
    page: PageRequest,
    *,
    kind: str | None = None,
    stage: str | None = None,
    open_only: bool = False,
    assignee_user_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
) -> PageResult:
    """Newest first; the kind comes from a join in the same query (no N+1)."""
    conditions: list[ColumnElement[bool]] = []
    if kind is not None:
        conditions.append(WorkItemKind.key == kind)
    if stage is not None:
        conditions.append(WorkItem.stage == stage)
    if open_only:
        conditions.append(~WorkItem.stage.op("=")(WorkItemKind.terminal_stages.any_()))
    if assignee_user_id is not None:
        conditions.append(WorkItem.assignee_user_id == assignee_user_id)
    if entity_id is not None:
        conditions.append(WorkItem.entity_id == entity_id)
    statement = (
        select(WorkItem, WorkItemKind.key.label("kind"), WorkItemKind.terminal_stages)
        .join(WorkItemKind, WorkItemKind.id == WorkItem.kind_id)
        .where(and_(True, *conditions))
    )
    result = await fetch_page(session, statement, (WorkItem.created_at, WorkItem.id), page)
    return PageResult(
        [_item_view(r.WorkItem, r.kind, list(r.terminal_stages)) for r in result.items],
        result.next_cursor,
    )


async def get_work_item(
    session: AsyncSession, work_item_id: uuid.UUID
) -> tuple[WorkItemView, list[WorkItemEventView]]:
    """An item with its stage history, oldest first (two queries)."""
    row = (
        await session.execute(
            select(WorkItem, WorkItemKind.key.label("kind"), WorkItemKind.terminal_stages)
            .join(WorkItemKind, WorkItemKind.id == WorkItem.kind_id)
            .where(WorkItem.id == work_item_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Work item not found.")
    events = await session.scalars(
        select(WorkItemEvent)
        .where(WorkItemEvent.work_item_id == work_item_id)
        .order_by(WorkItemEvent.occurred_at, WorkItemEvent.id)
    )
    return _item_view(row.WorkItem, row.kind, list(row.terminal_stages)), [
        WorkItemEventView(e.from_stage, e.to_stage, e.actor_type, e.actor_user_id, e.occurred_at)
        for e in events
    ]


def _contact_view(c: Contact) -> ContactView:
    return ContactView(
        c.id,
        c.preferred_language,
        c.consent_status,
        c.display_name_ciphertext is not None or c.phone_ciphertext is not None,
        c.pii_erased_at is not None,
        c.created_at,
    )


async def contacts_page(session: AsyncSession, page: PageRequest) -> PageResult:
    result = await fetch_page(session, select(Contact), (Contact.created_at, Contact.id), page)
    return PageResult([_contact_view(c) for c in result.items], result.next_cursor)


async def get_contact(session: AsyncSession, contact_id: uuid.UUID) -> ContactView:
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise NotFound("Contact not found.")
    return _contact_view(contact)


async def find_contact_by_phone(
    session: AsyncSession, vault: Vault, workspace_id: uuid.UUID, phone: str
) -> ContactView | None:
    """Exact match via the workspace-specific digest; the phone is never compared in clear."""
    try:
        digest = vault.phone_digest(workspace_id, phone)
    except ValueError:
        raise ValidationFailed("Use an E.164 phone number, e.g. +9180...") from None
    contact = await session.scalar(select(Contact).where(Contact.phone_lookup_hmac == digest))
    return _contact_view(contact) if contact else None


def _conversation_view(c: Conversation) -> ConversationView:
    return ConversationView(
        c.id,
        c.agent_id,
        c.contact_id,
        c.channel,
        c.status,
        c.started_at,
        c.ended_at,
        c.language,
        c.primary_intent,
        c.disposition,
        c.safety_flag,
        c.failure_code,
        c.summary,
        c.is_test,
    )


async def conversations_page(
    session: AsyncSession,
    page: PageRequest,
    *,
    needs_review: bool = False,
    include_tests: bool = False,
) -> PageResult:
    """Newest first. `needs_review`: failed or safety-flagged real calls."""
    conditions: list[ColumnElement[bool]] = []
    if not include_tests:
        conditions.append(Conversation.is_test.is_(False))
    if needs_review:
        conditions.append(
            or_(Conversation.failure_code.is_not(None), Conversation.safety_flag.is_(True))
        )
    statement = select(Conversation).where(and_(True, *conditions))
    result = await fetch_page(session, statement, (Conversation.started_at, Conversation.id), page)
    return PageResult([_conversation_view(c) for c in result.items], result.next_cursor)


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> tuple[ConversationView, list[CallEventView]]:
    """A conversation and its call timeline (two queries)."""
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFound("Conversation not found.")
    events = await session.scalars(
        select(CallEvent)
        .where(CallEvent.conversation_id == conversation_id)
        .order_by(CallEvent.occurred_at, CallEvent.id)
    )
    return _conversation_view(conversation), [
        CallEventView(e.event_type, e.occurred_at, e.sanitized_payload) for e in events
    ]


def _task_view(t: Task) -> TaskView:
    return TaskView(
        t.id,
        t.title,
        t.description,
        t.status,
        t.work_item_id,
        t.assignee_user_id,
        t.due_at,
        t.completed_at,
        t.row_version,
        t.created_at,
    )


async def tasks_page(
    session: AsyncSession,
    page: PageRequest,
    *,
    status: str | None = None,
    assignee_user_id: uuid.UUID | None = None,
    work_item_id: uuid.UUID | None = None,
) -> PageResult:
    conditions: list[ColumnElement[bool]] = [Task.deleted_at.is_(None)]
    if status is not None:
        conditions.append(Task.status == status)
    if assignee_user_id is not None:
        conditions.append(Task.assignee_user_id == assignee_user_id)
    if work_item_id is not None:
        conditions.append(Task.work_item_id == work_item_id)
    result = await fetch_page(
        session, select(Task).where(and_(*conditions)), (Task.created_at, Task.id), page
    )
    return PageResult([_task_view(t) for t in result.items], result.next_cursor)


async def get_task(session: AsyncSession, task_id: uuid.UUID) -> TaskView:
    task = await session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        raise NotFound("Task not found.")
    return _task_view(task)

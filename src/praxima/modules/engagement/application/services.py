"""Writes for the CRM: contacts, consents, work items and tasks.

Personal data is sealed by the Vault before it reaches the database and opened only in
explicit, audited `reveal_*` calls. Audit entries record field names, never values.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import audit, catalog, iam, tenancy
from praxima.modules.engagement.application.vault import Vault
from praxima.modules.engagement.domain.work_items import check_transition
from praxima.modules.engagement.infrastructure.models import (
    Consent,
    Contact,
    Task,
    WorkItem,
    WorkItemEvent,
    WorkItemKind,
)
from praxima.modules.iam import Actor, require
from praxima.shared.db.base import utc_now
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import Conflict, FieldError, NotFound, ValidationFailed
from praxima.shared.kernel.ids import new_id
from praxima.shared.security.lookup import E164
from praxima.shared.validation import schema_errors


@dataclass(frozen=True)
class ContactDraft:
    display_name: str | None = None
    phone: str | None = None
    preferred_language: str | None = None


@dataclass(frozen=True)
class WorkItemDraft:
    kind: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    entity_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    subject_name: str | None = None
    callback_number: str | None = None
    staff_note: str | None = None


@dataclass(frozen=True)
class WorkItemChanges:
    payload: dict[str, Any] | None = None
    staff_note: str | None = None
    sla_due_at: datetime | None = None


@dataclass(frozen=True)
class RevealedContact:
    display_name: str | None
    phone: str | None
    erased: bool


@dataclass(frozen=True)
class RevealedWorkItem:
    subject_name: str | None
    callback_number: str | None
    staff_note: str | None
    erased: bool


async def _audit(
    session: AsyncSession,
    actor: Actor,
    workspace_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    change_diff: dict[str, Any] | None = None,
) -> None:
    await audit.record(
        session,
        organization_id=await tenancy.organization_of(session, workspace_id),
        workspace_id=workspace_id,
        actor_id=actor.user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        change_diff=change_diff,
    )


# ---------------------------------------------------------------- pack install


async def install_work_item_kinds(
    session: AsyncSession, actor: Actor, workspace_id: uuid.UUID
) -> list[str]:
    """Copy the workspace pack's work item kinds into it. Idempotent; returns keys added."""
    require(actor, "packs:install")
    pack = await tenancy.installed_pack(session, workspace_id)
    existing = set(
        (await session.execute(select(WorkItemKind.key, WorkItemKind.schema_version))).tuples()
    )
    added = []
    for spec in pack.work_item_kinds:
        if (spec.key, spec.schema_version) in existing:
            continue
        session.add(
            WorkItemKind(
                workspace_id=workspace_id,
                key=spec.key,
                name=spec.name,
                schema_version=spec.schema_version,
                payload_schema=spec.payload_schema,
                stages=list(spec.stages),
                initial_stage=spec.initial_stage,
                terminal_stages=list(spec.terminal_stages),
                subject_types=list(spec.subject_types),
                source_pack_key=pack.key,
                source_pack_version=pack.version,
            )
        )
        added.append(spec.key)
    with translate_db_errors():
        await session.flush()
    return added


# ---------------------------------------------------------------- contacts


async def create_contact(
    session: AsyncSession,
    actor: Actor,
    vault: Vault,
    *,
    workspace_id: uuid.UUID,
    draft: ContactDraft,
) -> uuid.UUID:
    require(actor, "crm:write")
    contact_id = new_id()
    try:
        digest = vault.phone_digest(workspace_id, draft.phone) if draft.phone else None
    except ValueError:
        raise ValidationFailed(errors=[FieldError("phone", "Use E.164, e.g. +9180...")]) from None
    sealed = draft.display_name is not None or draft.phone is not None
    contact = Contact(
        id=contact_id,
        workspace_id=workspace_id,
        display_name_ciphertext=vault.seal(
            workspace_id, contact_id, "display_name", draft.display_name
        ),
        phone_ciphertext=vault.seal(workspace_id, contact_id, "phone", draft.phone),
        phone_lookup_hmac=digest,
        pii_key_version=vault.version if sealed else None,
        preferred_language=draft.preferred_language,
    )
    with translate_db_errors(duplicate="A contact with this phone number already exists."):
        session.add(contact)
        await session.flush()
    await _audit(session, actor, workspace_id, "contact.create", "contact", contact_id)
    return contact_id


async def _contact(session: AsyncSession, contact_id: uuid.UUID) -> Contact:
    contact = await session.get(Contact, contact_id)
    if contact is None:
        raise NotFound("Contact not found.")
    return contact


async def reveal_contact(
    session: AsyncSession, actor: Actor, vault: Vault, *, contact_id: uuid.UUID
) -> RevealedContact:
    """Decrypt a contact's details. Every call is audited (who, when, which record)."""
    require(actor, "pii:reveal")
    contact = await _contact(session, contact_id)
    await _audit(session, actor, contact.workspace_id, "pii.reveal", "contact", contact.id)
    if contact.pii_erased_at is not None:
        return RevealedContact(None, None, erased=True)
    ws, version = contact.workspace_id, contact.pii_key_version
    return RevealedContact(
        vault.open(ws, contact.id, "display_name", contact.display_name_ciphertext, version),
        vault.open(ws, contact.id, "phone", contact.phone_ciphertext, version),
        erased=False,
    )


async def erase_contact(session: AsyncSession, actor: Actor, *, contact_id: uuid.UUID) -> None:
    """Right to erasure: drop the ciphertext and lookup digest; keep the audit trail."""
    require(actor, "pii:erase")
    contact = await _contact(session, contact_id)
    contact.display_name_ciphertext = contact.phone_ciphertext = None
    contact.phone_lookup_hmac = contact.pii_key_version = None
    contact.pii_erased_at = utc_now()
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, contact.workspace_id, "pii.erase", "contact", contact.id)


async def record_consent(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    consent_type: str,
    action: str,
    notice_version: str,
    source: str,
    conversation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Append a consent decision (and update the contact's current status)."""
    require(actor, "crm:write")
    if action not in ("granted", "withdrawn"):
        raise ValidationFailed(errors=[FieldError("action", "Use granted or withdrawn.")])
    contact = await _contact(session, contact_id)
    consent = Consent(
        workspace_id=workspace_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        consent_type=consent_type,
        action=action,
        notice_version=notice_version,
        source=source,
    )
    contact.consent_status = action
    contact.consented_at = consent.recorded_at = utc_now()
    with translate_db_errors(reference="The conversation doesn't exist."):
        session.add(consent)
        await session.flush()
    await _audit(session, actor, workspace_id, f"consent.{action}", "contact", contact_id)
    return consent.id


# ---------------------------------------------------------------- work items


async def _current_kind(session: AsyncSession, key: str) -> WorkItemKind:
    kind = await session.scalar(
        select(WorkItemKind)
        .where(WorkItemKind.key == key, WorkItemKind.status == "active")
        .order_by(WorkItemKind.schema_version.desc())
        .limit(1)
    )
    if kind is None:
        raise ValidationFailed(errors=[FieldError("kind", "Unknown work item kind.")])
    return kind


async def _check_subject(session: AsyncSession, kind: WorkItemKind, entity_id: uuid.UUID) -> None:
    try:
        type_key: str | None = (await catalog.get_entity(session, entity_id)).type
    except NotFound:
        type_key = None
    if type_key not in kind.subject_types:
        allowed = ", ".join(kind.subject_types) or "nothing"
        raise ValidationFailed(errors=[FieldError("entity_id", f"Must be one of: {allowed}.")])


def _check_payload(kind: WorkItemKind, payload: dict[str, Any]) -> None:
    if errors := schema_errors(kind.payload_schema, payload, "payload"):
        raise ValidationFailed(errors=errors)


async def create_work_item(
    session: AsyncSession,
    actor: Actor,
    vault: Vault,
    *,
    workspace_id: uuid.UUID,
    draft: WorkItemDraft,
    actor_type: str = "user",
) -> uuid.UUID:
    """Open a request or lead. Idempotent: the same key returns the existing item."""
    require(actor, "crm:write")
    existing = await session.scalar(
        select(WorkItem.id).where(WorkItem.idempotency_key == draft.idempotency_key)
    )
    if existing is not None:
        return existing
    kind = await _current_kind(session, draft.kind)
    _check_payload(kind, draft.payload)
    if draft.entity_id is not None:
        await _check_subject(session, kind, draft.entity_id)
    if draft.callback_number is not None and not E164.fullmatch(draft.callback_number):
        raise ValidationFailed(errors=[FieldError("callback_number", "Use E.164, e.g. +9180...")])
    item_id = new_id()
    personal = (draft.subject_name, draft.callback_number, draft.staff_note)
    item = WorkItem(
        id=item_id,
        workspace_id=workspace_id,
        kind_id=kind.id,
        conversation_id=draft.conversation_id,
        contact_id=draft.contact_id,
        entity_id=draft.entity_id,
        stage=kind.initial_stage,
        payload=draft.payload,
        subject_name_ciphertext=vault.seal(
            workspace_id, item_id, "subject_name", draft.subject_name
        ),
        callback_number_ciphertext=vault.seal(
            workspace_id, item_id, "callback_number", draft.callback_number
        ),
        staff_note_ciphertext=vault.seal(workspace_id, item_id, "staff_note", draft.staff_note),
        pii_key_version=vault.version if any(v is not None for v in personal) else None,
        idempotency_key=draft.idempotency_key,
        updated_by=actor.user_id,
    )
    with translate_db_errors(reference="A linked contact, conversation or item doesn't exist."):
        session.add(item)
        await session.flush()
        session.add(
            WorkItemEvent(
                workspace_id=workspace_id,
                work_item_id=item_id,
                from_stage=None,
                to_stage=kind.initial_stage,
                actor_type=actor_type,
                actor_user_id=actor.user_id if actor_type == "user" else None,
            )
        )
        await session.flush()
    await _audit(session, actor, workspace_id, "work_item.create", "work_item", item_id)
    return item_id


async def _work_item(
    session: AsyncSession, work_item_id: uuid.UUID, row_version: int | None = None
) -> WorkItem:
    item = await session.get(WorkItem, work_item_id)
    if item is None:
        raise NotFound("Work item not found.")
    if row_version is not None and item.row_version != row_version:
        raise Conflict("This item was changed by someone else. Reload and try again.")
    return item


async def move_work_item(
    session: AsyncSession,
    actor: Actor,
    *,
    work_item_id: uuid.UUID,
    row_version: int,
    stage: str,
) -> None:
    """Move to another stage of its kind (never out of a closed one); history is kept."""
    require(actor, "crm:write")
    item = await _work_item(session, work_item_id, row_version)
    kind = await session.get(WorkItemKind, item.kind_id)
    assert kind is not None  # composite FK guarantees it
    check_transition(kind.stages, kind.terminal_stages, item.stage, stage)
    previous, item.stage, item.updated_by = item.stage, stage, actor.user_id
    session.add(
        WorkItemEvent(
            workspace_id=item.workspace_id,
            work_item_id=item.id,
            from_stage=previous,
            to_stage=stage,
            actor_type="user",
            actor_user_id=actor.user_id,
        )
    )
    with translate_db_errors():
        await session.flush()
    await _audit(
        session,
        actor,
        item.workspace_id,
        "work_item.move",
        "work_item",
        item.id,
        {"from": previous, "to": stage},
    )


async def _check_member(session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    organization_id = await tenancy.organization_of(session, workspace_id)
    if await iam.role_in(session, user_id, organization_id, workspace_id) is None:
        raise ValidationFailed(
            errors=[FieldError("assignee_user_id", "Not a member of this workspace.")]
        )


async def assign_work_item(
    session: AsyncSession,
    actor: Actor,
    *,
    work_item_id: uuid.UUID,
    row_version: int,
    assignee_user_id: uuid.UUID | None,
) -> None:
    """Assign to a member of this workspace (or unassign with None)."""
    require(actor, "crm:assign")
    item = await _work_item(session, work_item_id, row_version)
    if assignee_user_id is not None:
        await _check_member(session, item.workspace_id, assignee_user_id)
    item.assignee_user_id, item.updated_by = assignee_user_id, actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, item.workspace_id, "work_item.assign", "work_item", item.id)


async def update_work_item(
    session: AsyncSession,
    actor: Actor,
    vault: Vault,
    *,
    work_item_id: uuid.UUID,
    row_version: int,
    changes: WorkItemChanges,
) -> None:
    require(actor, "crm:write")
    item = await _work_item(session, work_item_id, row_version)
    fields: list[str] = []
    if changes.payload is not None:
        kind = await session.get(WorkItemKind, item.kind_id)
        assert kind is not None
        _check_payload(kind, changes.payload)
        item.payload, fields = changes.payload, [*fields, "payload"]
    if changes.staff_note is not None:
        if item.pii_erased_at is not None:
            raise Conflict("Personal data of this item was erased.")
        # Re-sealing the note under the current key keeps one key version per row.
        _reseal(vault, item)
        item.staff_note_ciphertext = vault.seal(
            item.workspace_id, item.id, "staff_note", changes.staff_note
        )
        item.pii_key_version, fields = vault.version, [*fields, "staff_note"]
    if changes.sla_due_at is not None:
        item.sla_due_at, fields = changes.sla_due_at, [*fields, "sla_due_at"]
    if not fields:
        raise ValidationFailed("Nothing to change.")
    item.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(
        session,
        actor,
        item.workspace_id,
        "work_item.update",
        "work_item",
        item.id,
        {"fields": fields},
    )


def _reseal(vault: Vault, item: WorkItem) -> None:
    """Re-encrypt the other personal fields under the vault's current key version."""
    if item.pii_key_version in (None, vault.version):
        return
    ws, old = item.workspace_id, item.pii_key_version
    for name in ("subject_name", "callback_number"):
        column = f"{name}_ciphertext"
        plain = vault.open(ws, item.id, name, getattr(item, column), old)
        setattr(item, column, vault.seal(ws, item.id, name, plain))


async def reveal_work_item(
    session: AsyncSession, actor: Actor, vault: Vault, *, work_item_id: uuid.UUID
) -> RevealedWorkItem:
    """Decrypt a work item's personal fields. Every call is audited."""
    require(actor, "pii:reveal")
    item = await _work_item(session, work_item_id)
    await _audit(session, actor, item.workspace_id, "pii.reveal", "work_item", item.id)
    if item.pii_erased_at is not None:
        return RevealedWorkItem(None, None, None, erased=True)
    ws, version = item.workspace_id, item.pii_key_version
    return RevealedWorkItem(
        vault.open(ws, item.id, "subject_name", item.subject_name_ciphertext, version),
        vault.open(ws, item.id, "callback_number", item.callback_number_ciphertext, version),
        vault.open(ws, item.id, "staff_note", item.staff_note_ciphertext, version),
        erased=False,
    )


async def erase_work_item(session: AsyncSession, actor: Actor, *, work_item_id: uuid.UUID) -> None:
    require(actor, "pii:erase")
    item = await _work_item(session, work_item_id)
    item.subject_name_ciphertext = item.callback_number_ciphertext = None
    item.staff_note_ciphertext = item.pii_key_version = None
    item.pii_erased_at, item.updated_by = utc_now(), actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, item.workspace_id, "pii.erase", "work_item", item.id)


# ---------------------------------------------------------------- tasks


async def create_task(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    title: str,
    description: str | None = None,
    work_item_id: uuid.UUID | None = None,
    assignee_user_id: uuid.UUID | None = None,
    due_at: datetime | None = None,
) -> uuid.UUID:
    require(actor, "crm:write")
    if assignee_user_id is not None:
        await _check_member(session, workspace_id, assignee_user_id)
    task = Task(
        workspace_id=workspace_id,
        title=title,
        description=description,
        work_item_id=work_item_id,
        assignee_user_id=assignee_user_id,
        due_at=due_at,
        created_by=actor.user_id,
    )
    with translate_db_errors(reference="The linked item or user doesn't exist."):
        session.add(task)
        await session.flush()
    await _audit(session, actor, workspace_id, "task.create", "task", task.id)
    return task.id


async def set_task_status(
    session: AsyncSession, actor: Actor, *, task_id: uuid.UUID, row_version: int, status: str
) -> None:
    """Complete, cancel or reopen a task (completion time is recorded)."""
    require(actor, "crm:write")
    if status not in ("open", "done", "cancelled"):
        raise ValidationFailed(errors=[FieldError("status", "Unknown status.")])
    task = await session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        raise NotFound("Task not found.")
    if task.row_version != row_version:
        raise Conflict("This task was changed by someone else. Reload and try again.")
    task.status, task.updated_by = status, actor.user_id
    task.completed_at = utc_now() if status == "done" else None
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, task.workspace_id, f"task.{status}", "task", task.id)

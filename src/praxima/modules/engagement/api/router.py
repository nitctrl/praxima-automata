"""CRM endpoints: work items, contacts, conversations, tasks. No logic or queries.

Personal data leaves the API only through the audited POST .../reveal endpoints.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from praxima.entrypoints.http.deps import Paging, PiiVault, WorkspaceAccess
from praxima.entrypoints.http.responses import Page, PageInfo
from praxima.modules import engagement, iam
from praxima.modules.engagement.api.schemas import (
    AssigneeIn,
    ConsentIn,
    ContactIn,
    ContactOut,
    ContactSearchIn,
    ConversationDetailOut,
    ConversationOut,
    RevealedContactOut,
    RevealedWorkItemOut,
    TaskIn,
    TaskOut,
    TaskPatch,
    TaskStatus,
    WorkItemDetailOut,
    WorkItemIn,
    WorkItemKindOut,
    WorkItemOut,
    WorkItemPatch,
)
from praxima.shared.errors import NotFound
from praxima.shared.kernel.ids import new_id

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["crm"])
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT
IdempotencyKey = Annotated[
    str | None,
    Header(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Send the same key when retrying; the first item is returned.",
    ),
]


def _workspace(access: WorkspaceAccess) -> uuid.UUID:
    assert access.workspace_id is not None  # WorkspaceAccess always scopes a workspace
    return access.workspace_id


# ---------------------------------------------------------------- work items


@router.get("/work-item-kinds")
async def list_work_item_kinds(access: WorkspaceAccess) -> Page[WorkItemKindOut]:
    """Installed from the pack: payload schema, stages and subject types (a short list)."""
    iam.require(access.actor, "crm:read")
    kinds = [WorkItemKindOut.of(k) for k in await engagement.work_item_kinds(access.session)]
    return Page(data=kinds, page=PageInfo(limit=len(kinds), next_cursor=None))


@router.get("/work-items")
async def list_work_items(
    access: WorkspaceAccess,
    page: Paging,
    kind: Annotated[str | None, Query(max_length=63)] = None,
    stage: Annotated[str | None, Query(max_length=63)] = None,
    open: Annotated[bool, Query(description="Only items not in a terminal stage")] = False,
    assignee_user_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
) -> Page[WorkItemOut]:
    iam.require(access.actor, "crm:read")
    result = await engagement.work_items_page(
        access.session,
        page,
        kind=kind,
        stage=stage,
        open_only=open,
        assignee_user_id=assignee_user_id,
        entity_id=entity_id,
    )
    return Page.build(result, [WorkItemOut.of(i) for i in result.items], page.limit)


@router.post("/work-items", status_code=CREATED)
async def create_work_item(
    body: WorkItemIn,
    access: WorkspaceAccess,
    vault: PiiVault,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> WorkItemDetailOut:
    """Record a request by hand; payload is validated against the kind's schema."""
    item_id = await engagement.create_work_item(
        access.session,
        access.actor,
        vault,
        workspace_id=_workspace(access),
        draft=body.draft(idempotency_key or f"staff:{new_id()}"),
    )
    response.headers["Location"] = f"/api/v1/workspaces/{access.workspace_id}/work-items/{item_id}"
    return WorkItemDetailOut.of(*await engagement.get_work_item(access.session, item_id))


@router.get("/work-items/{work_item_id}")
async def read_work_item(work_item_id: uuid.UUID, access: WorkspaceAccess) -> WorkItemDetailOut:
    iam.require(access.actor, "crm:read")
    return WorkItemDetailOut.of(*await engagement.get_work_item(access.session, work_item_id))


@router.patch("/work-items/{work_item_id}")
async def update_work_item(
    work_item_id: uuid.UUID, body: WorkItemPatch, access: WorkspaceAccess, vault: PiiVault
) -> WorkItemDetailOut:
    """Move to another stage (`{stage}`), or change payload, staff note or due time."""
    if body.stage is not None:
        await engagement.move_work_item(
            access.session,
            access.actor,
            work_item_id=work_item_id,
            row_version=body.row_version,
            stage=body.stage,
        )
    else:
        await engagement.update_work_item(
            access.session,
            access.actor,
            vault,
            work_item_id=work_item_id,
            row_version=body.row_version,
            changes=body.changes(),
        )
    return WorkItemDetailOut.of(*await engagement.get_work_item(access.session, work_item_id))


@router.put("/work-items/{work_item_id}/assignee")
async def assign_work_item(
    work_item_id: uuid.UUID, body: AssigneeIn, access: WorkspaceAccess
) -> WorkItemOut:
    """Assign to a workspace member, or unassign with `null` (manager+)."""
    await engagement.assign_work_item(
        access.session,
        access.actor,
        work_item_id=work_item_id,
        row_version=body.row_version,
        assignee_user_id=body.assignee_user_id,
    )
    view, _ = await engagement.get_work_item(access.session, work_item_id)
    return WorkItemOut.of(view)


@router.post("/work-items/{work_item_id}/reveal")
async def reveal_work_item(
    work_item_id: uuid.UUID, access: WorkspaceAccess, vault: PiiVault
) -> RevealedWorkItemOut:
    """Decrypt the subject's name, callback number and staff note (staff+, audited)."""
    return RevealedWorkItemOut.of(
        await engagement.reveal_work_item(
            access.session, access.actor, vault, work_item_id=work_item_id
        )
    )


@router.delete("/work-items/{work_item_id}/personal-details", status_code=NO_CONTENT)
async def erase_work_item(work_item_id: uuid.UUID, access: WorkspaceAccess) -> None:
    """Erase the item's personal details for good (admin+); the item and history stay."""
    await engagement.erase_work_item(access.session, access.actor, work_item_id=work_item_id)


# ---------------------------------------------------------------- contacts


@router.get("/contacts")
async def list_contacts(access: WorkspaceAccess, page: Paging) -> Page[ContactOut]:
    iam.require(access.actor, "crm:read")
    result = await engagement.contacts_page(access.session, page)
    return Page.build(result, [ContactOut.of(c) for c in result.items], page.limit)


@router.post("/contacts", status_code=CREATED)
async def create_contact(
    body: ContactIn, access: WorkspaceAccess, vault: PiiVault, response: Response
) -> ContactOut:
    contact_id = await engagement.create_contact(
        access.session, access.actor, vault, workspace_id=_workspace(access), draft=body.draft()
    )
    response.headers["Location"] = f"/api/v1/workspaces/{access.workspace_id}/contacts/{contact_id}"
    return ContactOut.of(await engagement.get_contact(access.session, contact_id))


@router.post("/contacts/search")
async def find_contact(
    body: ContactSearchIn, access: WorkspaceAccess, vault: PiiVault
) -> ContactOut:
    """Exact phone match (POST so the number never appears in a URL). 404 if none."""
    iam.require(access.actor, "crm:read")
    found = await engagement.find_contact_by_phone(
        access.session, vault, _workspace(access), body.phone
    )
    if found is None:
        raise NotFound("No contact with this phone number.")
    return ContactOut.of(found)


@router.get("/contacts/{contact_id}")
async def read_contact(contact_id: uuid.UUID, access: WorkspaceAccess) -> ContactOut:
    iam.require(access.actor, "crm:read")
    return ContactOut.of(await engagement.get_contact(access.session, contact_id))


@router.post("/contacts/{contact_id}/consents", status_code=CREATED)
async def record_consent(
    contact_id: uuid.UUID, body: ConsentIn, access: WorkspaceAccess
) -> ContactOut:
    """Append a consent decision given to staff; returns the contact's new status."""
    await engagement.record_consent(
        access.session,
        access.actor,
        workspace_id=_workspace(access),
        contact_id=contact_id,
        consent_type=body.consent_type,
        action=body.action,
        notice_version=body.notice_version,
        source="staff",
    )
    return ContactOut.of(await engagement.get_contact(access.session, contact_id))


@router.post("/contacts/{contact_id}/reveal")
async def reveal_contact(
    contact_id: uuid.UUID, access: WorkspaceAccess, vault: PiiVault
) -> RevealedContactOut:
    """Decrypt the contact's name and phone (staff+, audited)."""
    return RevealedContactOut.of(
        await engagement.reveal_contact(access.session, access.actor, vault, contact_id=contact_id)
    )


@router.delete("/contacts/{contact_id}/personal-details", status_code=NO_CONTENT)
async def erase_contact(contact_id: uuid.UUID, access: WorkspaceAccess) -> None:
    """Erase the contact's name, phone and lookup digest (admin+); history stays."""
    await engagement.erase_contact(access.session, access.actor, contact_id=contact_id)


# ---------------------------------------------------------------- conversations


@router.get("/conversations")
async def list_conversations(
    access: WorkspaceAccess,
    page: Paging,
    needs_review: Annotated[bool, Query(description="Failed or safety-flagged")] = False,
    include_tests: bool = False,
) -> Page[ConversationOut]:
    iam.require(access.actor, "crm:read")
    result = await engagement.conversations_page(
        access.session, page, needs_review=needs_review, include_tests=include_tests
    )
    return Page.build(result, [ConversationOut.of(c) for c in result.items], page.limit)


@router.get("/conversations/{conversation_id}")
async def read_conversation(
    conversation_id: uuid.UUID, access: WorkspaceAccess
) -> ConversationDetailOut:
    """Sanitized outcome and call timeline; never transcripts or audio."""
    iam.require(access.actor, "crm:read")
    return ConversationDetailOut.of(
        *await engagement.get_conversation(access.session, conversation_id)
    )


# ---------------------------------------------------------------- tasks


@router.get("/tasks")
async def list_tasks(
    access: WorkspaceAccess,
    page: Paging,
    status: Annotated[TaskStatus | None, Query()] = None,
    assignee_user_id: uuid.UUID | None = None,
    work_item_id: uuid.UUID | None = None,
) -> Page[TaskOut]:
    iam.require(access.actor, "crm:read")
    result = await engagement.tasks_page(
        access.session,
        page,
        status=status,
        assignee_user_id=assignee_user_id,
        work_item_id=work_item_id,
    )
    return Page.build(result, [TaskOut.of(t) for t in result.items], page.limit)


@router.post("/tasks", status_code=CREATED)
async def create_task(body: TaskIn, access: WorkspaceAccess, response: Response) -> TaskOut:
    task_id = await engagement.create_task(
        access.session, access.actor, workspace_id=_workspace(access), **body.model_dump()
    )
    response.headers["Location"] = f"/api/v1/workspaces/{access.workspace_id}/tasks/{task_id}"
    return TaskOut.of(await engagement.get_task(access.session, task_id))


@router.get("/tasks/{task_id}")
async def read_task(task_id: uuid.UUID, access: WorkspaceAccess) -> TaskOut:
    iam.require(access.actor, "crm:read")
    return TaskOut.of(await engagement.get_task(access.session, task_id))


@router.patch("/tasks/{task_id}")
async def set_task_status(task_id: uuid.UUID, body: TaskPatch, access: WorkspaceAccess) -> TaskOut:
    """Complete, cancel or reopen."""
    await engagement.set_task_status(
        access.session,
        access.actor,
        task_id=task_id,
        row_version=body.row_version,
        status=body.status,
    )
    return TaskOut.of(await engagement.get_task(access.session, task_id))

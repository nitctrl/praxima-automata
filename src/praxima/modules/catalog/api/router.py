"""Catalog endpoints: entity types, entities, relations and hours. No logic or queries here."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from praxima.entrypoints.http.deps import Paging, WorkspaceAccess
from praxima.entrypoints.http.responses import Page, PageInfo
from praxima.modules import catalog, iam
from praxima.modules.catalog.api.schemas import (
    AvailabilityOut,
    CreatedOut,
    EntityIn,
    EntityOut,
    EntityPatch,
    EntityTypeOut,
    ExceptionIn,
    ExceptionOut,
    RelationIn,
    RelationOut,
    RelationTypeOut,
    RuleIn,
    RuleOut,
    Status,
    StatusIn,
)

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["catalog"])
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT


def _workspace(access: WorkspaceAccess) -> uuid.UUID:
    assert access.workspace_id is not None  # WorkspaceAccess always scopes a workspace
    return access.workspace_id


@router.get("/entity-types")
async def list_entity_types(access: WorkspaceAccess) -> Page[EntityTypeOut]:
    iam.require(access.actor, "catalog:read")
    types = [EntityTypeOut.of(t) for t in await catalog.entity_types(access.session)]
    return Page(data=types, page=PageInfo(limit=len(types), next_cursor=None))


@router.get("/entities")
async def list_entities(
    access: WorkspaceAccess,
    page: Paging,
    type: Annotated[str | None, Query(max_length=63)] = None,
    status: Annotated[Status | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
) -> Page[EntityOut]:
    """Newest first. `q` matches names and aliases (case-insensitive)."""
    iam.require(access.actor, "catalog:read")
    result = await catalog.entities_page(
        access.session, page, type_key=type, status=status, search=q
    )
    return Page.build(result, [EntityOut.of(e) for e in result.items], page.limit)


@router.post("/entities", status_code=CREATED)
async def create_entity(
    body: EntityIn, access: WorkspaceAccess, request: Request, response: Response
) -> EntityOut:
    entity_id = await catalog.create_entity(
        access.session, access.actor, workspace_id=_workspace(access), draft=body.draft()
    )
    response.headers["Location"] = f"{request.url.path}/{entity_id}"
    return EntityOut.of(await catalog.get_entity(access.session, entity_id))


@router.get("/entities/{entity_id}")
async def read_entity(entity_id: uuid.UUID, access: WorkspaceAccess) -> EntityOut:
    iam.require(access.actor, "catalog:read")
    return EntityOut.of(await catalog.get_entity(access.session, entity_id))


@router.patch("/entities/{entity_id}")
async def update_entity(
    entity_id: uuid.UUID, body: EntityPatch, access: WorkspaceAccess
) -> EntityOut:
    await catalog.update_entity(
        access.session,
        access.actor,
        entity_id=entity_id,
        row_version=body.row_version,
        changes=body.changes(),
        status=body.publication_status,
    )
    return EntityOut.of(await catalog.get_entity(access.session, entity_id))


@router.delete("/entities/{entity_id}", status_code=NO_CONTENT)
async def delete_entity(
    entity_id: uuid.UUID,
    access: WorkspaceAccess,
    row_version: Annotated[int, Query(ge=1)],
) -> None:
    await catalog.delete_entity(
        access.session, access.actor, entity_id=entity_id, row_version=row_version
    )


@router.get("/relation-types")
async def list_relation_types(access: WorkspaceAccess) -> Page[RelationTypeOut]:
    """Link types from the workspace's pack: which entity types they join, and their fields."""
    iam.require(access.actor, "catalog:read")
    types = [
        RelationTypeOut.of(r)
        for r in await catalog.relation_types(access.session, _workspace(access))
    ]
    return Page(data=types, page=PageInfo(limit=len(types), next_cursor=None))


@router.get("/entities/{entity_id}/relations")
async def list_relations(entity_id: uuid.UUID, access: WorkspaceAccess) -> Page[RelationOut]:
    iam.require(access.actor, "catalog:read")
    await catalog.get_entity(access.session, entity_id)  # 404 when not in this workspace
    relations = [RelationOut.of(r) for r in await catalog.relations_of(access.session, entity_id)]
    return Page(data=relations, page=PageInfo(limit=len(relations), next_cursor=None))


@router.post("/relations", status_code=CREATED)
async def create_relation(body: RelationIn, access: WorkspaceAccess) -> CreatedOut:
    relation_id = await catalog.create_relation(
        access.session,
        access.actor,
        workspace_id=_workspace(access),
        relation_type=body.relation_type,
        from_entity_id=body.from_entity_id,
        to_entity_id=body.to_entity_id,
        attributes=body.attributes,
    )
    return CreatedOut(id=relation_id)


@router.patch("/relations/{relation_id}", status_code=NO_CONTENT)
async def set_relation_status(
    relation_id: uuid.UUID, body: StatusIn, access: WorkspaceAccess
) -> None:
    await catalog.set_detail_status(
        access.session,
        access.actor,
        kind="relation",
        detail_id=relation_id,
        status=body.publication_status,
    )


@router.delete("/relations/{relation_id}", status_code=NO_CONTENT)
async def delete_relation(relation_id: uuid.UUID, access: WorkspaceAccess) -> None:
    await catalog.delete_relation(access.session, access.actor, relation_id=relation_id)


@router.get("/entities/{entity_id}/availability")
async def read_availability(entity_id: uuid.UUID, access: WorkspaceAccess) -> AvailabilityOut:
    iam.require(access.actor, "catalog:read")
    await catalog.get_entity(access.session, entity_id)  # 404 when not in this workspace
    rules, exceptions = await catalog.availability_of(access.session, entity_id)
    return AvailabilityOut(
        rules=[RuleOut(**r.__dict__) for r in rules],
        exceptions=[ExceptionOut(**e.__dict__) for e in exceptions],
    )


@router.post("/availability-rules", status_code=CREATED)
async def create_rule(body: RuleIn, access: WorkspaceAccess) -> CreatedOut:
    rule_id = await catalog.add_availability_rule(
        access.session, access.actor, workspace_id=_workspace(access), draft=body.draft()
    )
    return CreatedOut(id=rule_id)


@router.patch("/availability-rules/{rule_id}", status_code=NO_CONTENT)
async def set_rule_status(rule_id: uuid.UUID, body: StatusIn, access: WorkspaceAccess) -> None:
    await catalog.set_detail_status(
        access.session,
        access.actor,
        kind="availability_rule",
        detail_id=rule_id,
        status=body.publication_status,
    )


@router.post("/availability-exceptions", status_code=CREATED)
async def create_exception(body: ExceptionIn, access: WorkspaceAccess) -> CreatedOut:
    exception_id = await catalog.add_availability_exception(
        access.session, access.actor, workspace_id=_workspace(access), draft=body.draft()
    )
    return CreatedOut(id=exception_id)


@router.patch("/availability-exceptions/{exception_id}", status_code=NO_CONTENT)
async def set_exception_status(
    exception_id: uuid.UUID, body: StatusIn, access: WorkspaceAccess
) -> None:
    await catalog.set_detail_status(
        access.session,
        access.actor,
        kind="availability_exception",
        detail_id=exception_id,
        status=body.publication_status,
    )

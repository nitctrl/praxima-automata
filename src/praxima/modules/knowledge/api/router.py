"""Knowledge endpoints: documents, review, FAQs, announcements, search. No logic or queries."""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from praxima.entrypoints.http.deps import Index, Paging, WorkspaceAccess
from praxima.entrypoints.http.responses import Page, PageInfo
from praxima.modules import iam, knowledge
from praxima.modules.knowledge.api.schemas import (
    AnnouncementIn,
    AnnouncementOut,
    AnnouncementPatch,
    DocumentDetailOut,
    DocumentOut,
    FaqIn,
    FaqOut,
    FaqPatch,
    SearchHitOut,
    SectionsIn,
    Status,
    VersionDetailOut,
    VersionOut,
    VersionStatusIn,
)

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["knowledge"])
CREATED = status.HTTP_201_CREATED
NO_CONTENT = status.HTTP_204_NO_CONTENT
Filename = Annotated[str, Query(min_length=1, max_length=200)]
Category = Annotated[str | None, Query(min_length=2, max_length=63)]


def _workspace(access: WorkspaceAccess) -> uuid.UUID:
    assert access.workspace_id is not None  # WorkspaceAccess always scopes a workspace
    return access.workspace_id


# ---------------------------------------------------------------- documents


@router.get("/documents")
async def list_documents(
    access: WorkspaceAccess, page: Paging, status: Annotated[str | None, Query()] = None
) -> Page[DocumentOut]:
    iam.require(access.actor, "knowledge:read")
    result = await knowledge.documents_page(access.session, page, status=status)
    return Page.build(result, [DocumentOut.of(d) for d in result.items], page.limit)


async def _created_version(
    access: WorkspaceAccess, version_id: uuid.UUID, response: Response
) -> VersionDetailOut:
    version, sections = await knowledge.get_version(access.session, version_id)
    response.headers["Location"] = (
        f"/api/v1/workspaces/{access.workspace_id}/documents/{version.document_id}"
        f"/versions/{version.id}"
    )
    return VersionDetailOut.of(version, sections)


@router.post("/documents", status_code=CREATED)
async def upload_document(
    request: Request,
    response: Response,
    access: WorkspaceAccess,
    filename: Filename,
    category: Annotated[str, Query(min_length=2, max_length=63)],
) -> VersionDetailOut:
    """Upload a .docx or .md file as raw bytes (application/octet-stream, max 5 MB)."""
    version_id = await knowledge.upload_document(
        access.session,
        access.actor,
        workspace_id=_workspace(access),
        filename=filename,
        data=await request.body(),
        category=category,
    )
    return await _created_version(access, version_id, response)


@router.post("/documents/{document_id}/versions", status_code=CREATED)
async def upload_version(
    document_id: uuid.UUID,
    request: Request,
    response: Response,
    access: WorkspaceAccess,
    filename: Filename,
    category: Category = None,
) -> VersionDetailOut:
    """Upload a new version of a document (raw bytes); it goes to review first."""
    version_id = await knowledge.upload_document(
        access.session,
        access.actor,
        workspace_id=_workspace(access),
        filename=filename,
        data=await request.body(),
        category=category,
        replaces=document_id,
    )
    return await _created_version(access, version_id, response)


@router.get("/documents/{document_id}")
async def read_document(document_id: uuid.UUID, access: WorkspaceAccess) -> DocumentDetailOut:
    iam.require(access.actor, "knowledge:read")
    document, versions = await knowledge.get_document(access.session, document_id)
    return DocumentDetailOut(
        document=DocumentOut.of(document), versions=[VersionOut.of(v) for v in versions]
    )


@router.delete("/documents/{document_id}", status_code=NO_CONTENT)
async def archive_document(
    document_id: uuid.UUID,
    access: WorkspaceAccess,
    index: Index,
    row_version: Annotated[int, Query(ge=1)],
) -> None:
    await knowledge.archive_document(
        access.session, access.actor, document_id=document_id, row_version=row_version, index=index
    )


@router.get("/documents/{document_id}/versions/{version_id}")
async def read_version(
    document_id: uuid.UUID, version_id: uuid.UUID, access: WorkspaceAccess
) -> VersionDetailOut:
    iam.require(access.actor, "knowledge:read")
    version, sections = await knowledge.get_version(access.session, version_id, document_id)
    return VersionDetailOut.of(version, sections)


@router.put("/documents/{document_id}/versions/{version_id}/sections")
async def replace_sections(
    document_id: uuid.UUID, version_id: uuid.UUID, body: SectionsIn, access: WorkspaceAccess
) -> VersionDetailOut:
    """Save the reviewed wording (replaces all sections; only while under review)."""
    await knowledge.replace_sections(
        access.session,
        access.actor,
        version_id=version_id,
        row_version=body.row_version,
        sections=body.drafts(),
        document_id=document_id,
    )
    version, sections = await knowledge.get_version(access.session, version_id)
    return VersionDetailOut.of(version, sections)


@router.patch("/documents/{document_id}/versions/{version_id}")
async def set_version_status(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    body: VersionStatusIn,
    access: WorkspaceAccess,
    index: Index,
) -> VersionOut:
    """Publish (the previous live version is superseded), reject, or archive."""
    await knowledge.set_version_status(
        access.session,
        access.actor,
        version_id=version_id,
        row_version=body.row_version,
        status=body.status,
        index=index,
        document_id=document_id,
    )
    version, _ = await knowledge.get_version(access.session, version_id)
    return VersionOut.of(version)


# ---------------------------------------------------------------- FAQs


@router.get("/faqs")
async def list_faqs(
    access: WorkspaceAccess, page: Paging, status: Annotated[Status | None, Query()] = None
) -> Page[FaqOut]:
    iam.require(access.actor, "knowledge:read")
    result = await knowledge.faqs_page(access.session, page, status=status)
    return Page.build(result, [FaqOut.of(f) for f in result.items], page.limit)


@router.post("/faqs", status_code=CREATED)
async def create_faq(body: FaqIn, access: WorkspaceAccess, response: Response) -> FaqOut:
    faq_id = await knowledge.create_faq(
        access.session, access.actor, workspace_id=_workspace(access), draft=body.draft()
    )
    response.headers["Location"] = f"/api/v1/workspaces/{access.workspace_id}/faqs/{faq_id}"
    return FaqOut.of(await knowledge.get_faq(access.session, faq_id))


@router.get("/faqs/{faq_id}")
async def read_faq(faq_id: uuid.UUID, access: WorkspaceAccess) -> FaqOut:
    iam.require(access.actor, "knowledge:read")
    return FaqOut.of(await knowledge.get_faq(access.session, faq_id))


@router.patch("/faqs/{faq_id}")
async def update_faq(faq_id: uuid.UUID, body: FaqPatch, access: WorkspaceAccess) -> FaqOut:
    await knowledge.update_faq(
        access.session,
        access.actor,
        faq_id=faq_id,
        row_version=body.row_version,
        changes=body.changes(),
        status=body.publication_status,
    )
    return FaqOut.of(await knowledge.get_faq(access.session, faq_id))


@router.delete("/faqs/{faq_id}", status_code=NO_CONTENT)
async def delete_faq(
    faq_id: uuid.UUID, access: WorkspaceAccess, row_version: Annotated[int, Query(ge=1)]
) -> None:
    await knowledge.delete_faq(access.session, access.actor, faq_id=faq_id, row_version=row_version)


# ---------------------------------------------------------------- announcements


@router.get("/announcements")
async def list_announcements(
    access: WorkspaceAccess,
    page: Paging,
    status: Annotated[Status | None, Query()] = None,
    active: Annotated[bool, Query(description="Only those in effect right now")] = False,
) -> Page[AnnouncementOut]:
    iam.require(access.actor, "knowledge:read")
    result = await knowledge.announcements_page(
        access.session,
        page,
        status=status,
        active_at=datetime.now(timezone.utc) if active else None,
    )
    return Page.build(result, [AnnouncementOut.of(a) for a in result.items], page.limit)


@router.post("/announcements", status_code=CREATED)
async def create_announcement(
    body: AnnouncementIn, access: WorkspaceAccess, response: Response
) -> AnnouncementOut:
    announcement_id = await knowledge.create_announcement(
        access.session, access.actor, workspace_id=_workspace(access), draft=body.draft()
    )
    response.headers["Location"] = (
        f"/api/v1/workspaces/{access.workspace_id}/announcements/{announcement_id}"
    )
    return AnnouncementOut.of(await knowledge.get_announcement(access.session, announcement_id))


@router.get("/announcements/{announcement_id}")
async def read_announcement(announcement_id: uuid.UUID, access: WorkspaceAccess) -> AnnouncementOut:
    iam.require(access.actor, "knowledge:read")
    return AnnouncementOut.of(await knowledge.get_announcement(access.session, announcement_id))


@router.patch("/announcements/{announcement_id}")
async def update_announcement(
    announcement_id: uuid.UUID, body: AnnouncementPatch, access: WorkspaceAccess
) -> AnnouncementOut:
    await knowledge.update_announcement(
        access.session,
        access.actor,
        announcement_id=announcement_id,
        row_version=body.row_version,
        changes=body.changes(),
        status=body.publication_status,
    )
    return AnnouncementOut.of(await knowledge.get_announcement(access.session, announcement_id))


@router.delete("/announcements/{announcement_id}", status_code=NO_CONTENT)
async def delete_announcement(
    announcement_id: uuid.UUID,
    access: WorkspaceAccess,
    row_version: Annotated[int, Query(ge=1)],
) -> None:
    await knowledge.delete_announcement(
        access.session, access.actor, announcement_id=announcement_id, row_version=row_version
    )


# ---------------------------------------------------------------- search


@router.get("/knowledge/search")
async def search(
    access: WorkspaceAccess,
    index: Index,
    q: Annotated[str, Query(min_length=2, max_length=300)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> Page[SearchHitOut]:
    """Search published documents: keywords, plus meaning when Qdrant is configured."""
    iam.require(access.actor, "knowledge:read")
    hits = await knowledge.search_knowledge(
        access.session, _workspace(access), q, index=index, limit=limit
    )
    return Page(
        data=[SearchHitOut.of(h) for h in hits], page=PageInfo(limit=limit, next_cursor=None)
    )

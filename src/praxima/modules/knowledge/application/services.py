"""Writes for knowledge: document upload/review/publish, FAQs and announcements."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules import audit, tenancy
from praxima.modules.iam import Actor, require
from praxima.modules.knowledge.application.ports import IndexedChunk, KnowledgeIndex
from praxima.modules.knowledge.domain.chunking import chunk_sections

# Read-only reuse of the existing, hardened .docx/.md extractor (no changes to it).
from praxima.modules.knowledge.domain.documents import DocumentRejected, extract
from praxima.modules.knowledge.infrastructure.models import (
    Announcement,
    Chunk,
    Document,
    DocumentSection,
    DocumentVersion,
    Faq,
)
from praxima.shared.db.base import utc_now
from praxima.shared.db.errors import translate_db_errors
from praxima.shared.errors import Conflict, FieldError, NotFound, ValidationFailed

logger = logging.getLogger(__name__)
PUBLICATION_STATES = frozenset({"draft", "published", "archived"})
MAX_SECTIONS = 200
MAX_DOCUMENT_CHARS = 100_000


@dataclass(frozen=True)
class SectionDraft:
    heading: str | None
    text: str
    entity_id: uuid.UUID | None = None
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class FaqDraft:
    canonical_question: str
    approved_answer: str
    category: str | None = None
    alternative_phrasings: tuple[str, ...] = ()
    entity_id: uuid.UUID | None = None


@dataclass(frozen=True)
class FaqChanges:
    canonical_question: str | None = None
    approved_answer: str | None = None
    category: str | None = None
    alternative_phrasings: tuple[str, ...] | None = None


@dataclass(frozen=True)
class AnnouncementDraft:
    kind: str
    public_message: str
    starts_at: datetime
    ends_at: datetime
    internal_note: str | None = None
    entity_id: uuid.UUID | None = None
    location_entity_id: uuid.UUID | None = None
    priority: int = 100


@dataclass(frozen=True)
class AnnouncementChanges:
    public_message: str | None = None
    internal_note: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    priority: int | None = None


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


# ---------------------------------------------------------------- documents


async def upload_document(
    session: AsyncSession,
    actor: Actor,
    *,
    workspace_id: uuid.UUID,
    filename: str,
    data: bytes,
    category: str,
    replaces: uuid.UUID | None = None,
) -> uuid.UUID:
    """Extract an upload into a new version awaiting review. Returns the version id."""
    require(actor, "knowledge:write")
    pack = await tenancy.installed_pack(session, workspace_id)
    if category not in pack.document_categories:
        raise ValidationFailed(errors=[FieldError("category", "Unknown document category.")])
    try:
        extraction = extract(filename, data)
    except DocumentRejected as exc:
        raise ValidationFailed(str(exc)) from None  # messages are fixed, value-free text
    if replaces is None:
        document = Document(
            workspace_id=workspace_id,
            title=extraction.title,
            category=category,
            created_by=actor.user_id,
        )
        session.add(document)
        version_no = 1
    else:
        found = await session.get(Document, replaces)
        if found is None or found.deleted_at is not None:
            raise NotFound("Document not found.")
        document = found
        latest = await session.scalar(
            select(func.max(DocumentVersion.version_no)).where(
                DocumentVersion.document_id == document.id
            )
        )
        version_no = int(latest or 0) + 1
    with translate_db_errors(duplicate="This document was already uploaded."):
        await session.flush()
        version = DocumentVersion(
            workspace_id=workspace_id,
            document_id=document.id,
            version_no=version_no,
            title=extraction.title,
            original_filename=filename[:200],
            mime_type=extraction.mime_type,
            checksum=extraction.checksum,
            extraction_warnings=list(extraction.warnings),
            created_by=actor.user_id,
        )
        session.add(version)
        await session.flush()
        session.add_all(
            DocumentSection(
                workspace_id=workspace_id,
                document_version_id=version.id,
                position=position,
                heading=section.heading or None,
                text=section.text,
                keywords=list(section.keywords),
            )
            for position, section in enumerate(extraction.sections)
        )
        await session.flush()
    await _audit(session, actor, workspace_id, "document.upload", "document_version", version.id)
    return version.id


async def _version(session: AsyncSession, version_id: uuid.UUID) -> DocumentVersion:
    version = await session.get(DocumentVersion, version_id)
    if version is None or version.deleted_at is not None:
        raise NotFound("Document version not found.")
    return version


def _check_sections(sections: list[SectionDraft]) -> None:
    if not 1 <= len(sections) <= MAX_SECTIONS:
        raise ValidationFailed(f"Keep between 1 and {MAX_SECTIONS} sections.")
    errors = []
    for index, section in enumerate(sections):
        if not 1 <= len(section.text.strip()) <= 4000:
            errors.append(FieldError(f"sections.{index}.text", "Must be 1 to 4000 characters."))
        if section.heading is not None and len(section.heading) > 200:
            errors.append(FieldError(f"sections.{index}.heading", "Is too long."))
    if errors:
        raise ValidationFailed(errors=errors)
    if sum(len(s.text) for s in sections) > MAX_DOCUMENT_CHARS:
        raise ValidationFailed("Keep the reviewed text under 100,000 characters.")


async def replace_sections(
    session: AsyncSession,
    actor: Actor,
    *,
    version_id: uuid.UUID,
    row_version: int,
    sections: list[SectionDraft],
) -> None:
    """Save the reviewed wording of a version still under review (replaces all sections)."""
    require(actor, "knowledge:write")
    _check_sections(sections)
    version = await _version(session, version_id)
    if version.row_version != row_version:
        raise Conflict("This version was changed by someone else. Reload and try again.")
    if version.status != "needs_review":
        raise Conflict("Only versions under review can be edited. Upload a new version.")
    await session.execute(
        delete(DocumentSection).where(DocumentSection.document_version_id == version.id)
    )
    session.add_all(
        DocumentSection(
            workspace_id=version.workspace_id,
            document_version_id=version.id,
            position=position,
            heading=section.heading or None,
            text=section.text.strip(),
            entity_id=section.entity_id,
            keywords=list(section.keywords),
            updated_by=actor.user_id,
        )
        for position, section in enumerate(sections)
    )
    version.updated_by = actor.user_id  # bumps row_version
    with translate_db_errors(reference="A linked item doesn't exist."):
        await session.flush()
    await _audit(
        session, actor, version.workspace_id, "document.review", "document_version", version.id
    )


async def _rebuild_chunks(session: AsyncSession, version: DocumentVersion) -> list[Chunk]:
    await session.execute(delete(Chunk).where(Chunk.document_version_id == version.id))
    sections = list(
        await session.scalars(
            select(DocumentSection)
            .where(DocumentSection.document_version_id == version.id)
            .order_by(DocumentSection.position)
        )
    )
    by_position = {s.position: s for s in sections}
    drafts = chunk_sections([(s.position, s.heading, s.text) for s in sections])
    chunks = [
        Chunk(
            workspace_id=version.workspace_id,
            document_version_id=version.id,
            section_id=by_position[draft.section_position].id,
            chunk_index=index,
            heading=draft.heading,
            text=draft.text,
            entity_id=by_position[draft.section_position].entity_id,
        )
        for index, draft in enumerate(drafts)
    ]
    session.add_all(chunks)
    await session.flush()
    return chunks


async def _index(index: KnowledgeIndex | None, chunks: list[Chunk]) -> None:
    """Best effort: a Qdrant outage never blocks publishing (keyword search still works)."""
    if index is None or not chunks:
        return
    try:
        await index.upsert(
            [
                IndexedChunk(
                    c.id,
                    c.workspace_id,
                    c.document_version_id,
                    f"{c.heading}\n{c.text}" if c.heading else c.text,
                )
                for c in chunks
            ]
        )
    except Exception as exc:
        logger.warning("Knowledge indexing failed (%s); keyword search only", type(exc).__name__)
        return
    for chunk in chunks:
        chunk.embedding_model = index.model


async def _unindex(index: KnowledgeIndex | None, version: DocumentVersion) -> None:
    if index is None:
        return
    try:
        await index.remove_version(version.workspace_id, version.id)
    except Exception as exc:
        logger.warning("Knowledge unindexing failed (%s)", type(exc).__name__)


async def set_version_status(
    session: AsyncSession,
    actor: Actor,
    *,
    version_id: uuid.UUID,
    row_version: int,
    status: str,
    index: KnowledgeIndex | None = None,
) -> None:
    """Publish (replacing the live version), reject, or archive a document version."""
    require(actor, "knowledge:publish")
    version = await _version(session, version_id)
    if version.row_version != row_version:
        raise Conflict("This version was changed by someone else. Reload and try again.")
    document = await session.get(Document, version.document_id)
    assert document is not None  # composite FK guarantees it
    now = utc_now()
    if status == "published":
        if version.status not in ("needs_review", "archived", "superseded"):
            raise Conflict("This version can't be published from its current state.")
        has_sections = await session.scalar(
            select(func.count())
            .select_from(DocumentSection)
            .where(DocumentSection.document_version_id == version.id)
        )
        if not has_sections:
            raise ValidationFailed("Review the extracted text before publishing.")
        previous_id = document.published_version_id
        if previous_id is not None and previous_id != version.id:
            previous = await _version(session, previous_id)
            previous.status = "superseded"
            await _unindex(index, previous)
        version.status, version.published_by, version.published_at = status, actor.user_id, now
        version.reviewed_by, version.reviewed_at = actor.user_id, now
        document.published_version_id, document.status = version.id, "active"
        with translate_db_errors():
            await session.flush()
            chunks = await _rebuild_chunks(session, version)
        await _index(index, chunks)
    elif status == "rejected":
        if version.status != "needs_review":
            raise Conflict("Only versions under review can be rejected.")
        version.status, version.reviewed_by, version.reviewed_at = status, actor.user_id, now
    elif status == "archived":
        if document.published_version_id == version.id:
            document.published_version_id = None
            await _unindex(index, version)
        version.status = status
    else:
        raise ValidationFailed(errors=[FieldError("status", "Unknown status.")])
    version.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(
        session,
        actor,
        version.workspace_id,
        f"document.{status}",
        "document_version",
        version.id,
    )


async def archive_document(
    session: AsyncSession,
    actor: Actor,
    *,
    document_id: uuid.UUID,
    row_version: int,
    index: KnowledgeIndex | None = None,
) -> None:
    """Take a document off the air (soft delete); its versions stay for history."""
    require(actor, "knowledge:publish")
    document = await session.get(Document, document_id)
    if document is None or document.deleted_at is not None:
        raise NotFound("Document not found.")
    if document.row_version != row_version:
        raise Conflict("This document was changed by someone else. Reload and try again.")
    if document.published_version_id is not None:
        live = await _version(session, document.published_version_id)
        live.status = "archived"
        await _unindex(index, live)
    document.published_version_id, document.status = None, "archived"
    document.deleted_at, document.updated_by = utc_now(), actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, document.workspace_id, "document.archive", "document", document.id)


# ---------------------------------------------------------------- FAQs


async def create_faq(
    session: AsyncSession, actor: Actor, *, workspace_id: uuid.UUID, draft: FaqDraft
) -> uuid.UUID:
    require(actor, "knowledge:write")
    faq = Faq(
        workspace_id=workspace_id,
        canonical_question=draft.canonical_question,
        approved_answer=draft.approved_answer,
        category=draft.category,
        alternative_phrasings=list(draft.alternative_phrasings),
        entity_id=draft.entity_id,
        created_by=actor.user_id,
    )
    with translate_db_errors(reference="The linked item doesn't exist."):
        session.add(faq)
        await session.flush()
    await _audit(session, actor, workspace_id, "faq.create", "faq", faq.id)
    return faq.id


async def _live_faq(session: AsyncSession, faq_id: uuid.UUID, row_version: int) -> Faq:
    faq = await session.get(Faq, faq_id)
    if faq is None or faq.deleted_at is not None:
        raise NotFound("FAQ not found.")
    if faq.row_version != row_version:
        raise Conflict("This FAQ was changed by someone else. Reload and try again.")
    return faq


async def update_faq(
    session: AsyncSession,
    actor: Actor,
    *,
    faq_id: uuid.UUID,
    row_version: int,
    changes: FaqChanges,
    status: str | None = None,
) -> None:
    """Change the wording and/or publication status under one row_version check."""
    content = {k: v for k, v in changes.__dict__.items() if v is not None}
    if content:
        require(actor, "knowledge:write")
    if status is not None:
        require(actor, "knowledge:publish")
        if status not in PUBLICATION_STATES:
            raise ValidationFailed(errors=[FieldError("publication_status", "Unknown status.")])
    if not content and status is None:
        raise ValidationFailed("Nothing to change.")
    faq = await _live_faq(session, faq_id, row_version)
    for name, value in content.items():
        setattr(faq, name, list(value) if name == "alternative_phrasings" else value)
    if status is not None:
        faq.publication_status = status
    faq.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    fields = sorted([*content, *(["publication_status"] if status else [])])
    await _audit(session, actor, faq.workspace_id, "faq.update", "faq", faq.id, {"fields": fields})


async def delete_faq(
    session: AsyncSession, actor: Actor, *, faq_id: uuid.UUID, row_version: int
) -> None:
    require(actor, "knowledge:write")
    faq = await _live_faq(session, faq_id, row_version)
    faq.deleted_at, faq.publication_status, faq.updated_by = utc_now(), "archived", actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(session, actor, faq.workspace_id, "faq.delete", "faq", faq.id)


# ---------------------------------------------------------------- announcements


def _window(starts_at: datetime, ends_at: datetime) -> Range[datetime]:
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise ValidationFailed("Give start and end times with a time zone.")
    if ends_at <= starts_at:
        raise ValidationFailed(errors=[FieldError("ends_at", "Must be after the start.")])
    return Range(starts_at, ends_at, bounds="[)")


async def create_announcement(
    session: AsyncSession, actor: Actor, *, workspace_id: uuid.UUID, draft: AnnouncementDraft
) -> uuid.UUID:
    """A live update (closure, doctor unavailable ...) of a kind the pack declares."""
    require(actor, "knowledge:write")
    pack = await tenancy.installed_pack(session, workspace_id)
    if draft.kind not in pack.announcement_kinds:
        raise ValidationFailed(errors=[FieldError("kind", "Unknown announcement kind.")])
    announcement = Announcement(
        workspace_id=workspace_id,
        kind=draft.kind,
        public_message=draft.public_message,
        internal_note=draft.internal_note,
        entity_id=draft.entity_id,
        location_entity_id=draft.location_entity_id,
        priority=draft.priority,
        valid_during=_window(draft.starts_at, draft.ends_at),
        created_by=actor.user_id,
    )
    with translate_db_errors(reference="The linked item doesn't exist."):
        session.add(announcement)
        await session.flush()
    await _audit(
        session, actor, workspace_id, "announcement.create", "announcement", announcement.id
    )
    return announcement.id


async def _live_announcement(
    session: AsyncSession, announcement_id: uuid.UUID, row_version: int
) -> Announcement:
    announcement = await session.get(Announcement, announcement_id)
    if announcement is None or announcement.deleted_at is not None:
        raise NotFound("Announcement not found.")
    if announcement.row_version != row_version:
        raise Conflict("This announcement was changed by someone else. Reload and try again.")
    return announcement


async def update_announcement(
    session: AsyncSession,
    actor: Actor,
    *,
    announcement_id: uuid.UUID,
    row_version: int,
    changes: AnnouncementChanges,
    status: str | None = None,
) -> None:
    content = {k: v for k, v in changes.__dict__.items() if v is not None}
    if content:
        require(actor, "knowledge:write")
    if status is not None:
        require(actor, "knowledge:publish")
        if status not in PUBLICATION_STATES:
            raise ValidationFailed(errors=[FieldError("publication_status", "Unknown status.")])
    if not content and status is None:
        raise ValidationFailed("Nothing to change.")
    announcement = await _live_announcement(session, announcement_id, row_version)
    if content.keys() & {"starts_at", "ends_at"}:
        current = announcement.valid_during
        starts = content.get("starts_at") or current.lower
        ends = content.get("ends_at") or current.upper
        assert starts is not None and ends is not None  # CHECK: the window is never empty
        announcement.valid_during = _window(starts, ends)
    for name in ("public_message", "internal_note", "priority"):
        if name in content:
            setattr(announcement, name, content[name])
    if status is not None:
        announcement.publication_status = status
    announcement.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    fields = sorted([*content, *(["publication_status"] if status else [])])
    await _audit(
        session,
        actor,
        announcement.workspace_id,
        "announcement.update",
        "announcement",
        announcement.id,
        {"fields": fields},
    )


async def delete_announcement(
    session: AsyncSession, actor: Actor, *, announcement_id: uuid.UUID, row_version: int
) -> None:
    require(actor, "knowledge:write")
    announcement = await _live_announcement(session, announcement_id, row_version)
    announcement.deleted_at, announcement.publication_status = utc_now(), "archived"
    announcement.updated_by = actor.user_id
    with translate_db_errors():
        await session.flush()
    await _audit(
        session,
        actor,
        announcement.workspace_id,
        "announcement.delete",
        "announcement",
        announcement.id,
    )

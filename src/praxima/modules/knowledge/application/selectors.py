"""Reads for knowledge, and hybrid search. RLS limits every query to the scoped workspace."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, literal_column, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from praxima.modules.knowledge.application.ports import KnowledgeIndex
from praxima.modules.knowledge.domain.chunking import reciprocal_rank_fusion
from praxima.modules.knowledge.domain.search import search_terms, tsquery_text
from praxima.modules.knowledge.infrastructure.models import (
    Announcement,
    Chunk,
    Document,
    DocumentSection,
    DocumentVersion,
    Faq,
)
from praxima.shared.db.pagination import PageRequest, PageResult, fetch_page
from praxima.shared.errors import NotFound

logger = logging.getLogger(__name__)
CANDIDATES = 20
# Must match ix_chunks_full_text exactly (as literal SQL) so Postgres can use the index.
_TSVECTOR: ColumnElement[Any] = literal_column(
    "to_tsvector('simple'::regconfig, "
    "((COALESCE(knowledge.chunks.heading, ''::text) || ' '::text) || knowledge.chunks.text))"
)


@dataclass(frozen=True)
class DocumentView:
    id: uuid.UUID
    title: str
    category: str | None
    status: str
    published_version_id: uuid.UUID | None
    published_version_no: int | None
    row_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class VersionView:
    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    status: str
    title: str
    original_filename: str
    extraction_warnings: list[str]
    row_version: int
    created_at: datetime
    published_at: datetime | None


@dataclass(frozen=True)
class SectionView:
    id: uuid.UUID
    position: int
    heading: str | None
    text: str
    entity_id: uuid.UUID | None
    keywords: list[str]


@dataclass(frozen=True)
class FaqView:
    id: uuid.UUID
    category: str | None
    canonical_question: str
    alternative_phrasings: list[str]
    approved_answer: str
    entity_id: uuid.UUID | None
    publication_status: str
    row_version: int
    created_at: datetime


@dataclass(frozen=True)
class AnnouncementView:
    id: uuid.UUID
    kind: str
    public_message: str
    internal_note: str | None
    entity_id: uuid.UUID | None
    location_entity_id: uuid.UUID | None
    priority: int
    starts_at: datetime
    ends_at: datetime
    publication_status: str
    row_version: int
    created_at: datetime


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    heading: str | None
    text: str
    entity_id: uuid.UUID | None


def _version_view(v: DocumentVersion) -> VersionView:
    return VersionView(
        v.id,
        v.document_id,
        v.version_no,
        v.status,
        v.title,
        v.original_filename,
        list(v.extraction_warnings),
        v.row_version,
        v.created_at,
        v.published_at,
    )


async def documents_page(
    session: AsyncSession, page: PageRequest, *, status: str | None = None
) -> PageResult:
    """Newest first, with the live version number joined in (one query, no N+1)."""
    conditions: list[ColumnElement[bool]] = [Document.deleted_at.is_(None)]
    if status is not None:
        conditions.append(Document.status == status)
    statement = (
        select(Document, DocumentVersion.version_no.label("live_no"))
        .outerjoin(DocumentVersion, DocumentVersion.id == Document.published_version_id)
        .where(and_(*conditions))
    )
    result = await fetch_page(session, statement, (Document.created_at, Document.id), page)
    return PageResult(
        [
            DocumentView(
                row.Document.id,
                row.Document.title,
                row.Document.category,
                row.Document.status,
                row.Document.published_version_id,
                row.live_no,
                row.Document.row_version,
                row.Document.created_at,
                row.Document.updated_at,
            )
            for row in result.items
        ],
        result.next_cursor,
    )


async def get_document(
    session: AsyncSession, document_id: uuid.UUID
) -> tuple[DocumentView, list[VersionView]]:
    """A document and all its versions, newest first (two queries)."""
    row = (
        await session.execute(
            select(Document, DocumentVersion.version_no.label("live_no"))
            .outerjoin(DocumentVersion, DocumentVersion.id == Document.published_version_id)
            .where(Document.id == document_id, Document.deleted_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Document not found.")
    d = row.Document
    versions = await session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id, DocumentVersion.deleted_at.is_(None))
        .order_by(DocumentVersion.version_no.desc())
    )
    view = DocumentView(
        d.id,
        d.title,
        d.category,
        d.status,
        d.published_version_id,
        row.live_no,
        d.row_version,
        d.created_at,
        d.updated_at,
    )
    return view, [_version_view(v) for v in versions]


async def get_version(
    session: AsyncSession, version_id: uuid.UUID, document_id: uuid.UUID | None = None
) -> tuple[VersionView, list[SectionView]]:
    """A version with its reviewed sections, in order (two queries).

    With `document_id`, the version must belong to that document (else 404).
    """
    version = await session.get(DocumentVersion, version_id)
    wrong_document = (
        document_id is not None and version is not None and (version.document_id != document_id)
    )
    if version is None or version.deleted_at is not None or wrong_document:
        raise NotFound("Document version not found.")
    sections = await session.scalars(
        select(DocumentSection)
        .where(DocumentSection.document_version_id == version_id)
        .order_by(DocumentSection.position)
    )
    return _version_view(version), [
        SectionView(s.id, s.position, s.heading, s.text, s.entity_id, list(s.keywords))
        for s in sections
    ]


def _faq_view(f: Faq) -> FaqView:
    return FaqView(
        f.id,
        f.category,
        f.canonical_question,
        list(f.alternative_phrasings),
        f.approved_answer,
        f.entity_id,
        f.publication_status,
        f.row_version,
        f.created_at,
    )


async def faqs_page(
    session: AsyncSession, page: PageRequest, *, status: str | None = None
) -> PageResult:
    conditions: list[ColumnElement[bool]] = [Faq.deleted_at.is_(None)]
    if status is not None:
        conditions.append(Faq.publication_status == status)
    result = await fetch_page(
        session, select(Faq).where(and_(*conditions)), (Faq.created_at, Faq.id), page
    )
    return PageResult([_faq_view(f) for f in result.items], result.next_cursor)


async def get_faq(session: AsyncSession, faq_id: uuid.UUID) -> FaqView:
    faq = await session.get(Faq, faq_id)
    if faq is None or faq.deleted_at is not None:
        raise NotFound("FAQ not found.")
    return _faq_view(faq)


def _announcement_view(a: Announcement) -> AnnouncementView:
    return AnnouncementView(
        a.id,
        a.kind,
        a.public_message,
        a.internal_note,
        a.entity_id,
        a.location_entity_id,
        a.priority,
        a.valid_during.lower,  # type: ignore[arg-type]  # CHECK: never empty
        a.valid_during.upper,  # type: ignore[arg-type]
        a.publication_status,
        a.row_version,
        a.created_at,
    )


async def announcements_page(
    session: AsyncSession,
    page: PageRequest,
    *,
    status: str | None = None,
    active_at: datetime | None = None,
) -> PageResult:
    """Newest first; `active_at` keeps only updates whose window contains that instant."""
    conditions: list[ColumnElement[bool]] = [Announcement.deleted_at.is_(None)]
    if status is not None:
        conditions.append(Announcement.publication_status == status)
    if active_at is not None:
        conditions.append(Announcement.valid_during.contains(active_at))
    result = await fetch_page(
        session,
        select(Announcement).where(and_(*conditions)),
        (Announcement.created_at, Announcement.id),
        page,
    )
    return PageResult([_announcement_view(a) for a in result.items], result.next_cursor)


async def get_announcement(session: AsyncSession, announcement_id: uuid.UUID) -> AnnouncementView:
    announcement = await session.get(Announcement, announcement_id)
    if announcement is None or announcement.deleted_at is not None:
        raise NotFound("Announcement not found.")
    return _announcement_view(announcement)


def _live_chunks() -> ColumnElement[bool]:
    """Chunks of the published version of an active, non-deleted document."""
    return and_(
        Document.published_version_id == Chunk.document_version_id,
        Document.status == "active",
        Document.deleted_at.is_(None),
    )


async def search_knowledge(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    query: str,
    *,
    index: KnowledgeIndex | None = None,
    limit: int = 5,
) -> list[SearchHit]:
    """Keyword ranking (Postgres) fused with semantic ranking (Qdrant, when available).

    Qdrant results are only candidates: they are re-checked against live, RLS-visible
    chunks here, so the vector store can never widen what a workspace sees.
    """
    rankings: list[list[uuid.UUID]] = []
    if terms := search_terms(query):  # all filler ("when do you ...") → semantic only
        tsquery = func.to_tsquery(sql_text("'simple'::regconfig"), tsquery_text(terms))
        rankings.append(
            list(
                await session.scalars(
                    select(Chunk.id)
                    .join(Document, _live_chunks())
                    .where(_TSVECTOR.op("@@")(tsquery))
                    .order_by(func.ts_rank(_TSVECTOR, tsquery).desc(), Chunk.id)
                    .limit(CANDIDATES)
                )
            )
        )
    if index is not None:
        try:
            rankings.append(await index.search(workspace_id, query, CANDIDATES))
        except Exception as exc:
            logger.warning("Semantic search unavailable (%s); keywords only", type(exc).__name__)
    candidates = reciprocal_rank_fusion(rankings, limit=CANDIDATES)
    if not candidates:
        return []
    rows = await session.execute(
        select(Chunk, Document.id.label("document_id"), Document.title.label("title"))
        .join(Document, _live_chunks())
        .where(Chunk.id.in_(candidates))
    )
    found = {row.Chunk.id: row for row in rows}
    hits = [found[c] for c in candidates if c in found][:limit]
    return [
        SearchHit(
            r.Chunk.id, r.document_id, r.title, r.Chunk.heading, r.Chunk.text, r.Chunk.entity_id
        )
        for r in hits
    ]


@dataclass(frozen=True)
class PublishedKnowledge:
    """Published knowledge as plain JSON-ready data in a stable order (for agent releases)."""

    sections: list[dict[str, Any]]
    faqs: list[dict[str, Any]]
    announcements: list[dict[str, Any]]


async def published_knowledge(session: AsyncSession, at: datetime) -> PublishedKnowledge:
    """Live document sections, approved answers valid at `at`, and live updates not yet over.

    Announcements keep their [start, end) window: scheduled ones are included so the runtime
    can apply them against the call's own clock.
    """
    sections = (
        await session.execute(
            select(DocumentSection, Document.title, Document.category)
            .join(Document, Document.published_version_id == DocumentSection.document_version_id)
            .where(Document.status == "active", Document.deleted_at.is_(None))
            .order_by(Document.title, Document.id, DocumentSection.position)
        )
    ).all()
    faqs = await session.scalars(
        select(Faq)
        .where(
            Faq.publication_status == "published",
            Faq.deleted_at.is_(None),
            or_(Faq.valid_during.is_(None), Faq.valid_during.op("@>")(at)),
        )
        .order_by(Faq.canonical_question, Faq.id)
    )
    announcements = await session.scalars(
        select(Announcement)
        .where(
            Announcement.publication_status == "published",
            Announcement.deleted_at.is_(None),
            func.upper(Announcement.valid_during) > at,
        )
        .order_by(func.lower(Announcement.valid_during), Announcement.id)
    )
    return PublishedKnowledge(
        sections=[
            {
                "id": str(row.DocumentSection.id),
                "document_title": row.title,
                "category": row.category,
                "heading": row.DocumentSection.heading,
                "text": row.DocumentSection.text,
                "entity_id": str(row.DocumentSection.entity_id)
                if row.DocumentSection.entity_id
                else None,
                "keywords": list(row.DocumentSection.keywords),
            }
            for row in sections
        ],
        faqs=[
            {
                "id": str(f.id),
                "question": f.canonical_question,
                "phrasings": list(f.alternative_phrasings),
                "answer": f.approved_answer,
                "category": f.category,
                "entity_id": str(f.entity_id) if f.entity_id else None,
            }
            for f in faqs
        ],
        announcements=[
            {
                "id": str(a.id),
                "kind": a.kind,
                "message": a.public_message,
                "entity_id": str(a.entity_id) if a.entity_id else None,
                "location_entity_id": str(a.location_entity_id) if a.location_entity_id else None,
                "priority": a.priority,
                "starts_at": a.valid_during.lower.isoformat() if a.valid_during.lower else None,
                "ends_at": a.valid_during.upper.isoformat() if a.valid_during.upper else None,
            }
            for a in announcements
        ],
    )

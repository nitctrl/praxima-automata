"""Reviewed knowledge: documents and versions, sections, search chunks, FAQs, announcements.

Vectors for chunks live in Qdrant (keyed by chunk id), not in Postgres.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
from sqlalchemy.orm import Mapped, mapped_column

from praxima.shared.db.base import (
    AuthoringMixin,
    Base,
    IdMixin,
    TenantMixin,
    TimestampMixin,
    utc_now,
)

SCHEMA = "knowledge"
PUBLICATION = "publication_status IN ('draft', 'published', 'archived')"
VERSION_STATES = (
    "processing",
    "needs_review",
    "published",
    "superseded",
    "rejected",
    "archived",
)
WORKSPACES = "tenancy.workspaces.id"


def _fk(columns: list[str], table: str, **options: Any) -> ForeignKeyConstraint:
    """Composite tenant key: a row can only reference a row of the same workspace."""
    return ForeignKeyConstraint(
        ["workspace_id", *columns], [f"{table}.workspace_id", f"{table}.id"], **options
    )


class Source(IdMixin, TenantMixin, AuthoringMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("source_type IN ('upload', 'url', 'connector')", name="source_type"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    source_type: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str | None] = mapped_column(Text)


class Document(IdMixin, TenantMixin, AuthoringMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _fk(["source_id"], f"{SCHEMA}.sources"),
        # Circular with document_versions: created after both tables exist.
        _fk(
            ["published_version_id"],
            f"{SCHEMA}.document_versions",
            use_alter=True,
            name="fk_documents_published_version",
        ),
        CheckConstraint("length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    source_id: Mapped[uuid.UUID | None]
    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default=sql_text("'active'"))
    published_version_id: Mapped[uuid.UUID | None]
    effective_during: Mapped[Range[datetime] | None] = mapped_column(TSTZRANGE)


class DocumentVersion(IdMixin, TenantMixin, AuthoringMixin, Base):
    """One upload of a document. `created_by` is the uploader. Review happens per version."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no"),
        # The same file can't be uploaded twice into a workspace.
        UniqueConstraint("workspace_id", "checksum"),
        UniqueConstraint("workspace_id", "id"),
        _fk(["document_id"], f"{SCHEMA}.documents"),
        CheckConstraint(f"status IN {VERSION_STATES}", name="status"),
        CheckConstraint("version_no >= 1", name="version_no"),
        CheckConstraint("checksum ~ '^[0-9a-f]{64}$'", name="checksum_format"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    document_id: Mapped[uuid.UUID]
    version_no: Mapped[int]
    status: Mapped[str] = mapped_column(
        Text, default="needs_review", server_default=sql_text("'needs_review'")
    )
    title: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(Text)
    storage_uri: Mapped[str | None] = mapped_column(Text)  # uploads are not retained by default
    extraction_warnings: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=sql_text("'{}'")
    )
    reviewed_by: Mapped[uuid.UUID | None]
    reviewed_at: Mapped[datetime | None]
    published_by: Mapped[uuid.UUID | None]
    published_at: Mapped[datetime | None]


class DocumentSection(IdMixin, TenantMixin, TimestampMixin, Base):
    """Staff-reviewed wording of one version; replaced as a whole when edited."""

    __tablename__ = "document_sections"
    __table_args__ = (
        UniqueConstraint("document_version_id", "position"),
        UniqueConstraint("workspace_id", "id"),
        _fk(["document_version_id"], f"{SCHEMA}.document_versions"),
        _fk(["entity_id"], "catalog.entities"),
        CheckConstraint("heading IS NULL OR length(heading) <= 200", name="heading_length"),
        CheckConstraint("length(text) BETWEEN 1 AND 4000", name="text_length"),
        CheckConstraint("position >= 0", name="position"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    document_version_id: Mapped[uuid.UUID]
    position: Mapped[int]
    heading: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None]
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=sql_text("'{}'")
    )
    updated_by: Mapped[uuid.UUID | None]


class Chunk(IdMixin, TenantMixin, Base):
    """Derived search unit, rebuilt from sections on publish. Its vector lives in Qdrant."""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "chunk_index"),
        _fk(["document_version_id"], f"{SCHEMA}.document_versions"),
        _fk(["section_id"], f"{SCHEMA}.document_sections"),
        _fk(["entity_id"], "catalog.entities"),
        Index(None, "workspace_id", "document_version_id"),
        # Lexical search, always available (Qdrant adds semantic ranking when configured).
        Index(
            "ix_chunks_full_text",
            # Written exactly as Postgres normalizes it, so migration diffs stay clean.
            sql_text(
                "to_tsvector('simple'::regconfig, "
                "((COALESCE(heading, ''::text) || ' '::text) || text))"
            ),
            postgresql_using="gin",
            info={"manual": True},  # created in revision 0004; Alembic can't diff expressions
        ),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    document_version_id: Mapped[uuid.UUID]
    section_id: Mapped[uuid.UUID]
    chunk_index: Mapped[int]
    heading: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None]
    embedding_model: Mapped[str | None] = mapped_column(Text)  # set once indexed in Qdrant
    created_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=sql_text("now()"))


class Faq(IdMixin, TenantMixin, AuthoringMixin, Base):
    __tablename__ = "faqs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _fk(["entity_id"], "catalog.entities"),
        Index(None, "workspace_id", "publication_status"),
        CheckConstraint("length(canonical_question) BETWEEN 1 AND 500", name="question_length"),
        CheckConstraint("length(approved_answer) BETWEEN 1 AND 2000", name="answer_length"),
        CheckConstraint(PUBLICATION, name="publication_status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    category: Mapped[str | None] = mapped_column(Text)
    canonical_question: Mapped[str] = mapped_column(Text)
    alternative_phrasings: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=sql_text("'{}'")
    )
    approved_answer: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None]
    publication_status: Mapped[str] = mapped_column(
        Text, default="draft", server_default=sql_text("'draft'")
    )
    valid_during: Mapped[Range[datetime] | None] = mapped_column(TSTZRANGE)


class Announcement(IdMixin, TenantMixin, AuthoringMixin, Base):
    """A live update (closure, doctor unavailable ...) valid for a time window."""

    __tablename__ = "announcements"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        _fk(["entity_id"], "catalog.entities"),
        _fk(["location_entity_id"], "catalog.entities"),
        Index(None, "workspace_id", "publication_status"),
        CheckConstraint("length(public_message) BETWEEN 1 AND 2000", name="message_length"),
        CheckConstraint(
            "internal_note IS NULL OR length(internal_note) <= 2000", name="note_length"
        ),
        CheckConstraint("NOT isempty(valid_during)", name="window"),
        CheckConstraint(PUBLICATION, name="publication_status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    kind: Mapped[str] = mapped_column(Text)
    public_message: Mapped[str] = mapped_column(Text)
    internal_note: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None]
    location_entity_id: Mapped[uuid.UUID | None]
    priority: Mapped[int] = mapped_column(default=100, server_default=sql_text("100"))
    valid_during: Mapped[Range[datetime]] = mapped_column(TSTZRANGE)
    publication_status: Mapped[str] = mapped_column(
        Text, default="draft", server_default=sql_text("'draft'")
    )

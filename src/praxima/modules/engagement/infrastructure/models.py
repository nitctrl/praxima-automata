"""CRM tables: contacts, consents, conversations, call events, work items, tasks.

Personal data is stored only as AES-GCM ciphertext (`*_ciphertext` + `pii_key_version`),
bound to workspace, record and field. Phones are also stored as a per-workspace HMAC for
lookup. Payloads, metadata and events never hold personal data.
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
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from praxima.shared.db.base import (
    AuthoringMixin,
    Base,
    IdMixin,
    TenantMixin,
    TimestampMixin,
    utc_now,
)
from praxima.shared.kernel.ids import new_id

SCHEMA = "engagement"
WORKSPACES = "tenancy.workspaces.id"


def _fk(columns: list[str], table: str) -> ForeignKeyConstraint:
    """Composite tenant key: a row can only reference a row of the same workspace."""
    return ForeignKeyConstraint(
        ["workspace_id", *columns], [f"{table}.workspace_id", f"{table}.id"]
    )


class Contact(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        # One contact per phone per workspace (the HMAC is workspace-specific).
        UniqueConstraint("workspace_id", "phone_lookup_hmac"),
        CheckConstraint(
            "consent_status IN ('unknown', 'granted', 'withdrawn')", name="consent_status"
        ),
        CheckConstraint(
            "phone_lookup_hmac IS NULL OR phone_lookup_hmac ~ '^[0-9a-f]{64}$'",
            name="lookup_format",
        ),
        CheckConstraint(
            "(display_name_ciphertext IS NULL AND phone_ciphertext IS NULL) "
            "OR pii_key_version IS NOT NULL",
            name="key_version",
        ),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    display_name_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    phone_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    phone_lookup_hmac: Mapped[str | None] = mapped_column(Text)
    pii_key_version: Mapped[str | None] = mapped_column(Text)
    preferred_language: Mapped[str | None] = mapped_column(Text)
    consent_status: Mapped[str] = mapped_column(
        Text, default="unknown", server_default=sql_text("'unknown'")
    )
    consented_at: Mapped[datetime | None]
    last_confirmed_at: Mapped[datetime | None]
    retention_until: Mapped[datetime | None]
    pii_erased_at: Mapped[datetime | None]


class Consent(IdMixin, TenantMixin, Base):
    """Append-only record of what a person agreed to or withdrew, and under which notice."""

    __tablename__ = "consents"
    __table_args__ = (
        _fk(["contact_id"], f"{SCHEMA}.contacts"),
        _fk(["conversation_id"], f"{SCHEMA}.conversations"),
        Index(None, "workspace_id", "contact_id"),
        CheckConstraint("action IN ('granted', 'withdrawn')", name="action"),
        CheckConstraint("length(notice_version) BETWEEN 1 AND 60", name="notice_version"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    contact_id: Mapped[uuid.UUID | None]
    conversation_id: Mapped[uuid.UUID | None]
    consent_type: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    notice_version: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=sql_text("now()"))
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB(none_as_null=True))


class Conversation(IdMixin, TenantMixin, Base):
    """One call (or chat). Not partitioned, so work items and events can reference it."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("provider", "provider_call_id"),
        _fk(["agent_id"], "agents.agents"),
        _fk(["phone_number_id"], "agents.phone_numbers"),
        _fk(["contact_id"], f"{SCHEMA}.contacts"),
        Index(None, "workspace_id", "started_at"),
        Index(
            "ix_conversations_needing_review",
            "workspace_id",
            "started_at",
            postgresql_where=sql_text("failure_code IS NOT NULL AND NOT is_test"),
        ),
        Index(
            "ix_conversations_active_heartbeat",
            "heartbeat_at",
            postgresql_where=sql_text("status = 'active'"),
        ),
        CheckConstraint("channel IN ('voice', 'chat', 'whatsapp')", name="channel"),
        CheckConstraint("status IN ('active', 'completed', 'failed', 'abandoned')", name="status"),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ended_after_start"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    agent_id: Mapped[uuid.UUID]
    agent_release_id: Mapped[uuid.UUID | None]  # FK added with the releases module (step 4)
    phone_number_id: Mapped[uuid.UUID | None]
    contact_id: Mapped[uuid.UUID | None]
    channel: Mapped[str] = mapped_column(Text, default="voice", server_default=sql_text("'voice'"))
    provider: Mapped[str] = mapped_column(Text)
    provider_call_id: Mapped[str] = mapped_column(Text)
    provider_room_id: Mapped[str | None] = mapped_column(Text)
    caller_number_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    pii_key_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default=sql_text("'active'"))
    started_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=sql_text("now()"))
    answered_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    deadline_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    language: Mapped[str | None] = mapped_column(Text)
    detected_languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=sql_text("'{}'")
    )
    primary_intent: Mapped[str | None] = mapped_column(Text)
    disposition: Mapped[str | None] = mapped_column(Text)
    transfer_attempted: Mapped[bool] = mapped_column(
        default=False, server_default=sql_text("false")
    )
    transfer_succeeded: Mapped[bool] = mapped_column(
        default=False, server_default=sql_text("false")
    )
    safety_flag: Mapped[bool] = mapped_column(default=False, server_default=sql_text("false"))
    failure_code: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)  # sanitized, no personal data
    is_test: Mapped[bool] = mapped_column(default=False, server_default=sql_text("false"))
    retention_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=sql_text("now()"))


class CallEvent(TenantMixin, Base):
    """Append-only call timeline, partitioned monthly (no foreign keys point at it)."""

    __tablename__ = "call_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", "occurred_at"),
        UniqueConstraint("conversation_id", "event_key", "occurred_at"),
        _fk(["conversation_id"], f"{SCHEMA}.conversations"),
        {"schema": SCHEMA, "postgresql_partition_by": "RANGE (occurred_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(default=new_id)
    occurred_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=sql_text("now()"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    conversation_id: Mapped[uuid.UUID]
    event_key: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    sanitized_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


class WorkItemKind(IdMixin, TenantMixin, TimestampMixin, Base):
    """Installed (copied) from the workspace's pack, like entity types."""

    __tablename__ = "work_item_kinds"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", "schema_version"),
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("initial_stage = ANY (stages)", name="initial_stage"),
        CheckConstraint("terminal_stages <@ stages", name="terminal_stages"),
        CheckConstraint("status IN ('active', 'deprecated')", name="status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[int]
    payload_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    stages: Mapped[list[str]] = mapped_column(ARRAY(Text))
    initial_stage: Mapped[str] = mapped_column(Text)
    terminal_stages: Mapped[list[str]] = mapped_column(ARRAY(Text))
    subject_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=sql_text("'{}'")
    )
    source_pack_key: Mapped[str | None] = mapped_column(Text)
    source_pack_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default=sql_text("'active'"))


class WorkItem(IdMixin, TenantMixin, TimestampMixin, Base):
    """A request or lead for staff to follow up. Never a confirmed booking."""

    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key"),
        UniqueConstraint("workspace_id", "id"),
        _fk(["kind_id"], f"{SCHEMA}.work_item_kinds"),
        _fk(["conversation_id"], f"{SCHEMA}.conversations"),
        _fk(["contact_id"], f"{SCHEMA}.contacts"),
        _fk(["entity_id"], "catalog.entities"),
        Index(None, "workspace_id", "stage", "created_at"),
        Index(None, "workspace_id", "entity_id"),
        Index(None, "workspace_id", "assignee_user_id"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        CheckConstraint(
            "(subject_name_ciphertext IS NULL AND callback_number_ciphertext IS NULL "
            "AND staff_note_ciphertext IS NULL) OR pii_key_version IS NOT NULL",
            name="key_version",
        ),
        CheckConstraint("length(idempotency_key) BETWEEN 8 AND 200", name="idempotency_key"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    kind_id: Mapped[uuid.UUID]
    conversation_id: Mapped[uuid.UUID | None]
    contact_id: Mapped[uuid.UUID | None]
    entity_id: Mapped[uuid.UUID | None]
    stage: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'")
    )
    subject_name_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    callback_number_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    staff_note_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    pii_key_version: Mapped[str | None] = mapped_column(Text)
    pii_erased_at: Mapped[datetime | None]
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("iam.users.id"))
    idempotency_key: Mapped[str] = mapped_column(Text)
    sla_due_at: Mapped[datetime | None]
    retention_until: Mapped[datetime | None]
    updated_by: Mapped[uuid.UUID | None]
    row_version: Mapped[int] = mapped_column(server_default=sql_text("1"))

    __mapper_args__ = {"version_id_col": row_version}


class WorkItemEvent(IdMixin, TenantMixin, Base):
    """Append-only stage history of a work item."""

    __tablename__ = "work_item_events"
    __table_args__ = (
        _fk(["work_item_id"], f"{SCHEMA}.work_items"),
        Index(None, "workspace_id", "work_item_id", "occurred_at"),
        CheckConstraint("actor_type IN ('user', 'runtime', 'system')", name="actor_type"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    work_item_id: Mapped[uuid.UUID]
    from_stage: Mapped[str | None] = mapped_column(Text)
    to_stage: Mapped[str | None] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("iam.users.id"))
    occurred_at: Mapped[datetime] = mapped_column(default=utc_now, server_default=sql_text("now()"))
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB(none_as_null=True))


class Task(IdMixin, TenantMixin, AuthoringMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        _fk(["work_item_id"], f"{SCHEMA}.work_items"),
        Index(None, "workspace_id", "assignee_user_id", "status"),
        CheckConstraint("length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint(
            "description IS NULL OR length(description) <= 2000", name="description_length"
        ),
        CheckConstraint("status IN ('open', 'done', 'cancelled')", name="status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(WORKSPACES))
    work_item_id: Mapped[uuid.UUID | None]
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("iam.users.id"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="open", server_default=sql_text("'open'"))
    due_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]

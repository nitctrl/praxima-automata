"""Agent configuration and ingress routing (phone number → agent → workspace)."""

import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from praxima.shared.db.base import AuthoringMixin, Base, IdMixin, TenantMixin, TimestampMixin

SCHEMA = "agents"
E164 = r"^\+[1-9][0-9]{7,14}$"


class Agent(IdMixin, TenantMixin, AuthoringMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug"),
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'", name="slug_format"),
        CheckConstraint("length(name) BETWEEN 1 AND 150", name="name_length"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        CheckConstraint(
            "length(greeting_message) BETWEEN 1 AND 2000 "
            "AND length(emergency_message) BETWEEN 1 AND 2000 "
            "AND length(fallback_message) BETWEEN 1 AND 2000",
            name="messages",
        ),
        CheckConstraint("prompt_version >= 1", name="prompt_version"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.workspaces.id"))
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default=text("'active'"))
    persona: Mapped[str | None] = mapped_column(Text)
    prompt_template_key: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    stt_provider: Mapped[str | None] = mapped_column(Text)
    stt_model: Mapped[str | None] = mapped_column(Text)
    stt_language: Mapped[str | None] = mapped_column(Text)
    llm_provider: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    tts_provider: Mapped[str | None] = mapped_column(Text)
    tts_model: Mapped[str | None] = mapped_column(Text)
    tts_language: Mapped[str | None] = mapped_column(Text)
    greeting_message: Mapped[str] = mapped_column(Text)
    emergency_message: Mapped[str] = mapped_column(Text)
    fallback_message: Mapped[str] = mapped_column(Text)
    transfer_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    transfer_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


class PhoneNumber(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "phone_numbers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"], [f"{SCHEMA}.agents.workspace_id", f"{SCHEMA}.agents.id"]
        ),
        # A number routes to exactly one agent at a time; released numbers can be reused.
        Index(None, "phone_number", unique=True, postgresql_where=text("status = 'active'")),
        CheckConstraint(f"phone_number ~ '{E164}'", name="e164"),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        CheckConstraint("status IN ('active', 'inactive', 'released')", name="status"),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.workspaces.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(index=True)
    phone_number: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    trusted_trunk_id: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(
        Text, default="inbound", server_default=text("'inbound'")
    )
    status: Mapped[str] = mapped_column(Text, default="active", server_default=text("'active'"))


class AgentTool(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "agent_tools"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_key"),
        ForeignKeyConstraint(
            ["workspace_id", "agent_id"], [f"{SCHEMA}.agents.workspace_id", f"{SCHEMA}.agents.id"]
        ),
        {"schema": SCHEMA},
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenancy.workspaces.id"))
    agent_id: Mapped[uuid.UUID]
    tool_key: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    updated_by: Mapped[uuid.UUID | None]

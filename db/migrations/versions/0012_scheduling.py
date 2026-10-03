"""Scheduling: per-workspace booking settings and bookings of a resource's time.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-03

A booking reserves a slot of a bookable entity (a doctor, a sales agent). The database
refuses two live bookings (held or confirmed) of the same entity whose slots overlap, so a
double booking can't happen even under concurrent requests. Callers' names and numbers are
stored only as ciphertext bound to workspace, booking and field, like work items.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
_IN_WORKSPACE = "workspace_id = tenancy.current_workspace_id()"

TABLES = f"""
CREATE SCHEMA IF NOT EXISTS scheduling;
REVOKE ALL ON SCHEMA scheduling FROM PUBLIC;

CREATE TABLE scheduling.booking_settings (
    workspace_id uuid PRIMARY KEY REFERENCES tenancy.workspaces (id),
    requires_confirmation boolean NOT NULL DEFAULT true,
    hold_minutes integer NOT NULL DEFAULT 30
        CONSTRAINT ck_booking_settings_hold CHECK (hold_minutes BETWEEN 5 AND 1440),
    min_notice_minutes integer NOT NULL DEFAULT 60
        CONSTRAINT ck_booking_settings_notice CHECK (min_notice_minutes BETWEEN 0 AND 10080),
    horizon_days integer NOT NULL DEFAULT 30
        CONSTRAINT ck_booking_settings_horizon CHECK (horizon_days BETWEEN 1 AND 365),
    slot_minutes integer
        CONSTRAINT ck_booking_settings_slot CHECK (slot_minutes BETWEEN 5 AND 480),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid,
    updated_by uuid,
    deleted_at timestamptz,
    row_version integer NOT NULL DEFAULT 1
);

CREATE TABLE scheduling.bookings (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES tenancy.workspaces (id),
    resource_entity_id uuid NOT NULL,
    subject_entity_id uuid,
    slot tstzrange NOT NULL,
    status text NOT NULL,
    hold_until timestamptz,
    source text NOT NULL,
    conversation_id uuid,
    agent_id uuid,
    subject_name_ciphertext bytea,
    phone_ciphertext bytea,
    phone_last4 text,
    staff_note_ciphertext bytea,
    pii_key_version text,
    cancel_reason text,
    idempotency_key text,
    confirmed_at timestamptz,
    confirmed_by uuid,
    cancelled_at timestamptz,
    cancelled_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid,
    updated_by uuid,
    deleted_at timestamptz,
    row_version integer NOT NULL DEFAULT 1,
    CONSTRAINT uq_bookings_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_bookings_idempotency UNIQUE (workspace_id, idempotency_key),
    CONSTRAINT fk_bookings_resource FOREIGN KEY (workspace_id, resource_entity_id)
        REFERENCES catalog.entities (workspace_id, id),
    CONSTRAINT fk_bookings_subject FOREIGN KEY (workspace_id, subject_entity_id)
        REFERENCES catalog.entities (workspace_id, id),
    CONSTRAINT fk_bookings_conversation FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES engagement.conversations (workspace_id, id),
    CONSTRAINT fk_bookings_agent FOREIGN KEY (workspace_id, agent_id)
        REFERENCES agents.agents (workspace_id, id),
    CONSTRAINT ck_bookings_status CHECK (
        status IN ('held', 'confirmed', 'cancelled', 'completed', 'no_show', 'expired')),
    CONSTRAINT ck_bookings_source CHECK (source IN ('call', 'staff')),
    CONSTRAINT ck_bookings_slot CHECK (
        NOT isempty(slot) AND lower_inc(slot) AND NOT upper_inc(slot)
        AND NOT lower_inf(slot) AND NOT upper_inf(slot)),
    CONSTRAINT ck_bookings_hold CHECK (status <> 'held' OR hold_until IS NOT NULL),
    CONSTRAINT ck_bookings_last4 CHECK (phone_last4 IS NULL OR phone_last4 ~ '^[0-9]{{4}}$'),
    CONSTRAINT ck_bookings_reason CHECK (cancel_reason IS NULL OR length(cancel_reason) <= 300),
    CONSTRAINT ck_bookings_key_version CHECK (
        (subject_name_ciphertext IS NULL AND phone_ciphertext IS NULL
         AND staff_note_ciphertext IS NULL) OR pii_key_version IS NOT NULL),
    -- No two live bookings of the same resource overlap (needs btree_gist, see 0001).
    CONSTRAINT ex_bookings_no_overlap EXCLUDE USING gist (
        workspace_id WITH =, resource_entity_id WITH =, slot WITH &&
    ) WHERE (status IN ('held', 'confirmed'))
);
CREATE INDEX ix_bookings_resource_start
    ON scheduling.bookings (workspace_id, resource_entity_id, lower(slot));
CREATE INDEX ix_bookings_start ON scheduling.bookings (workspace_id, lower(slot));
CREATE INDEX ix_bookings_held ON scheduling.bookings (workspace_id, hold_until)
    WHERE status = 'held';
"""

POLICIES = "".join(
    f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY {name}_read ON {table} FOR SELECT USING ({_IN_WORKSPACE});
CREATE POLICY {name}_create ON {table} FOR INSERT WITH CHECK ({_IN_WORKSPACE});
CREATE POLICY {name}_update ON {table} FOR UPDATE
    USING ({_IN_WORKSPACE}) WITH CHECK ({_IN_WORKSPACE});
"""
    for table, name in (
        ("scheduling.booking_settings", "booking_settings"),
        ("scheduling.bookings", "bookings"),
    )
)


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(POLICIES)


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

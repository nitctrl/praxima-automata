"""Google Calendar sync: connections, cached busy time, booking events, and a job outbox.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-03

A bookable entry (a doctor, a sales agent) can connect one Google Calendar. Its refresh token
is stored only as ciphertext bound to workspace, connection and field. Booking changes queue a
job in `ops.outbox` in the same transaction; the background worker (`entrypoints/jobs.py`)
claims due jobs across workspaces through `ops.claim_outbox` (the only cross-tenant step) and
runs each one scoped to its workspace. Busy time read from Google blocks slots, for staff and
for the voice agent (`runtime_slot_context` now includes it).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
_IN_WORKSPACE = "workspace_id = tenancy.current_workspace_id()"

TABLES = """
CREATE TABLE scheduling.calendar_connections (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES tenancy.workspaces (id),
    entity_id uuid NOT NULL,
    provider text NOT NULL DEFAULT 'google',
    account_email text,
    calendar_id text NOT NULL DEFAULT 'primary',
    refresh_token_ciphertext bytea,
    pii_key_version text,
    status text NOT NULL DEFAULT 'active',
    error_code text,
    last_synced_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid,
    updated_by uuid,
    deleted_at timestamptz,
    row_version integer NOT NULL DEFAULT 1,
    CONSTRAINT uq_calendar_connections_entity UNIQUE (workspace_id, entity_id),
    CONSTRAINT fk_calendar_connections_entity FOREIGN KEY (workspace_id, entity_id)
        REFERENCES catalog.entities (workspace_id, id),
    CONSTRAINT ck_calendar_connections_provider CHECK (provider IN ('google')),
    CONSTRAINT ck_calendar_connections_status CHECK (status IN ('active', 'error', 'revoked')),
    CONSTRAINT ck_calendar_connections_key CHECK (
        refresh_token_ciphertext IS NULL OR pii_key_version IS NOT NULL)
);

CREATE TABLE scheduling.external_busy (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES tenancy.workspaces (id),
    entity_id uuid NOT NULL,
    busy tstzrange NOT NULL,
    synced_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_external_busy_entity FOREIGN KEY (workspace_id, entity_id)
        REFERENCES catalog.entities (workspace_id, id),
    CONSTRAINT ck_external_busy_range CHECK (NOT isempty(busy))
);
CREATE INDEX ix_external_busy_entity ON scheduling.external_busy USING gist (
    workspace_id, entity_id, busy);

ALTER TABLE scheduling.bookings
    ADD COLUMN calendar_event_id text,
    ADD COLUMN calendar_sync_status text,
    ADD CONSTRAINT ck_bookings_calendar_sync CHECK (
        calendar_sync_status IS NULL
        OR calendar_sync_status IN ('pending', 'synced', 'failed', 'removed'));

CREATE TABLE ops.outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES tenancy.workspaces (id),
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_outbox_status CHECK (status IN ('pending', 'running', 'done', 'failed')),
    CONSTRAINT ck_outbox_kind CHECK (kind ~ '^[a-z_]+\\.[a-z_]+$'),
    CONSTRAINT ck_outbox_payload CHECK (jsonb_typeof(payload) = 'object')
);
CREATE INDEX ix_outbox_due ON ops.outbox (next_attempt_at)
    WHERE status IN ('pending', 'running');
"""

# Forced RLS applies to the owner too, so the two cross-tenant worker functions (owner-run,
# SECURITY DEFINER) need their own narrow policy: the flag they set AND being the table owner.
# The API's login can set the flag but isn't the owner, so it gains nothing.
WORKER = """
CREATE FUNCTION ops.is_worker() RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT COALESCE(pg_catalog.current_setting('app.worker', true) = 'on', false)
       AND current_user = (SELECT pg_catalog.pg_get_userbyid(c.relowner)
                             FROM pg_catalog.pg_class c WHERE c.oid = 'ops.outbox'::regclass)
$$;
"""

POLICIES = (
    "".join(
        f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY {name}_read ON {table} FOR SELECT USING ({_IN_WORKSPACE});
CREATE POLICY {name}_create ON {table} FOR INSERT WITH CHECK ({_IN_WORKSPACE});
CREATE POLICY {name}_update ON {table} FOR UPDATE
    USING ({_IN_WORKSPACE}) WITH CHECK ({_IN_WORKSPACE});
"""
        for table, name in (
            ("scheduling.calendar_connections", "calendar_connections"),
            ("scheduling.external_busy", "external_busy"),
            ("ops.outbox", "outbox"),
        )
    )
    + f"""
-- Busy time is replaced wholesale on each sync.
CREATE POLICY external_busy_delete ON scheduling.external_busy FOR DELETE USING ({_IN_WORKSPACE});
CREATE POLICY outbox_worker ON ops.outbox FOR ALL
    USING (ops.is_worker()) WITH CHECK (ops.is_worker());
CREATE POLICY calendar_connections_worker ON scheduling.calendar_connections FOR SELECT
    USING (ops.is_worker());
"""
)

FUNCTIONS = r"""
-- The worker's only cross-tenant step: lease up to p_limit due jobs (a crashed worker's lease
-- runs out after 5 minutes and the job is claimed again).
CREATE FUNCTION ops.claim_outbox(p_limit integer)
RETURNS TABLE (id uuid, workspace_id uuid, kind text, payload jsonb, attempts integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM pg_catalog.set_config('app.worker', 'on', true);
    RETURN QUERY
    UPDATE ops.outbox o
       SET status = 'running', attempts = o.attempts + 1,
           next_attempt_at = now() + interval '5 minutes', updated_at = now()
     WHERE o.id IN (
        SELECT d.id FROM ops.outbox d
         WHERE d.status IN ('pending', 'running') AND d.next_attempt_at <= now()
         ORDER BY d.next_attempt_at
         LIMIT LEAST(GREATEST(p_limit, 1), 100)
         FOR UPDATE SKIP LOCKED)
    RETURNING o.id, o.workspace_id, o.kind, o.payload, o.attempts;
END $$;

-- Queue a busy-time sync for every active connection not synced for p_minutes (and not
-- already queued). Returns how many were queued.
CREATE FUNCTION scheduling.enqueue_busy_syncs(p_minutes integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_count integer;
BEGIN
    PERFORM pg_catalog.set_config('app.worker', 'on', true);
    INSERT INTO ops.outbox (workspace_id, kind, payload)
    SELECT c.workspace_id, 'calendar.sync_busy',
           pg_catalog.jsonb_build_object('entity_id', c.entity_id)
      FROM scheduling.calendar_connections c
     WHERE c.status = 'active' AND c.deleted_at IS NULL
       AND (c.last_synced_at IS NULL
            OR c.last_synced_at < now() - make_interval(mins => GREATEST(p_minutes, 1)))
       AND NOT EXISTS (
            SELECT 1 FROM ops.outbox o
             WHERE o.kind = 'calendar.sync_busy' AND o.status IN ('pending', 'running')
               AND o.workspace_id = c.workspace_id
               AND o.payload ->> 'entity_id' = c.entity_id::text);
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END $$;

-- The voice agent's slot context now also counts busy time read from the entry's calendar.
CREATE OR REPLACE FUNCTION scheduling.runtime_slot_context(
    p_workspace_id uuid, p_entity_id uuid, p_from timestamptz, p_to timestamptz
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_rules jsonb;
BEGIN
    v_rules := scheduling.runtime_booking_rules(p_workspace_id, p_entity_id);
    IF NOT (v_rules ->> 'enabled')::boolean THEN
        RETURN v_rules;
    END IF;
    RETURN v_rules || pg_catalog.jsonb_build_object('busy', COALESCE((
        SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_array(lower(r), upper(r)))
          FROM (
            SELECT b.slot AS r FROM scheduling.bookings b
             WHERE b.resource_entity_id = p_entity_id
               AND b.slot && pg_catalog.tstzrange(p_from, p_to, '[)')
               AND (b.status = 'confirmed' OR (b.status = 'held' AND b.hold_until > now()))
            UNION ALL
            SELECT x.busy FROM scheduling.external_busy x
             WHERE x.entity_id = p_entity_id
               AND x.busy && pg_catalog.tstzrange(p_from, p_to, '[)')
          ) ranges
    ), '[]'::jsonb));
END $$;

-- Bookings the agent confirms at once go to the calendar like staff bookings do.
CREATE FUNCTION scheduling.queue_calendar_sync() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status OR NEW.slot IS DISTINCT FROM OLD.slot THEN
        IF NEW.status = 'confirmed' OR NEW.calendar_event_id IS NOT NULL THEN
            INSERT INTO ops.outbox (workspace_id, kind, payload)
            VALUES (NEW.workspace_id, 'calendar.sync_booking',
                    pg_catalog.jsonb_build_object('booking_id', NEW.id));
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE FUNCTION scheduling.queue_calendar_sync_insert() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF NEW.status = 'confirmed' THEN
        INSERT INTO ops.outbox (workspace_id, kind, payload)
        VALUES (NEW.workspace_id, 'calendar.sync_booking',
                pg_catalog.jsonb_build_object('booking_id', NEW.id));
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER bookings_calendar_sync AFTER UPDATE ON scheduling.bookings
    FOR EACH ROW EXECUTE FUNCTION scheduling.queue_calendar_sync();
CREATE TRIGGER bookings_calendar_sync_insert AFTER INSERT ON scheduling.bookings
    FOR EACH ROW EXECUTE FUNCTION scheduling.queue_calendar_sync_insert();

REVOKE ALL ON FUNCTION ops.claim_outbox(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION scheduling.enqueue_busy_syncs(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION scheduling.queue_calendar_sync() FROM PUBLIC;
REVOKE ALL ON FUNCTION scheduling.queue_calendar_sync_insert() FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(WORKER)
    op.execute(POLICIES)
    op.execute(FUNCTIONS)


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

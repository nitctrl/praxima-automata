"""Voice runtime booking: open-slot context and booking a slot on a call.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-03

Like 0009, the voice worker's login reads no tables: it calls these SECURITY DEFINER
functions with the workspace its call was resolved to (trusted ingress), and each sets
`app.workspace_id` so forced RLS applies. The worker computes open slots from its pinned
release's published hours (shared/kernel/slots.py); the database enforces the rest: the entry
is bookable in the workspace's pack, the slot is within notice and window, and live bookings
never overlap (0012's exclusion constraint). Names and phones arrive already encrypted.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
FUNCTIONS = r"""
-- The pack's booking block for a workspace, and whether an entity's type is bookable in it.
CREATE FUNCTION scheduling.runtime_booking_rules(p_workspace_id uuid, p_entity_id uuid)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_booking jsonb;
    v_type text;
    v_settings record;
BEGIN
    PERFORM pg_catalog.set_config('app.workspace_id', p_workspace_id::text, true);
    SELECT pv.manifest -> 'booking' INTO v_booking
      FROM tenancy.workspaces w
      JOIN tenancy.pack_versions pv ON pv.pack_key = w.pack_key AND pv.version = w.pack_version
     WHERE w.id = p_workspace_id AND w.deleted_at IS NULL;
    IF v_booking IS NULL OR v_booking = 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('enabled', false, 'reason', 'no_booking');
    END IF;
    SELECT t.key INTO v_type
      FROM catalog.entities e JOIN catalog.entity_types t ON t.id = e.entity_type_id
     WHERE e.id = p_entity_id AND e.deleted_at IS NULL;
    IF v_type IS NULL OR NOT (v_booking -> 'resource_types') ? v_type THEN
        RETURN pg_catalog.jsonb_build_object('enabled', false, 'reason', 'not_bookable');
    END IF;
    SELECT s.requires_confirmation, s.hold_minutes, s.min_notice_minutes, s.horizon_days,
           s.slot_minutes
      INTO v_settings
      FROM scheduling.booking_settings s WHERE s.workspace_id = p_workspace_id;
    RETURN pg_catalog.jsonb_build_object(
        'enabled', true,
        'subject_types', COALESCE(v_booking -> 'subject_types', '[]'::jsonb),
        'slot_minutes', COALESCE(v_settings.slot_minutes, (v_booking ->> 'slot_minutes')::integer),
        'requires_confirmation', COALESCE(v_settings.requires_confirmation, true),
        'hold_minutes', COALESCE(v_settings.hold_minutes, 30),
        'min_notice_minutes', COALESCE(v_settings.min_notice_minutes, 60),
        'horizon_days', COALESCE(v_settings.horizon_days, 30)
    );
END $$;

-- Booking rules plus the entity's busy ranges in [p_from, p_to): what the worker needs to
-- compute open slots from its release's published hours.
CREATE FUNCTION scheduling.runtime_slot_context(
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
        SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_array(lower(b.slot), upper(b.slot)))
          FROM scheduling.bookings b
         WHERE b.resource_entity_id = p_entity_id
           AND b.slot && pg_catalog.tstzrange(p_from, p_to, '[)')
           AND (b.status = 'confirmed' OR (b.status = 'held' AND b.hold_until > now()))
    ), '[]'::jsonb));
END $$;

-- Book one slot for a caller. Returns {status: held|confirmed, id, starts_at, ends_at,
-- hold_until, created} or {status: taken|outside_window|not_bookable|...}. Idempotent per key.
CREATE FUNCTION scheduling.runtime_book_slot(
    p_workspace_id uuid, p_conversation_id uuid, p_agent_id uuid, p_id uuid,
    p_entity_id uuid, p_subject_entity_id uuid, p_starts_at timestamptz,
    p_idempotency_key text, p_subject_name_ciphertext bytea, p_phone_ciphertext bytea,
    p_phone_last4 text, p_pii_key_version text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_rules jsonb;
    v_existing record;
    v_subject_type text;
    v_minutes integer;
    v_status text;
    v_hold timestamptz;
    v_ends timestamptz;
BEGIN
    v_rules := scheduling.runtime_booking_rules(p_workspace_id, p_entity_id);
    SELECT b.id, b.status, lower(b.slot) AS starts_at, upper(b.slot) AS ends_at, b.hold_until
      INTO v_existing
      FROM scheduling.bookings b WHERE b.idempotency_key = p_idempotency_key;
    IF v_existing.id IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'status', v_existing.status, 'id', v_existing.id, 'created', false,
            'starts_at', v_existing.starts_at, 'ends_at', v_existing.ends_at,
            'hold_until', v_existing.hold_until);
    END IF;
    IF NOT (v_rules ->> 'enabled')::boolean THEN
        RETURN pg_catalog.jsonb_build_object('status', v_rules ->> 'reason');
    END IF;
    IF p_subject_entity_id IS NOT NULL THEN
        SELECT t.key INTO v_subject_type
          FROM catalog.entities e JOIN catalog.entity_types t ON t.id = e.entity_type_id
         WHERE e.id = p_subject_entity_id AND e.deleted_at IS NULL;
        IF v_subject_type IS NULL OR NOT (v_rules -> 'subject_types') ? v_subject_type THEN
            RETURN pg_catalog.jsonb_build_object('status', 'subject_not_allowed');
        END IF;
    END IF;
    IF p_starts_at < now() + make_interval(mins => (v_rules ->> 'min_notice_minutes')::integer)
       OR p_starts_at > now() + make_interval(days => (v_rules ->> 'horizon_days')::integer) THEN
        RETURN pg_catalog.jsonb_build_object('status', 'outside_window');
    END IF;
    -- Lapsed holds free their slot before the overlap check.
    UPDATE scheduling.bookings SET status = 'expired', updated_at = now()
     WHERE resource_entity_id = p_entity_id AND status = 'held' AND hold_until <= now();
    v_minutes := (v_rules ->> 'slot_minutes')::integer;
    v_ends := p_starts_at + make_interval(mins => v_minutes);
    IF (v_rules ->> 'requires_confirmation')::boolean THEN
        v_status := 'held';
        v_hold := now() + make_interval(mins => (v_rules ->> 'hold_minutes')::integer);
    ELSE
        v_status := 'confirmed';
    END IF;
    BEGIN
        INSERT INTO scheduling.bookings (
            id, workspace_id, resource_entity_id, subject_entity_id, slot, status, hold_until,
            source, conversation_id, agent_id, subject_name_ciphertext, phone_ciphertext,
            phone_last4, pii_key_version, idempotency_key, confirmed_at, created_at, updated_at
        ) VALUES (
            p_id, p_workspace_id, p_entity_id, p_subject_entity_id,
            pg_catalog.tstzrange(p_starts_at, v_ends, '[)'), v_status, v_hold, 'call',
            p_conversation_id, p_agent_id, p_subject_name_ciphertext, p_phone_ciphertext,
            p_phone_last4, p_pii_key_version, p_idempotency_key,
            CASE WHEN v_status = 'confirmed' THEN now() END, now(), now()
        );
    EXCEPTION WHEN exclusion_violation THEN
        RETURN pg_catalog.jsonb_build_object('status', 'taken');
    END;
    RETURN pg_catalog.jsonb_build_object(
        'status', v_status, 'id', p_id, 'created', true,
        'starts_at', p_starts_at, 'ends_at', v_ends, 'hold_until', v_hold);
END $$;

REVOKE ALL ON FUNCTION scheduling.runtime_booking_rules(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION scheduling.runtime_slot_context(uuid, uuid, timestamptz, timestamptz)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION scheduling.runtime_book_slot(
    uuid, uuid, uuid, uuid, uuid, uuid, timestamptz, text, bytea, bytea, text, text) FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(FUNCTIONS)


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

"""Voice runtime writes: conversations, call events and requests from calls (step 4c).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29

The voice worker's login reads no tables and writes no tables: it can only call these
SECURITY DEFINER functions (EXECUTE revoked from PUBLIC; granted by scripts/voice_runtime.py).
Each takes the workspace the call was resolved to (by the trusted called number) and sets
`app.workspace_id`, so forced RLS still applies to every row read or written. Personal data
arrives already encrypted (bound to workspace, record and field) and is stored as is.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
FUNCTIONS = r"""
CREATE FUNCTION engagement.runtime_start_conversation(
    p_workspace_id uuid, p_agent_id uuid, p_release_id uuid, p_called_number text,
    p_provider text, p_provider_call_id text, p_is_test boolean
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_id uuid;
    v_number uuid;
BEGIN
    IF p_provider !~ '^[a-z][a-z0-9-]{1,40}$'
       OR p_provider_call_id !~ '^[A-Za-z0-9_.:-]{1,200}$' THEN
        RAISE EXCEPTION 'invalid call identifier' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_catalog.set_config('app.workspace_id', p_workspace_id::text, true);
    IF NOT EXISTS (
        SELECT 1 FROM releases.agent_releases r
         WHERE r.id = p_release_id AND r.agent_id = p_agent_id
    ) THEN
        RAISE EXCEPTION 'release does not belong to this agent' USING ERRCODE = '42501';
    END IF;
    SELECT n.id INTO v_number
      FROM agents.phone_numbers n
     WHERE n.phone_number = p_called_number AND n.agent_id = p_agent_id;
    INSERT INTO engagement.conversations (
        id, workspace_id, agent_id, agent_release_id, phone_number_id, channel, provider,
        provider_call_id, status, started_at, answered_at, is_test, created_at
    ) VALUES (
        gen_random_uuid(), p_workspace_id, p_agent_id, p_release_id, v_number, 'voice',
        p_provider, p_provider_call_id, 'active', now(), now(), p_is_test, now()
    )
    ON CONFLICT (provider, provider_call_id) DO NOTHING
    RETURNING id INTO v_id;
    IF v_id IS NULL THEN  -- the same call started twice (retry): reuse it
        SELECT c.id INTO v_id FROM engagement.conversations c
         WHERE c.provider = p_provider AND c.provider_call_id = p_provider_call_id;
        IF v_id IS NULL THEN
            RAISE EXCEPTION 'call identifier already used' USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN v_id;
END $$;

CREATE FUNCTION engagement.runtime_record_event(
    p_workspace_id uuid, p_conversation_id uuid, p_event_key text, p_event_type text,
    p_payload jsonb
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF p_event_type NOT IN ('call_started', 'tool_called', 'request_created', 'call_ended')
       OR p_event_key !~ '^[A-Za-z0-9_.:-]{1,100}$'
       OR (p_payload IS NOT NULL AND pg_catalog.jsonb_typeof(p_payload) <> 'object') THEN
        RAISE EXCEPTION 'invalid call event' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_catalog.set_config('app.workspace_id', p_workspace_id::text, true);
    INSERT INTO engagement.call_events (
        id, occurred_at, workspace_id, conversation_id, event_key, event_type, sanitized_payload
    ) VALUES (
        gen_random_uuid(), now(), p_workspace_id, p_conversation_id, p_event_key, p_event_type,
        p_payload
    );
END $$;

CREATE FUNCTION engagement.runtime_finish_conversation(
    p_workspace_id uuid, p_conversation_id uuid, p_status text, p_primary_intent text,
    p_disposition text, p_failure_code text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF p_status NOT IN ('completed', 'failed', 'abandoned') THEN
        RAISE EXCEPTION 'invalid call status' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_catalog.set_config('app.workspace_id', p_workspace_id::text, true);
    UPDATE engagement.conversations c
       SET status = p_status, ended_at = now(), primary_intent = p_primary_intent,
           disposition = p_disposition, failure_code = p_failure_code
     WHERE c.id = p_conversation_id AND c.status = 'active';
END $$;

CREATE FUNCTION engagement.runtime_create_work_item(
    p_workspace_id uuid, p_conversation_id uuid, p_id uuid, p_kind text,
    p_idempotency_key text, p_payload jsonb, p_entity_id uuid,
    p_subject_name_ciphertext bytea, p_callback_number_ciphertext bytea,
    p_pii_key_version text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_existing uuid;
    v_kind record;
    v_entity_type text;
BEGIN
    PERFORM pg_catalog.set_config('app.workspace_id', p_workspace_id::text, true);
    SELECT w.id INTO v_existing FROM engagement.work_items w
     WHERE w.idempotency_key = p_idempotency_key;
    IF v_existing IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object('id', v_existing, 'created', false);
    END IF;
    SELECT k.id, k.initial_stage, k.subject_types INTO v_kind
      FROM engagement.work_item_kinds k
     WHERE k.key = p_kind AND k.status = 'active'
     ORDER BY k.schema_version DESC LIMIT 1;
    IF v_kind.id IS NULL THEN
        RAISE EXCEPTION 'unknown request type' USING ERRCODE = '22023';
    END IF;
    IF p_entity_id IS NOT NULL THEN
        SELECT t.key INTO v_entity_type
          FROM catalog.entities e JOIN catalog.entity_types t ON t.id = e.entity_type_id
         WHERE e.id = p_entity_id AND e.deleted_at IS NULL;
        IF v_entity_type IS NULL OR NOT v_entity_type = ANY (v_kind.subject_types) THEN
            RAISE EXCEPTION 'entity not allowed for this request type' USING ERRCODE = '22023';
        END IF;
    END IF;
    INSERT INTO engagement.work_items (
        id, workspace_id, kind_id, conversation_id, entity_id, stage, payload,
        subject_name_ciphertext, callback_number_ciphertext, pii_key_version,
        idempotency_key, created_at, updated_at
    ) VALUES (
        p_id, p_workspace_id, v_kind.id, p_conversation_id, p_entity_id, v_kind.initial_stage,
        COALESCE(p_payload, '{}'::jsonb), p_subject_name_ciphertext,
        p_callback_number_ciphertext, p_pii_key_version, p_idempotency_key, now(), now()
    );
    INSERT INTO engagement.work_item_events (
        id, workspace_id, work_item_id, from_stage, to_stage, actor_type, occurred_at
    ) VALUES (
        gen_random_uuid(), p_workspace_id, p_id, NULL, v_kind.initial_stage, 'runtime', now()
    );
    RETURN pg_catalog.jsonb_build_object('id', p_id, 'created', true);
END $$;

REVOKE ALL ON FUNCTION engagement.runtime_start_conversation(
    uuid, uuid, uuid, text, text, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION engagement.runtime_record_event(uuid, uuid, text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION engagement.runtime_finish_conversation(
    uuid, uuid, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION engagement.runtime_create_work_item(
    uuid, uuid, uuid, text, text, jsonb, uuid, bytea, bytea, text) FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(FUNCTIONS)


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

"""Voice runtime lookup: called number → agent → its live release, in one call (step 4b).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29

The voice worker never reads tables. It calls this SECURITY DEFINER function with the number
the caller dialled (from trusted SIP metadata, never caller input), and gets back the live
release snapshot of the agent that number is routed to, or a reason why there is none.
The function sets the trusted `app.called_number` (so the phone_numbers_ingress policy
exposes exactly that number) and then the resolved `app.workspace_id`, so forced RLS still
applies to every row it reads. EXECUTE is revoked from PUBLIC: grant it to the restricted
runtime login only (see scripts/voice_runtime.py).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
LOOKUP = r"""
CREATE FUNCTION releases.live_release_for_number(p_called_number text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_workspace uuid;
    v_agent uuid;
    v_agent_status text;
    v_release record;
BEGIN
    IF p_called_number IS NULL OR p_called_number !~ '^\+[1-9][0-9]{7,14}$' THEN
        RETURN pg_catalog.jsonb_build_object('reason', 'invalid_number');
    END IF;
    PERFORM pg_catalog.set_config('app.called_number', p_called_number, true);
    SELECT n.workspace_id, n.agent_id INTO v_workspace, v_agent
      FROM agents.phone_numbers n
     WHERE n.phone_number = p_called_number AND n.status = 'active';
    IF v_agent IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('reason', 'unknown_number');
    END IF;
    PERFORM pg_catalog.set_config('app.workspace_id', v_workspace::text, true);
    SELECT a.status INTO v_agent_status
      FROM agents.agents a
     WHERE a.id = v_agent AND a.deleted_at IS NULL;
    IF v_agent_status IS DISTINCT FROM 'active' THEN
        RETURN pg_catalog.jsonb_build_object('reason', 'agent_disabled');
    END IF;
    SELECT r.id, r.version_no, r.snapshot INTO v_release
      FROM releases.agent_releases r
     WHERE r.agent_id = v_agent AND r.status = 'published';
    IF v_release.id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('reason', 'no_live_release');
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'release_id', v_release.id,
        'version_no', v_release.version_no,
        'workspace_id', v_workspace,
        'agent_id', v_agent,
        'snapshot', v_release.snapshot
    );
END $$;
REVOKE ALL ON FUNCTION releases.live_release_for_number(text) FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(LOOKUP)


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

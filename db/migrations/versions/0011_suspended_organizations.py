"""Calls to a suspended organization's numbers get no release (the caller hears "unavailable").

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-29

Same lookup as 0008 plus one check: the workspace's organization must be active. The voice
worker already treats any `reason` as "no release", so it needs no change. CREATE OR REPLACE
keeps the function's owner and grants.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
LOOKUP = r"""
CREATE OR REPLACE FUNCTION releases.live_release_for_number(p_called_number text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_workspace uuid;
    v_organization uuid;
    v_organization_status text;
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
    SELECT w.organization_id INTO v_organization
      FROM tenancy.workspaces w
     WHERE w.id = v_workspace AND w.deleted_at IS NULL;
    PERFORM pg_catalog.set_config('app.organization_id', v_organization::text, true);
    SELECT o.status INTO v_organization_status
      FROM tenancy.organizations o
     WHERE o.id = v_organization AND o.deleted_at IS NULL;
    IF v_organization_status IS DISTINCT FROM 'active' THEN
        RETURN pg_catalog.jsonb_build_object('reason', 'organization_inactive');
    END IF;
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

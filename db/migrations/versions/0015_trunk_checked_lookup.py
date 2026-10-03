"""Release lookup that also checks the SIP trunk the call arrived on.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-04

A phone number may record the trunk it is expected on (`agents.phone_numbers.
trusted_trunk_id`). SIP calls pass the trunk LiveKit reported; if the number names a trunk
and the call came in on another one, there is no release (`untrusted_trunk`), so a number
spoofed onto another trunk can't reach this tenant. Numbers without a trunk behave as before.
Console tests (not SIP) keep using `live_release_for_number`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
FUNCTION = r"""
CREATE FUNCTION releases.live_release_for_call(p_called_number text, p_trunk_id text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_trunk text;
BEGIN
    IF p_called_number IS NULL OR p_called_number !~ '^\+[1-9][0-9]{7,14}$' THEN
        RETURN pg_catalog.jsonb_build_object('reason', 'invalid_number');
    END IF;
    -- The ingress policy lets this transaction see exactly the active number dialled.
    PERFORM pg_catalog.set_config('app.called_number', p_called_number, true);
    SELECT n.trusted_trunk_id INTO v_trunk
      FROM agents.phone_numbers n
     WHERE n.phone_number = p_called_number AND n.status = 'active';
    IF v_trunk IS NOT NULL AND v_trunk IS DISTINCT FROM p_trunk_id THEN
        RETURN pg_catalog.jsonb_build_object('reason', 'untrusted_trunk');
    END IF;
    RETURN releases.live_release_for_number(p_called_number);
END $$;
REVOKE ALL ON FUNCTION releases.live_release_for_call(text, text) FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(FUNCTION)


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

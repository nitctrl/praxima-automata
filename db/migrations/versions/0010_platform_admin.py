"""Platform admin writes: register pack versions, set their status, manage platform admins.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29

The API's restricted login may only read the platform tables (pack_versions, platform_admins).
Platform admins change them through these SECURITY DEFINER functions, which refuse anyone
who isn't a platform admin (`app.user_id` comes from the request's trusted scope). EXECUTE is
revoked from PUBLIC and granted to the API login by scripts/api_role.py. Listing admins is a
function too: a policy calling iam.is_platform_admin() on its own table would recurse.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
FUNCTIONS = r"""
CREATE FUNCTION iam.require_platform_admin() RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF NOT iam.is_platform_admin() THEN
        RAISE EXCEPTION 'platform admins only' USING ERRCODE = 'insufficient_privilege';
    END IF;
END $$;

-- 'registered', 'unchanged' (same checksum) or 'conflict' (files changed, version didn't).
CREATE FUNCTION tenancy.admin_register_pack_version(
    p_key text, p_version text, p_manifest jsonb, p_checksum text
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_existing text;
BEGIN
    PERFORM iam.require_platform_admin();
    SELECT checksum INTO v_existing FROM tenancy.pack_versions
    WHERE pack_key = p_key AND version = p_version;
    IF FOUND THEN
        RETURN CASE WHEN v_existing = p_checksum THEN 'unchanged' ELSE 'conflict' END;
    END IF;
    INSERT INTO tenancy.pack_versions (pack_key, version, manifest, checksum)
    VALUES (p_key, p_version, p_manifest, p_checksum);
    RETURN 'registered';
END $$;

-- False when there's no such version. The table's CHECK constraint validates the status.
CREATE FUNCTION tenancy.admin_set_pack_status(p_key text, p_version text, p_status text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM iam.require_platform_admin();
    UPDATE tenancy.pack_versions SET status = p_status
    WHERE pack_key = p_key AND version = p_version;
    RETURN FOUND;
END $$;

CREATE FUNCTION iam.admin_list_platform_admins()
RETURNS TABLE (user_id uuid, granted_by uuid, created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM iam.require_platform_admin();
    RETURN QUERY SELECT a.user_id, a.granted_by, a.created_at
        FROM iam.platform_admins a ORDER BY a.created_at, a.user_id;
END $$;

-- True when newly granted, false when they already were an admin.
CREATE FUNCTION iam.admin_grant_platform_admin(p_user_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM iam.require_platform_admin();
    INSERT INTO iam.platform_admins (user_id, granted_by)
    VALUES (p_user_id, iam.current_user_id())
    ON CONFLICT (user_id) DO NOTHING;
    RETURN FOUND;
END $$;

-- 'revoked', 'missing', 'self' (you can't remove yourself) or 'last' (keep one admin).
CREATE FUNCTION iam.admin_revoke_platform_admin(p_user_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    PERFORM iam.require_platform_admin();
    IF p_user_id = iam.current_user_id() THEN
        RETURN 'self';
    END IF;
    -- Serialize revocations so two admins can't remove each other at once.
    LOCK TABLE iam.platform_admins IN SHARE ROW EXCLUSIVE MODE;
    IF NOT EXISTS (SELECT 1 FROM iam.platform_admins WHERE user_id = p_user_id) THEN
        RETURN 'missing';
    END IF;
    IF (SELECT count(*) FROM iam.platform_admins) <= 1 THEN
        RETURN 'last';
    END IF;
    DELETE FROM iam.platform_admins WHERE user_id = p_user_id;
    RETURN 'revoked';
END $$;

REVOKE ALL ON FUNCTION iam.require_platform_admin() FROM PUBLIC;
REVOKE ALL ON FUNCTION tenancy.admin_register_pack_version(text, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenancy.admin_set_pack_status(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.admin_list_platform_admins() FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.admin_grant_platform_admin(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.admin_revoke_platform_admin(uuid) FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute(FUNCTIONS)


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

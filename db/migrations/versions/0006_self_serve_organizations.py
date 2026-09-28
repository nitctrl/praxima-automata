"""Self-serve sign-up: a signed-in user with no organization may create one they own.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27

Until now only platform admins could insert organizations. This adds a second, narrow
INSERT policy (permissive policies are OR-ed): the row must name the current user as its
creator, and that user must not belong to any organization yet (one self-serve organization
per person). Whether self-serve sign-up is switched on at all is the API's decision
(PRAXIMA_SELF_SIGNUP); the database guarantees the limits even when it is.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written. Frozen here: never import application code.
SELF_SERVE = """
CREATE POLICY organizations_self_serve ON tenancy.organizations FOR INSERT WITH CHECK (
    created_by IS NOT NULL
    AND created_by = iam.current_user_id()
    AND NOT EXISTS (SELECT 1 FROM iam.memberships m WHERE m.user_id = iam.current_user_id())
);
"""


def upgrade() -> None:
    op.execute(SELF_SERVE)


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

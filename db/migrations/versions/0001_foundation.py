"""Foundation: required extensions and one schema per module.

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy: a revision must not change when application code changes later.
SCHEMAS = (
    "iam",
    "tenancy",
    "agents",
    "releases",
    "catalog",
    "knowledge",
    "engagement",
    "billing",
    "audit",
    "ops",
)
# pgcrypto: digests; citext: emails; btree_gist: exclusion constraints; pg_trgm: fuzzy search.
EXTENSIONS = ("pgcrypto", "citext", "btree_gist", "pg_trgm")
# Supabase's convention, used everywhere so local and Supabase databases match. Extensions
# that already exist elsewhere are left where they are.
EXTENSION_SCHEMA = "extensions"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {EXTENSION_SCHEMA}")
    for extension in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension} WITH SCHEMA {EXTENSION_SCHEMA}")
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")


def downgrade() -> None:
    raise RuntimeError("Migrations are forward-only; ship a new revision instead.")

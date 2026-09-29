"""SQLAlchemy base, tenant scoping and Alembic foundation (ADR 0001)."""

import asyncio
import importlib.util
import io
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, String, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateTable

from praxima.shared.db.base import (
    MODULE_SCHEMAS,
    NAMING_CONVENTION,
    AuthoringMixin,
    Base,
    IdMixin,
    TenantMixin,
)
from praxima.shared.db.engine import (
    Scope,
    bypasses_rls,
    create_engine,
    normalize_url,
    tenant_transaction,
)
from praxima.shared.db.registry import import_models
from praxima.shared.db.settings import ConfigurationError
from praxima.shared.kernel.ids import new_id

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(buffer: io.StringIO | None = None) -> Config:
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.attributes["configure_logger"] = False
    return config


def test_ids_are_uuid7_and_time_ordered():
    first = new_id()
    time.sleep(0.002)
    second = new_id()
    assert first.version == 7 and first.variant == uuid.RFC_4122
    assert first.int >> 80 <= second.int >> 80  # millisecond prefix sorts by creation time
    assert len({new_id() for _ in range(1000)}) == 1000


class _TestBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = Base.type_annotation_map


class _Widget(IdMixin, TenantMixin, AuthoringMixin, _TestBase):
    __tablename__ = "widgets"
    __table_args__ = (UniqueConstraint("workspace_id", "key"), {"schema": "catalog"})
    key: Mapped[str] = mapped_column(String(50))


def test_mixins_add_standard_columns_and_optimistic_locking():
    columns = _Widget.__table__.c
    assert {
        "id",
        "workspace_id",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "deleted_at",
        "row_version",
    } <= set(columns.keys())
    assert not columns.workspace_id.nullable
    assert columns.deleted_at.nullable
    assert columns.created_at.type.timezone
    assert _Widget.__mapper__.version_id_col is columns.row_version
    assert _Widget.__table__.c.id.default.arg.__name__ == "new_id"


def test_constraint_names_are_deterministic():
    ddl = str(CreateTable(_Widget.__table__).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT pk_widgets PRIMARY KEY" in ddl
    assert "CONSTRAINT uq_widgets_workspace_id_key UNIQUE" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl


def test_engine_requires_psycopg_url():
    with pytest.raises(ConfigurationError):
        create_engine("mysql://user:pass@localhost/db")
    assert normalize_url("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.committed = self.rolled_back = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        try:
            yield
            self.committed = True
        except BaseException:
            self.rolled_back = True
            raise

    async def execute(self, statement: object, params: dict[str, str]) -> None:
        self.calls.append((str(statement), params))


SET = "SELECT set_config(:name, :value, true)"


def test_tenant_transaction_scopes_workspace_and_organization():
    session = _FakeSession()
    workspace, organization = new_id(), new_id()

    async def run() -> None:
        async with tenant_transaction(lambda: session, workspace, organization):  # type: ignore[arg-type]
            pass

    asyncio.run(run())
    assert session.calls == [
        (SET, {"name": "app.workspace_id", "value": str(workspace)}),
        (SET, {"name": "app.organization_id", "value": str(organization)}),
    ]
    assert session.committed


def test_tenant_transaction_rolls_back_on_error():
    session = _FakeSession()

    async def run() -> None:
        async with tenant_transaction(lambda: session, new_id()):  # type: ignore[arg-type]
            raise ValueError("boom")

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert session.rolled_back and not session.committed


def test_every_module_model_lives_in_infrastructure():
    for name in import_models():
        assert name.endswith(".infrastructure.models")
    for table in Base.metadata.tables.values():
        assert table.schema in MODULE_SCHEMAS, table.fullname


def test_offline_sql_creates_schemas_and_refuses_downgrade(monkeypatch):
    monkeypatch.delenv("DB_OWNER_DATABASE_URL", raising=False)
    buffer = io.StringIO()
    command.upgrade(alembic_config(buffer), "head", sql=True)
    sql = buffer.getvalue()
    assert "CREATE TABLE ops.alembic_version" in sql
    for schema in MODULE_SCHEMAS:
        assert f"CREATE SCHEMA IF NOT EXISTS {schema};" in sql
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA extensions;" in sql
    assert "CREATE EXTENSION IF NOT EXISTS citext;" not in sql  # never into public
    with pytest.raises(RuntimeError, match="forward-only"):
        command.downgrade(alembic_config(io.StringIO()), "0001:base", sql=True)


def test_revisions_are_numbered_and_linear():
    versions = sorted((ROOT / "db/migrations/versions").glob("*.py"))
    assert versions and all(path.name[:4].isdigit() for path in versions)
    assert [path.name[:4] for path in versions] == [f"{i:04d}" for i in range(1, len(versions) + 1)]


@pytest.mark.skipif(
    not os.environ.get("PRAXIMA_TEST_DATABASE_URL"),
    reason="Set PRAXIMA_TEST_DATABASE_URL to a disposable database on an external Postgres.",
)
def test_upgrade_against_real_postgres_is_idempotent(monkeypatch):
    monkeypatch.setenv("DB_OWNER_DATABASE_URL", os.environ["PRAXIMA_TEST_DATABASE_URL"])
    command.upgrade(alembic_config(), "head")
    command.upgrade(alembic_config(), "head")  # second run applies nothing

    from sqlalchemy import create_engine as create_sync_engine
    from sqlalchemy import text

    engine = create_sync_engine(os.environ["PRAXIMA_TEST_DATABASE_URL"])
    with engine.connect() as connection:
        schemas = set(
            connection.execute(
                text("SELECT nspname FROM pg_namespace WHERE nspname = ANY(:names)"),
                {"names": list(MODULE_SCHEMAS)},
            ).scalars()
        )
        version = connection.execute(text("SELECT version_num FROM ops.alembic_version")).scalar()
    engine.dispose()
    assert schemas == set(MODULE_SCHEMAS)
    assert version == sorted(p.name[:4] for p in (ROOT / "db/migrations/versions").glob("*.py"))[-1]


def test_scope_sets_only_what_is_known():
    user, org = new_id(), new_id()
    assert Scope(user_id=user, organization_id=org).settings() == [
        ("app.organization_id", str(org)),
        ("app.user_id", str(user)),
    ]
    assert Scope().settings() == []


def test_optional_json_columns_store_sql_null():
    """SQLAlchemy writes Python None as JSON 'null' unless none_as_null; that breaks
    'IS NULL' checks and CHECK (attributes IS NULL OR jsonb_typeof(...) = 'object')."""
    from sqlalchemy.dialects.postgresql import JSONB

    import_models()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, JSONB) and column.nullable:
                assert column.type.none_as_null, f"{table.fullname}.{column.name}"


def test_rls_bypass_check_is_unknown_when_unreachable():
    # Startup goes on (v1 answers 503 until the database is back); nothing is guessed.
    assert bypasses_rls("postgresql://nobody@127.0.0.1:1/none") is None


@pytest.mark.skipif(
    not os.environ.get("PRAXIMA_TEST_DATABASE_URL"),
    reason="Set PRAXIMA_TEST_DATABASE_URL to a disposable database on an external Postgres.",
)
def test_rls_bypass_check_against_real_postgres():
    # The test login is the (non-superuser) owner, and FORCE RLS applies to it.
    assert bypasses_rls(os.environ["PRAXIMA_TEST_DATABASE_URL"]) is False


def test_api_role_script_covers_every_table_schema():
    """scripts/api_role.py grants each module schema that has tables (ops stays owner-only)."""
    spec = importlib.util.spec_from_file_location("api_role", ROOT / "scripts/api_role.py")
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    import_models()
    with_tables = {table.schema for table in Base.metadata.tables.values()} - {"ops"}
    assert with_tables <= set(script.SCHEMAS) <= set(MODULE_SCHEMAS)
    assert all(name.split(".")[0] in script.SCHEMAS for name in script.READ_ONLY)
    migrations = "".join(p.read_text() for p in (ROOT / "db/migrations/versions").glob("*.py"))
    for function in script.FUNCTIONS:  # granted functions exist and are closed to PUBLIC
        name = function.split("(")[0]
        assert f"CREATE FUNCTION {name}(" in migrations
        assert f"REVOKE ALL ON FUNCTION {name}(" in migrations

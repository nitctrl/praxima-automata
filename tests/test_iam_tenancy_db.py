"""Step 1 against a real Postgres: login, tenant isolation (RLS), conflicts, paging, audit.

Runs only when PRAXIMA_TEST_DATABASE_URL points at a disposable database, e.g.
postgresql+psycopg://<user>@/praxima_test?host=/var/run/postgresql
"""

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from support.queries import count_queries

from praxima.modules import iam, tenancy
from praxima.packs import loader as packs
from praxima.shared.db.base import Base
from praxima.shared.db.engine import Scope, create_engine, scoped_transaction, session_factory
from praxima.shared.db.pagination import PageRequest
from praxima.shared.db.registry import import_models
from praxima.shared.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from praxima.shared.kernel.ids import new_id

URL = os.environ.get("PRAXIMA_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    "audit.audit_log, knowledge.chunks, knowledge.document_sections, knowledge.faqs, "
    "knowledge.announcements, knowledge.document_versions, knowledge.documents, "
    "knowledge.sources, catalog.availability_exceptions, catalog.availability_rules, "
    "catalog.entity_relations, catalog.entities, catalog.entity_types, agents.agent_tools, "
    "agents.phone_numbers, agents.agents, iam.api_keys, iam.memberships, iam.identities, "
    "iam.platform_admins, iam.users, tenancy.workspaces, tenancy.organizations, "
    "tenancy.pack_versions"
)
CLINIC = packs.load("clinic")
DRAFT = tenancy.WorkspaceDraft(
    slug="main",
    name="Main clinic",
    industry="healthcare",
    pack_key="clinic",
    pack_version="1.0.0",
    timezone="Asia/Kolkata",
    default_language="hi-IN",
    supported_languages=("hi-IN", "en-IN"),
)


@pytest.fixture(scope="module")
def migrated() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False
    os.environ["DB_OWNER_DATABASE_URL"] = URL
    command.upgrade(config, "head")


@pytest.fixture
def admin_id(migrated: None) -> uuid.UUID:
    """Empty tables, one pack version and one platform admin (seeded as the owner role)."""
    admin = new_id()
    engine = create_sync_engine(URL)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        connection.execute(
            text(
                "INSERT INTO tenancy.pack_versions (pack_key, version, manifest, checksum) "
                "VALUES (:key, :version, CAST(:manifest AS jsonb), :checksum)"
            ),
            {
                "key": CLINIC.key,
                "version": CLINIC.version,
                "manifest": json.dumps(CLINIC.payload()),
                "checksum": CLINIC.checksum(),
            },
        )
        connection.execute(
            text("INSERT INTO iam.users (id, email) VALUES (:id, 'admin@platform.test')"),
            {"id": admin},
        )
        connection.execute(
            text("INSERT INTO iam.platform_admins (user_id) VALUES (:id)"), {"id": admin}
        )
    engine.dispose()
    return admin


Sessions = async_sessionmaker[AsyncSession]


def run(scenario: Callable[[AsyncEngine, Sessions], Awaitable[None]]) -> None:
    async def main() -> None:
        engine = create_engine(URL)
        try:
            await scenario(engine, session_factory(engine))
        finally:
            await engine.dispose()

    asyncio.run(main())


def identity(subject: str, email: str, verified: bool = True) -> iam.VerifiedIdentity:
    return iam.VerifiedIdentity("supabase", subject, email, verified, None)


def login_scope(who: iam.VerifiedIdentity) -> Scope:
    return Scope(
        identity_provider=who.provider,
        identity_subject=who.subject,
        identity_email=who.email.lower() if who.email_verified else None,
    )


async def login(sessions: Sessions, who: iam.VerifiedIdentity) -> iam.Principal:
    async with scoped_transaction(sessions, login_scope(who)) as session:
        return await iam.login(session, who)


async def onboard(
    sessions: Sessions, admin_id: uuid.UUID, slug: str, owner: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Platform admin creates an organization; its owner creates the first workspace."""
    admin = iam.Actor(admin_id, None, is_platform_admin=True)
    async with scoped_transaction(sessions, Scope(user_id=admin_id)) as session:
        org = await tenancy.create_organization(
            session, admin, slug=slug, name=slug.title(), owner_user_id=owner
        )
    async with scoped_transaction(sessions, Scope(user_id=owner, organization_id=org)) as session:
        workspace = await tenancy.create_workspace(
            session, iam.Actor(owner, "owner"), organization_id=org, draft=DRAFT
        )
    return org, workspace


def test_models_never_rely_on_server_only_defaults():
    """INSERT must not need RETURNING: under RLS a new row may not be readable back yet."""
    import_models()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.server_default is not None and column.name != "row_version":
                assert column.default is not None, f"{table.fullname}.{column.name}"


def test_login_provisions_once_then_reuses_the_user(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        first = await login(sessions, identity("sub-a", "Asha@Example.test"))
        again = await login(sessions, identity("sub-a", "asha@example.test"))
        other = await login(sessions, identity("sub-b", "bina@example.test"))
        assert first == again and first.email == "asha@example.test"
        assert other.user_id != first.user_id

    run(scenario)


def test_invited_user_is_linked_only_with_a_verified_email(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        invited = new_id()
        async with scoped_transaction(sessions, Scope(user_id=admin_id)) as session:
            await session.execute(
                text("INSERT INTO iam.users (id, email) VALUES (:id, 'new@clinic.test')"),
                {"id": invited},
            )
        with pytest.raises(Conflict):  # unverified email: never taken over
            await login(sessions, identity("sub-x", "new@clinic.test", verified=False))
        linked = await login(sessions, identity("sub-y", "new@clinic.test"))
        assert linked.user_id == invited

    run(scenario)


def test_tenants_are_isolated_by_the_database(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        a = await login(sessions, identity("sub-a", "a@one.test"))
        b = await login(sessions, identity("sub-b", "b@two.test"))
        org_a, ws_a = await onboard(sessions, admin_id, "one", a.user_id)
        org_b, ws_b = await onboard(sessions, admin_id, "two", b.user_id)

        async with scoped_transaction(sessions, Scope(user_id=a.user_id)) as session:
            assert [w.id for w in await tenancy.visible_workspaces(session)] == [ws_a]
            assert [
                m.organization_id for m in await iam.active_memberships(session, a.user_id)
            ] == [org_a]

        scope_a = Scope(user_id=a.user_id, organization_id=org_a)
        async with scoped_transaction(sessions, scope_a) as session:
            with pytest.raises(NotFound):
                await tenancy.get_workspace(session, ws_b)
            assert await iam.role_in(session, b.user_id, org_b) is None  # b's rows are hidden

        # Even if application code were wrong, RLS refuses writes into another tenant.
        with pytest.raises(PermissionDenied):
            async with scoped_transaction(sessions, scope_a) as session:
                await iam.set_membership(
                    session,
                    iam.Actor(a.user_id, "owner"),
                    organization_id=org_b,
                    user_id=a.user_id,
                    role="viewer",
                )

        # The composite key refuses a workspace from another organization.
        with pytest.raises(ValidationFailed):
            async with scoped_transaction(sessions, Scope(user_id=admin_id)) as session:
                await iam.set_membership(
                    session,
                    iam.Actor(admin_id, None, is_platform_admin=True),
                    organization_id=org_a,
                    user_id=a.user_id,
                    role="staff",
                    workspace_id=ws_b,
                )

    run(scenario)


def test_stale_workspace_update_is_a_conflict(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        owner = await login(sessions, identity("sub-o", "o@one.test"))
        org, ws = await onboard(sessions, admin_id, "one", owner.user_id)
        scope = Scope(user_id=owner.user_id, organization_id=org)
        actor = iam.Actor(owner.user_id, "owner")
        change = tenancy.WorkspaceChanges(name="Renamed")

        async with scoped_transaction(sessions, scope) as session:
            await tenancy.update_workspace(
                session, actor, workspace_id=ws, row_version=1, changes=change
            )
        async with scoped_transaction(sessions, scope) as session:
            assert (await tenancy.get_workspace(session, ws)).row_version == 2
            with pytest.raises(Conflict):
                await tenancy.update_workspace(
                    session, actor, workspace_id=ws, row_version=1, changes=change
                )
            with pytest.raises(PermissionDenied):
                await tenancy.update_workspace(
                    session,
                    iam.Actor(owner.user_id, "viewer"),
                    workspace_id=ws,
                    row_version=2,
                    changes=change,
                )
            with pytest.raises(ValidationFailed):
                await tenancy.update_workspace(
                    session,
                    actor,
                    workspace_id=ws,
                    row_version=2,
                    changes=tenancy.WorkspaceChanges(timezone="Mars/Olympus"),
                )

    run(scenario)


def test_member_pages_take_one_query_each(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        owner = await login(sessions, identity("sub-o", "o@one.test"))
        org, ws = await onboard(sessions, admin_id, "one", owner.user_id)
        scope = Scope(user_id=owner.user_id, organization_id=org)
        for n in range(5):
            member = await login(sessions, identity(f"sub-{n}", f"m{n}@one.test"))
            async with scoped_transaction(sessions, scope) as session:
                await iam.set_membership(
                    session,
                    iam.Actor(owner.user_id, "owner"),
                    organization_id=org,
                    user_id=member.user_id,
                    role="staff",
                    workspace_id=ws,
                )

        seen: list[str] = []
        cursor = None
        async with scoped_transaction(sessions, scope) as session:
            for _ in range(3):
                with count_queries(engine) as statements:
                    page = await iam.members_page(session, org, ws, PageRequest(2, cursor))
                    seen += [m.email for m in page.items]  # touches the joined user
                assert len(statements) == 1, statements
                cursor = page.next_cursor
        assert cursor is None
        assert sorted(seen) == [f"m{n}@one.test" for n in range(5)]
        assert seen == [f"m{n}@one.test" for n in reversed(range(5))]  # newest first

    run(scenario)


def test_audit_is_append_only_and_tenant_scoped(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        a = await login(sessions, identity("sub-a", "a@one.test"))
        b = await login(sessions, identity("sub-b", "b@two.test"))
        org_a, _ = await onboard(sessions, admin_id, "one", a.user_id)
        org_b, _ = await onboard(sessions, admin_id, "two", b.user_id)
        count = text("SELECT count(*) FROM audit.audit_log")

        async with scoped_transaction(sessions, Scope(organization_id=org_a)) as session:
            actions = set(await session.scalars(text("SELECT action FROM audit.audit_log")))
            assert actions == {"organization.create", "membership.set", "workspace.create"}
            # Partitions have RLS with no policy: they can't be read around the parent.
            month = await session.scalar(text("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYYMM')"))
            assert await session.scalar(text(f"SELECT count(*) FROM audit.audit_log_p{month}")) == 0

        async with scoped_transaction(sessions, Scope(organization_id=org_b)) as session:
            assert await session.scalar(count) == 3

        # Layer 1, RLS: there is no UPDATE or DELETE policy, so nothing can match.
        async with scoped_transaction(sessions, Scope(organization_id=org_a)) as session:
            changed = await session.execute(text("UPDATE audit.audit_log SET action = 'x'"))
            deleted = await session.execute(text("DELETE FROM audit.audit_log"))
            assert changed.rowcount == 0 and deleted.rowcount == 0  # type: ignore[attr-defined]
            assert await session.scalar(count) == 3
        # Layer 2, trigger: rejects changes even for a role that bypasses RLS.
        async with scoped_transaction(sessions, Scope()) as session:
            triggers = await session.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgrelid = 'audit.audit_log'::regclass "
                    "AND NOT tgisinternal"
                )
            )
            assert list(triggers) == ["audit_log_append_only"]

    run(scenario)

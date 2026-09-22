"""Actual PostgreSQL RLS, not mocked authorization or an admin-only proof.

Setup uses migration privilege; every isolation assertion switches to authenticated
or clinic_runtime. All mutation fixtures are rolled back, including auth users.
"""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from clinic.db import RuntimeDatabase
from clinic.development import fixture_id
from clinic.resolver import (
    ClinicResolver,
    ClinicUnavailable,
    ConfigurationRepository,
    InboundDestination,
    PostgresResolutionRepository,
)
from clinic.settings import ConfigurationError
from clinic.staff import Forbidden, StaffRepository

pytestmark = pytest.mark.integration
A, B = fixture_id("A"), fixture_id("B")


def authenticate(db, clinic=A, role="manager", active=True):
    user = uuid4()
    db.execute("INSERT INTO auth.users (id) VALUES (%s)", (user,))
    db.execute(
        "INSERT INTO public.clinic_users (clinic_id,auth_user_id,role,status) VALUES (%s,%s,%s,%s)",
        (clinic, user, role, "active" if active else "inactive"),
    )
    db.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (str(user),))
    db.execute("SET LOCAL ROLE authenticated")
    return user


def test_every_tenant_table_has_rls_and_key(db):
    rows = db.execute(
        "SELECT c.relname, c.relrowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind='r'"
    ).fetchall()
    assert len(rows) == 22
    assert all(row[1] for row in rows)
    for name, _ in rows:
        column = "id" if name == "clinics" else "clinic_id"
        assert db.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
            (name, column),
        ).fetchone() == ("NO",)


@pytest.mark.parametrize("clinic,other", [(A, B), (B, A)])
def test_authenticated_cannot_read_other_clinic(db, clinic, other):
    authenticate(db, clinic)
    assert db.execute("SELECT current_user").fetchone() == ("authenticated",)
    rows = db.execute("SELECT DISTINCT clinic_id FROM public.doctors").fetchall()
    assert rows == [(clinic,)]
    assert db.execute("SELECT id FROM public.doctors WHERE clinic_id=%s", (other,)).fetchall() == []
    assert (
        db.execute(
            "SELECT id FROM public.knowledge_documents WHERE clinic_id=%s", (other,)
        ).fetchall()
        == []
    )


def test_manager_cannot_modify_other_schedule_or_move_tenant(db):
    authenticate(db)
    assert (
        db.execute(
            "UPDATE public.weekly_schedules SET status='inactive' WHERE clinic_id=%s RETURNING id",
            (B,),
        ).fetchall()
        == []
    )
    assert db.execute(
        "UPDATE public.weekly_schedules SET status='inactive' WHERE clinic_id=%s RETURNING id", (A,)
    ).fetchall()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("UPDATE public.doctors SET clinic_id=%s WHERE clinic_id=%s", (B, A))


@pytest.mark.parametrize("role", ["viewer", "receptionist"])
def test_non_managers_cannot_edit_configuration(db, role):
    authenticate(db, role=role)
    assert (
        db.execute(
            "UPDATE public.weekly_schedules SET status='inactive' WHERE clinic_id=%s RETURNING id",
            (A,),
        ).fetchall()
        == []
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute(
            "INSERT INTO public.doctors (clinic_id,display_name,normalized_name,speciality) "
            "VALUES (%s,'Fixture','fixture','General')",
            (A,),
        )


def test_inactive_membership_has_no_access(db):
    authenticate(db, active=False)
    assert db.execute("SELECT id FROM public.doctors").fetchall() == []


def test_no_identity_cannot_browse(db):
    db.execute("SET LOCAL ROLE authenticated")
    assert db.execute("SELECT id FROM public.doctors").fetchall() == []


def test_owner_cannot_promote_membership_or_assign_phone(db):
    authenticate(db, role="owner")
    for table in [
        "clinic_users",
        "phone_numbers",
        "configuration_versions",
        "appointment_requests",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(sql.SQL("DELETE FROM public.{}").format(sql.Identifier(table)))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("UPDATE public.clinic_users SET role='owner'")


def test_cross_tenant_foreign_keys_block_even_admin(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        db.execute(
            "INSERT INTO public.weekly_schedules (clinic_id,doctor_id,location_id,day_of_week,"
            "start_time,end_time) VALUES (%s,%s,%s,0,'09:00','10:00')",
            (A, fixture_id("B-doctor-0"), fixture_id("A-location")),
        )


@pytest.mark.parametrize(
    "number,trunk,provider",
    [
        ("+12025550199", "fixture-only-not-a-live-trunk", "test"),
        ("+12025550101", "wrong-trunk", "test"),
        ("+12025550101", "fixture-only-not-a-live-trunk", "plivo"),
        ("' OR 1=1 --", "fixture-only-not-a-live-trunk", "test"),
    ],
)
def test_invalid_destination_fails_closed_in_sql(db, number, trunk, provider):
    db.execute("SET LOCAL ROLE clinic_runtime")
    assert (
        db.execute(
            "SELECT * FROM clinic_private.resolve_destination(%s,%s,%s)", (provider, number, trunk)
        ).fetchall()
        == []
    )


@pytest.mark.parametrize("kind", ["phone", "clinic", "unpublished"])
def test_inactive_routes_fail_closed(db, kind):
    if kind == "phone":
        db.execute("UPDATE public.phone_numbers SET status='inactive' WHERE clinic_id=%s", (A,))
    elif kind == "clinic":
        db.execute("UPDATE public.clinics SET status='inactive' WHERE id=%s", (A,))
    else:
        db.execute(
            "UPDATE public.clinics SET active_configuration_version_id=NULL WHERE id=%s", (A,)
        )
    db.execute("SET LOCAL ROLE clinic_runtime")
    assert (
        db.execute(
            "SELECT * FROM clinic_private.resolve_destination('test',%s,%s)",
            ("+12025550101", "fixture-only-not-a-live-trunk"),
        ).fetchall()
        == []
    )


def test_browser_cannot_call_ingress_resolver(db):
    authenticate(db)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT * FROM clinic_private.resolve_destination('test','x','x')")


def test_runtime_role_is_limited_and_reads_only_scoped_snapshots(db):
    role = db.execute(
        "SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb FROM pg_roles "
        "WHERE rolname='clinic_runtime'"
    ).fetchone()
    assert role == (False, False, False, False)
    db.execute("SET LOCAL ROLE clinic_runtime")
    assert db.execute("SELECT id FROM public.configuration_versions").fetchall() == []
    for table in ["doctors", "phone_numbers", "clinic_users", "appointment_requests", "audit_logs"]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
            db.execute(sql.SQL("SELECT * FROM public.{}").format(sql.Identifier(table)))
    db.execute("SELECT set_config('app.clinic_id', %s, true)", (str(A),))
    assert db.execute(
        "SELECT DISTINCT clinic_id FROM public.configuration_versions"
    ).fetchall() == [(A,)]


def test_all_tenant_tables_and_private_function_privileges(db):
    authenticate(db)
    tables = db.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname"
    ).fetchall()
    for (name,) in tables:
        if name == "caller_profiles":
            # Phase 3/4 restrict even encrypted profile data; no caller reuse is enabled.
            with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
                db.execute("SELECT clinic_id FROM public.caller_profiles")
            continue
        column = "id" if name == "clinics" else "clinic_id"
        assert (
            db.execute(
                sql.SQL("SELECT {} FROM public.{} WHERE {}=%s").format(
                    sql.Identifier(column), sql.Identifier(name), sql.Identifier(column)
                ),
                (B,),
            ).fetchall()
            == []
        )
    for role in ["anon", "authenticated", "clinic_runtime"]:
        functions = db.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='clinic_private' AND has_function_privilege(%s,p.oid,'EXECUTE')",
            (role,),
        ).fetchall()
        expected = {
            "authenticated": [
                ("has_membership",),
                ("preview_configuration",),
                ("publish_configuration",),
            ],
            "clinic_runtime": [
                ("resolve_destination",),
                ("start_call",),
                ("update_call",),
                ("create_request",),
                ("record_usage",),
            ],
            "anon": [],
        }
        assert sorted(functions) == sorted(expected[role])
        for (table,) in tables:
            assert db.execute(
                "SELECT has_table_privilege('anon',%s,'SELECT,INSERT,UPDATE,DELETE')",
                (f"public.{table}",),
            ).fetchone() == (False,)


def test_old_snapshot_survives_publication_and_draft_edits(db):
    old = fixture_id("A-version")
    new = uuid4()
    original = db.execute(
        "SELECT snapshot FROM public.configuration_versions WHERE id=%s", (old,)
    ).fetchone()[0]
    db.execute("UPDATE public.configuration_versions SET status='superseded' WHERE id=%s", (old,))
    db.execute(
        "INSERT INTO public.configuration_versions "
        "(id,clinic_id,version_number,status,snapshot,prompt_version,published_at) "
        "SELECT %s,clinic_id,2,'published',snapshot,'fixture-v2',now() "
        "FROM public.configuration_versions WHERE id=%s",
        (new, old),
    )
    db.execute("UPDATE public.clinics SET active_configuration_version_id=%s WHERE id=%s", (new, A))
    db.execute(
        "UPDATE public.doctors SET display_name='Unpublished draft change' WHERE clinic_id=%s", (A,)
    )
    db.execute("SET LOCAL ROLE clinic_runtime")
    resolved = db.execute(
        "SELECT configuration_version_id FROM clinic_private.resolve_destination('test',%s,%s)",
        ("+12025550101", "fixture-only-not-a-live-trunk"),
    ).fetchone()
    assert resolved == (new,)
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(A),))
    assert (
        db.execute(
            "SELECT snapshot FROM public.configuration_versions WHERE id=%s", (old,)
        ).fetchone()[0]
        == original
    )


def test_draft_is_not_runtime_readable(db):
    draft = uuid4()
    db.execute(
        "INSERT INTO public.configuration_versions "
        "(id,clinic_id,version_number,snapshot,prompt_version) VALUES (%s,%s,9,'{}','draft')",
        (draft, A),
    )
    db.execute("SET LOCAL ROLE clinic_runtime")
    db.execute("SELECT set_config('app.clinic_id',%s,true)", (str(A),))
    assert (
        db.execute("SELECT id FROM public.configuration_versions WHERE id=%s", (draft,)).fetchall()
        == []
    )


def test_no_duplicate_active_number(db):
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute(
            "INSERT INTO public.phone_numbers "
            "(clinic_id,provider,e164_number,trusted_trunk_id,status) "
            "VALUES (%s,'test','+12025550101','other-fixture','active')",
            (B,),
        )


def test_publication_is_immutable_and_snapshots_exclude_notes(db):
    snapshot = db.execute(
        "SELECT snapshot FROM public.configuration_versions WHERE clinic_id=%s", (A,)
    ).fetchone()[0]
    assert "PRIVATE" not in json.dumps(snapshot)
    assert "internal_note" not in json.dumps(snapshot)
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            "UPDATE public.configuration_versions SET snapshot='{}' WHERE clinic_id=%s", (A,)
        )
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("DELETE FROM public.configuration_versions WHERE clinic_id=%s", (A,))


def test_audit_is_append_only(db):
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("UPDATE public.audit_logs SET action='rewritten' WHERE clinic_id=%s", (A,))


def test_fee_overlap_and_timezone_constraints(db):
    with pytest.raises(psycopg.errors.ExclusionViolation), db.transaction():
        db.execute(
            "INSERT INTO public.doctor_services (clinic_id,doctor_id,service_id,current_fee,"
            "effective_from) VALUES (%s,%s,%s,1,'2026-02-01')",
            (A, fixture_id("A-doctor-0"), fixture_id("A-service-0")),
        )
    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute("UPDATE public.clinics SET timezone='Not/AZone' WHERE id=%s", (A,))


def test_runtime_pool_resolution_pinning_and_scope_reset(runtime_settings):
    async def exercise():
        database = RuntimeDatabase(runtime_settings)
        await database.open()
        try:
            resolver = ClinicResolver(PostgresResolutionRepository(database))

            async def one(label, number):
                scope = await resolver.resolve(
                    InboundDestination(number, "fixture-only-not-a-live-trunk", "test")
                )
                assert scope.clinic_id == fixture_id(label)
                snapshot = await ConfigurationRepository(database, scope).load()
                assert all(
                    f"({label}, fictional)" in row["display_name"] for row in snapshot["doctors"]
                )
                other = "B" if label == "A" else "A"
                with pytest.raises(ClinicUnavailable):
                    await ConfigurationRepository(
                        database,
                        replace(scope, configuration_version_id=fixture_id(f"{other}-version")),
                    ).load()
                async with database.connection() as conn:
                    assert (
                        await (
                            await conn.execute("SELECT id FROM public.configuration_versions")
                        ).fetchall()
                        == []
                    )

            await asyncio.gather(
                *[
                    one(label, number)
                    for _ in range(4)
                    for label, number in [("A", "+12025550101"), ("B", "+12025550102")]
                ]
            )
            with pytest.raises(ClinicUnavailable):
                await resolver.resolve(InboundDestination("+12025550199", "unknown", "test"))
        finally:
            await database.close()

    asyncio.run(exercise())


def test_runtime_rejects_privileged_database_url(database_settings):
    async def exercise():
        database = RuntimeDatabase(database_settings)
        try:
            with pytest.raises(ConfigurationError):
                await database.open()
        finally:
            await database.close()

    asyncio.run(exercise())


def test_staff_repository_authorizes_each_operation(database_settings):
    async def exercise():
        async with (
            await psycopg.AsyncConnection.connect(
                database_settings.dsn, row_factory=dict_row, connect_timeout=10
            ) as conn,
            conn.transaction(force_rollback=True),
        ):
            user = uuid4()
            await conn.execute("INSERT INTO auth.users (id) VALUES (%s)", (user,))
            await conn.execute(
                "INSERT INTO public.clinic_users (clinic_id,auth_user_id,role) "
                "VALUES (%s,%s,'manager')",
                (A, user),
            )
            await conn.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (str(user),))
            await conn.execute("SET LOCAL ROLE authenticated")
            assert len(await StaffRepository(conn, A).list_doctors()) == 2
            with pytest.raises(Forbidden):
                await StaffRepository(conn, B).list_doctors()
            with pytest.raises(Forbidden):
                await StaffRepository(conn, B).deactivate_schedule(fixture_id("B-schedule-0"))
            assert not await StaffRepository(conn, A).deactivate_schedule(
                fixture_id("B-schedule-0")
            )

    asyncio.run(exercise())

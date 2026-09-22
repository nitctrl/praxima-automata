"""Rollback-only actual database publication, RLS, tools and immutable history tests."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from clinic.development import fixture_id
from clinic.publication import ClinicConfigurationService
from clinic.resolver import ClinicScope, ConfigurationRepository
from clinic.snapshot import Snapshot
from clinic.staff import Forbidden
from clinic.tools import ClinicTools

pytestmark = pytest.mark.integration
A, B = fixture_id("A"), fixture_id("B")


@asynccontextmanager
async def staff_connection(settings, role="manager"):
    async with (
        await psycopg.AsyncConnection.connect(
            settings.dsn, row_factory=dict_row, autocommit=True, connect_timeout=10
        ) as conn,
        conn.transaction(force_rollback=True),
    ):
        await conn.execute("SET LOCAL statement_timeout='10s'")
        user = uuid4()
        await conn.execute("INSERT INTO auth.users(id) VALUES (%s)", (user,))
        await conn.execute(
            "INSERT INTO public.clinic_users(clinic_id,auth_user_id,role) VALUES (%s,%s,%s)",
            (A, user, role),
        )
        await conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(user),))
        await conn.execute("SET LOCAL ROLE authenticated")
        yield conn


def test_preview_publish_rollback_and_public_allowlists(database_settings):
    async def exercise():
        async with staff_connection(database_settings) as conn:
            service = ClinicConfigurationService(conn, A)
            before = await service.preview()
            assert "PRIVATE" not in before.snapshot.model_dump_json()
            assert "internal_note" not in before.snapshot.model_dump_json()
            assert "storage_path" not in before.snapshot.model_dump_json()
            assert before.active_version == fixture_id("A-version")
            first = await service.publish(before)
            await conn.execute(
                "UPDATE public.doctors SET display_name='Draft doctor change' WHERE id=%s",
                (fixture_id("A-doctor-0"),),
            )
            second = await service.publish(await service.preview())
            rolled = await service.rollback(await service.preview(first))
            assert len({first, second, rolled}) == 3
            row = await (
                await conn.execute(
                    "SELECT snapshot,source_version_id "
                    "FROM public.configuration_versions WHERE id=%s",
                    (rolled,),
                )
            ).fetchone()
            assert Snapshot.model_validate(row["snapshot"]) == before.snapshot
            assert row["source_version_id"] == first
            audit = await (
                await conn.execute(
                    "SELECT action FROM public.audit_logs WHERE resource_id=%s",
                    (rolled,),
                )
            ).fetchone()
            assert audit["action"] == "configuration_rollback_published"

    asyncio.run(exercise())


@pytest.mark.parametrize("role", ["viewer", "receptionist"])
def test_publication_denied_in_service_and_sql(database_settings, role):
    async def exercise():
        async with staff_connection(database_settings, role) as conn:
            with pytest.raises(Forbidden):
                await ClinicConfigurationService(conn, A).preview()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT * FROM clinic_private.preview_configuration(%s,NULL)", (A,)
                    )

    asyncio.run(exercise())


def test_cross_clinic_publication_denied(database_settings):
    async def exercise():
        async with staff_connection(database_settings) as conn:
            with pytest.raises(Forbidden):
                await ClinicConfigurationService(conn, B).preview()
            async with conn.transaction():
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT * FROM clinic_private.publish_configuration(%s,'x',NULL,NULL)",
                            (B,),
                        )

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["authoring", "active"])
def test_stale_preview_rejected_without_partial_publication(database_settings, change):
    async def exercise():
        async with staff_connection(database_settings) as conn:
            service = ClinicConfigurationService(conn, A)
            preview = await service.preview()
            if change == "authoring":
                await conn.execute(
                    "UPDATE public.doctors SET display_name='Changed draft' WHERE id=%s",
                    (fixture_id("A-doctor-0"),),
                )
            else:
                await service.publish(preview)
            old_count = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM public.configuration_versions WHERE clinic_id=%s",
                    (A,),
                )
            ).fetchone()
            with pytest.raises(psycopg.errors.SerializationFailure):
                await service.publish(preview)
            new_count = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM public.configuration_versions WHERE clinic_id=%s",
                    (A,),
                )
            ).fetchone()
            assert old_count == new_count

    asyncio.run(exercise())


@pytest.mark.parametrize("invalid", ["hours", "reference", "date_conflict", "notice"])
def test_database_publication_validation(database_settings, invalid):
    async def exercise():
        async with staff_connection(database_settings) as conn:
            if invalid == "hours":
                await conn.execute(
                    "UPDATE public.weekly_schedules SET status='inactive' "
                    "WHERE clinic_id=%s AND doctor_id IS NULL",
                    (A,),
                )
            elif invalid == "reference":
                await conn.execute(
                    "UPDATE public.doctors SET status='inactive' WHERE id=%s",
                    (fixture_id("A-doctor-0"),),
                )
            elif invalid == "notice":
                await conn.execute(
                    "UPDATE public.temporary_notices SET notice_type='unknown' WHERE clinic_id=%s",
                    (A,),
                )
            else:
                await conn.execute(
                    "INSERT INTO public.schedule_exceptions "
                    "(clinic_id,doctor_id,location_id,exception_date,status,"
                    "public_message,publication_status) "
                    "SELECT clinic_id,doctor_id,location_id,exception_date,status,"
                    "public_message,'published' "
                    "FROM public.schedule_exceptions WHERE clinic_id=%s",
                    (A,),
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                await ClinicConfigurationService(conn, A).preview()

    asyncio.run(exercise())


def test_draft_faq_and_notice_excluded(database_settings):
    async def exercise():
        async with staff_connection(database_settings) as conn:
            await conn.execute(
                "UPDATE public.approved_faqs SET publication_status='draft' WHERE clinic_id=%s",
                (A,),
            )
            await conn.execute(
                "UPDATE public.temporary_notices SET publication_status='draft' WHERE clinic_id=%s",
                (A,),
            )
            preview = await ClinicConfigurationService(conn, A).preview()
            assert not preview.snapshot.approved_faqs
            assert not preview.snapshot.temporary_notices

    asyncio.run(exercise())


def test_tool_reads_pinned_sql_snapshot_after_new_publication(database_settings):
    async def exercise():
        async with staff_connection(database_settings) as conn:
            service = ClinicConfigurationService(conn, A)
            old = await service.publish(await service.preview())
            await conn.execute(
                "UPDATE public.doctors SET display_name='New draft name' WHERE id=%s",
                (fixture_id("A-doctor-0"),),
            )
            await service.publish(await service.preview())
            await conn.execute("SET LOCAL ROLE clinic_runtime")

            # Same transaction allows rollback-only fixtures. Every repository SELECT
            # executes under actual clinic_runtime RLS, not the migration owner.
            class BoundDatabase:
                @asynccontextmanager
                async def connection(self, clinic_id):
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT set_config('app.clinic_id',%s,true)", (str(clinic_id),)
                        )
                        yield conn

            scope = ClinicScope(A, fixture_id("A-phone"), old, "Asia/Kolkata", ("hi-IN", "en-IN"))
            repository = ConfigurationRepository(BoundDatabase(), scope)
            tools = ClinicTools(repository, scope, clock=lambda: datetime.now(timezone.utc))
            result = await tools.find_doctors(name="Anaya")
            assert result["status"] == "success"
            assert "New draft" not in json.dumps(result)
            assert "PRIVATE" not in json.dumps(result)
            fee = await tools.get_consultation_fee(doctor="Anaya", service="Consultation")
            assert fee["status"] == "success" and float(fee["data"]["amount"]) == 450
            foreign = replace(scope, configuration_version_id=fixture_id("B-version"))
            failed = await ClinicTools(
                ConfigurationRepository(BoundDatabase(), foreign), foreign
            ).find_doctors()
            assert failed["status"] == "unavailable"

    asyncio.run(exercise())


def test_publication_lock_serializes_other_publishers_and_authoring(database_settings):
    async def exercise():
        # Different temporary principals can see their own uncommitted memberships.
        # Both connections roll back all users/publications: no committed test cleanup.
        async with staff_connection(database_settings) as first:
            async with staff_connection(database_settings) as second:
                first_service = ClinicConfigurationService(first, A)
                second_service = ClinicConfigurationService(second, A)
                first_preview = await first_service.preview()
                second_preview = await second_service.preview()
                await first_service.publish(first_preview)
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    await second_service.publish(second_preview)
                await second.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    async with second.transaction():
                        await second.execute(
                            "UPDATE public.doctors SET display_name='Concurrent draft edit' "
                            "WHERE id=%s",
                            (fixture_id("A-doctor-0"),),
                        )
        # Above transactions exit in reverse order; neither changes live fixtures.

    asyncio.run(exercise())


def test_rollback_rejects_foreign_and_legacy_source(database_settings):
    async def exercise():
        async with staff_connection(database_settings) as conn:
            service = ClinicConfigurationService(conn, A)
            for source in [fixture_id("B-version"), fixture_id("A-version")]:
                with pytest.raises(psycopg.errors.CheckViolation):
                    await service.preview(source)

    asyncio.run(exercise())

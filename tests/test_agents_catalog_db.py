"""Step 2 against a real Postgres: pack install, entities, relations, availability, agents.

Runs only when PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

import uuid
from datetime import date, time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support.queries import count_queries
from test_iam_tenancy_db import (  # noqa: F401
    URL,
    Sessions,
    admin_id,
    identity,
    login,
    migrated,
    onboard,
    run,
)

from praxima.modules import agents, catalog, iam
from praxima.shared.db.engine import Scope, scoped_transaction
from praxima.shared.db.pagination import PageRequest
from praxima.shared.errors import Conflict, NotFound, PermissionDenied, ValidationFailed

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
DOCTOR = {"specialization": "Cardiology", "experience_years": 15}


async def workspace(
    sessions: Sessions, admin: uuid.UUID, slug: str
) -> tuple[Scope, iam.Actor, uuid.UUID]:
    """An organization + workspace with the clinic pack installed; returns the owner's scope."""
    owner = await login(sessions, identity(f"sub-{slug}", f"owner@{slug}.test"))
    org, ws = await onboard(sessions, admin, slug, owner.user_id)
    scope = Scope(user_id=owner.user_id, organization_id=org, workspace_id=ws)
    actor = iam.Actor(owner.user_id, "owner")
    async with scoped_transaction(sessions, scope) as session:
        await catalog.install_pack(session, actor, ws)
    return scope, actor, ws


def draft(type_key: str, key: str, name: str, **attributes: object) -> catalog.EntityDraft:
    return catalog.EntityDraft(type_key, key, name, attributes=dict(attributes))


def test_pack_install_is_idempotent(admin_id):  # noqa: F811
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            assert await catalog.install_pack(session, actor, ws) == []
            types = await catalog.entity_types(session)
        assert [t.key for t in types] == ["doctor", "location", "service"]
        assert types[0].attributes_schema["required"] == ["specialization"]

    run(scenario)


def test_entities_validate_version_search_and_delete(admin_id):  # noqa: F811
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            doctor = await catalog.create_entity(
                session,
                actor,
                workspace_id=ws,
                draft=catalog.EntityDraft(
                    "doctor", "dr-sharma", "Dr. Asha Sharma", ("Sharma ji",), DOCTOR
                ),
            )
            with pytest.raises(ValidationFailed) as invalid:
                await catalog.create_entity(
                    session,
                    actor,
                    workspace_id=ws,
                    draft=draft("doctor", "dr-x", "Dr X", experience_years="many"),
                )
            assert {e.field for e in invalid.value.errors} == {
                "attributes.experience_years",
                "attributes.specialization",
            }
            with pytest.raises(ValidationFailed):
                await catalog.create_entity(
                    session, actor, workspace_id=ws, draft=draft("nurse", "n-1", "N")
                )
            with pytest.raises(PermissionDenied):
                await catalog.create_entity(
                    session,
                    iam.Actor(actor.user_id, "viewer"),
                    workspace_id=ws,
                    draft=draft("service", "s-1", "S"),
                )

        async with scoped_transaction(sessions, scope) as session:
            with pytest.raises(Conflict):
                await catalog.create_entity(
                    session,
                    actor,
                    workspace_id=ws,
                    draft=draft("doctor", "dr-sharma", "Dup", specialization="ENT"),
                )

        async with scoped_transaction(sessions, scope) as session:
            for query in ("asha", "SHARMA JI"):
                found = await catalog.entities_page(session, PageRequest(), search=query)
                assert [e.id for e in found.items] == [doctor]
            change = catalog.EntityChanges(attributes={**DOCTOR, "qualification": "MD"})
            await catalog.update_entity(
                session, actor, entity_id=doctor, row_version=1, changes=change
            )
            with pytest.raises(Conflict):
                await catalog.update_entity(
                    session, actor, entity_id=doctor, row_version=1, changes=change
                )
            await catalog.set_entity_status(
                session, actor, entity_id=doctor, row_version=2, status="published"
            )
            view = await catalog.get_entity(session, doctor)
            assert (view.type, view.publication_status, view.row_version) == (
                "doctor",
                "published",
                3,
            )
            await catalog.delete_entity(session, actor, entity_id=doctor, row_version=3)
            with pytest.raises(NotFound):
                await catalog.get_entity(session, doctor)
            # A deleted key can be reused.
            await catalog.create_entity(
                session,
                actor,
                workspace_id=ws,
                draft=draft("doctor", "dr-sharma", "Dr. New", specialization="ENT"),
            )

    run(scenario)


def test_entity_pages_take_one_query(admin_id):  # noqa: F811
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            for n in range(5):
                await catalog.create_entity(
                    session,
                    actor,
                    workspace_id=ws,
                    draft=draft("service", f"s-{n}", f"Service {n}", fee=100 + n),
                )
        async with scoped_transaction(sessions, scope) as session:
            cursor, seen = None, []
            for _ in range(3):
                with count_queries(engine) as statements:
                    page = await catalog.entities_page(
                        session, PageRequest(2, cursor), type_key="service"
                    )
                    seen += [(e.type, e.name) for e in page.items]
                assert len(statements) == 1, statements
                cursor = page.next_cursor
        assert cursor is None and len(seen) == 5 and {t for t, _ in seen} == {"service"}

    run(scenario)


def test_catalog_is_isolated_between_workspaces(admin_id):  # noqa: F811
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope_a, actor_a, ws_a = await workspace(sessions, admin_id, "one")
        scope_b, _, ws_b = await workspace(sessions, admin_id, "two")
        async with scoped_transaction(sessions, scope_a) as session:
            doctor_a = await catalog.create_entity(
                session,
                actor_a,
                workspace_id=ws_a,
                draft=draft("doctor", "dr-a", "Dr A", specialization="ENT"),
            )
        async with scoped_transaction(sessions, scope_b) as session:
            with pytest.raises(NotFound):
                await catalog.get_entity(session, doctor_a)
            assert (await catalog.entities_page(session, PageRequest())).items == []
        # Even raw SQL can't write into another workspace (RLS) ...
        with pytest.raises(Exception, match="row-level security"):
            async with scoped_transaction(sessions, scope_b) as session:
                await session.execute(
                    text(
                        "UPDATE catalog.entities SET workspace_id = :b WHERE workspace_id = :a "
                        "RETURNING id"
                    ),
                    {"a": ws_a, "b": ws_b},
                )
                await session.execute(
                    text(
                        "INSERT INTO catalog.entity_types (id, workspace_id, key, name, "
                        "schema_version, attributes_schema) "
                        "VALUES (:id, :a, 'x_type', 'X', 1, '{}')"
                    ),
                    {"id": uuid.uuid4(), "a": ws_a},
                )
        # ... or reference another workspace's row (composite tenant foreign key).
        with pytest.raises(Exception, match="foreign key"):
            async with scoped_transaction(sessions, scope_b) as session:
                await session.execute(
                    text(
                        "INSERT INTO catalog.entity_relations (id, workspace_id, from_entity_id, "
                        "to_entity_id, relation_type) VALUES (:id, :b, :doc, :doc2, 'x')"
                    ),
                    {"id": uuid.uuid4(), "b": ws_b, "doc": doctor_a, "doc2": uuid.uuid4()},
                )

    run(scenario)


def test_relations_follow_the_pack(admin_id):  # noqa: F811
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            doctor = await catalog.create_entity(
                session,
                actor,
                workspace_id=ws,
                draft=draft("doctor", "dr-a", "Dr A", specialization="ENT"),
            )
            consult = await catalog.create_entity(
                session, actor, workspace_id=ws, draft=draft("service", "consult", "Consult")
            )
            link = await catalog.create_relation(
                session,
                actor,
                workspace_id=ws,
                relation_type="doctor_offers_service",
                from_entity_id=doctor,
                to_entity_id=consult,
                attributes={"fee": 800, "currency": "INR"},
            )
            for bad in (
                {"from_entity_id": consult, "to_entity_id": doctor, "attributes": None},
                {"from_entity_id": doctor, "to_entity_id": consult, "attributes": {"fee": -1}},
            ):
                with pytest.raises(ValidationFailed):
                    await catalog.create_relation(
                        session,
                        actor,
                        workspace_id=ws,
                        relation_type="doctor_offers_service",
                        **bad,  # type: ignore[arg-type]
                    )
        with pytest.raises(Conflict):  # overlapping duplicate (exclusion constraint)
            async with scoped_transaction(sessions, scope) as session:
                await catalog.create_relation(
                    session,
                    actor,
                    workspace_id=ws,
                    relation_type="doctor_offers_service",
                    from_entity_id=doctor,
                    to_entity_id=consult,
                )
        async with scoped_transaction(sessions, scope) as session:
            await catalog.set_detail_status(
                session, actor, kind="relation", detail_id=link, status="published"
            )
            await session.flush()  # write the pending audit entry before counting reads
            with count_queries(engine) as statements:
                from_doctor = await catalog.relations_of(session, doctor)
                from_service = await catalog.relations_of(session, consult)
            assert len(statements) == 2
        assert [(r.direction, r.other_name, r.attributes["fee"]) for r in from_doctor] == [
            ("outgoing", "Consult", 800)
        ]
        assert from_service[0].direction == "incoming" and from_service[0].other_type == "doctor"
        assert from_doctor[0].publication_status == "published"

    run(scenario)


def test_availability_rules_and_exceptions(admin_id):  # noqa: F811
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            doctor = await catalog.create_entity(
                session,
                actor,
                workspace_id=ws,
                draft=draft("doctor", "dr-a", "Dr A", specialization="ENT"),
            )
            service = await catalog.create_entity(
                session, actor, workspace_id=ws, draft=draft("service", "consult", "Consult")
            )

            def rule(**changes: object) -> catalog.AvailabilityRuleDraft:
                values: dict[str, object] = {
                    "timezone": "Asia/Kolkata",
                    "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,TH",
                    "start_time": time(9),
                    "end_time": time(13),
                    "entity_id": doctor,
                }
                merged = {**values, **changes}
                return catalog.AvailabilityRuleDraft(**merged)  # type: ignore[arg-type]

            await catalog.add_availability_rule(session, actor, workspace_id=ws, draft=rule())
            for bad in (
                rule(rrule="EVERY MONDAY"),
                rule(end_time=time(8)),
                rule(timezone="Moon/Base"),
                rule(entity_id=service),  # services have no hours in the clinic pack
            ):
                with pytest.raises(ValidationFailed):
                    await catalog.add_availability_rule(session, actor, workspace_id=ws, draft=bad)
            await catalog.add_availability_exception(
                session,
                actor,
                workspace_id=ws,
                draft=catalog.AvailabilityExceptionDraft(
                    "Asia/Kolkata",
                    date(2026, 10, 20),
                    False,
                    entity_id=doctor,
                    public_message="On leave for Diwali.",
                ),
            )
            rules, exceptions = await catalog.availability_of(session, doctor)
        assert [(r.rrule, r.start_time, r.end_time) for r in rules] == [
            ("FREQ=WEEKLY;BYDAY=MO,TU,TH", time(9), time(13))
        ]
        assert [(e.exception_date, e.is_available) for e in exceptions] == [
            (date(2026, 10, 20), False)
        ]

    run(scenario)


def test_agents_tools_and_phone_number_routing(admin_id):  # noqa: F811
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope_a, actor_a, ws_a = await workspace(sessions, admin_id, "one")
        scope_b, actor_b, ws_b = await workspace(sessions, admin_id, "two")
        messages = {
            "greeting_message": "Namaste, how can I help?",
            "emergency_message": "Please call 112 now.",
            "fallback_message": "Our staff will call you back.",
        }
        number = "+919876543210"

        async with scoped_transaction(sessions, scope_a) as session:
            agent_a = await agents.create_agent(
                session,
                actor_a,
                workspace_id=ws_a,
                draft=agents.AgentDraft("Reception", "reception", **messages),
            )
            await agents.set_tool(
                session, actor_a, agent_id=agent_a, tool_key="request_callback", enabled=False
            )
            with pytest.raises(ValidationFailed):
                await agents.set_tool(
                    session, actor_a, agent_id=agent_a, tool_key="book_appointment", enabled=True
                )
            with pytest.raises(ValidationFailed):
                await agents.assign_phone_number(
                    session,
                    actor_a,
                    agent_id=agent_a,
                    phone_number="98765 43210",
                    provider="plivo",
                )
            with pytest.raises(PermissionDenied):
                await agents.assign_phone_number(
                    session,
                    iam.Actor(actor_a.user_id, "manager"),
                    agent_id=agent_a,
                    phone_number=number,
                    provider="plivo",
                )
            number_a = await agents.assign_phone_number(
                session, actor_a, agent_id=agent_a, phone_number=number, provider="plivo"
            )
            view = await agents.get_agent(session, agent_a)
        tools = {t.key: t.enabled for t in view.tools}
        assert set(tools) == {
            "find_entities",
            "get_entity",
            "get_availability",
            "search_knowledge",
            "get_announcements",
            "create_work_item",
            "request_callback",
        }
        assert tools["request_callback"] is False and tools["find_entities"] is True
        assert [n.phone_number for n in view.phone_numbers] == [number]

        async with scoped_transaction(sessions, scope_b) as session:
            agent_b = await agents.create_agent(
                session,
                actor_b,
                workspace_id=ws_b,
                draft=agents.AgentDraft("Desk", "desk", **messages),
            )
        with pytest.raises(Conflict):  # the number already routes to workspace A
            async with scoped_transaction(sessions, scope_b) as session:
                await agents.assign_phone_number(
                    session, actor_b, agent_id=agent_b, phone_number=number, provider="plivo"
                )

        # Ingress: only the exact dialled number is visible, and only with that scope.
        async with scoped_transaction(sessions, Scope(called_number=number)) as session:
            target = await agents.resolve_ingress(session, number)
        assert target is not None and (target.agent_id, target.workspace_id) == (agent_a, ws_a)
        async with scoped_transaction(sessions, Scope()) as session:
            assert await agents.resolve_ingress(session, number) is None
        async with scoped_transaction(sessions, Scope(called_number="+911111111111")) as session:
            assert await agents.resolve_ingress(session, number) is None

        async with scoped_transaction(sessions, scope_a) as session:
            await agents.release_phone_number(session, actor_a, phone_number_id=number_a)
        async with scoped_transaction(sessions, Scope(called_number=number)) as session:
            assert await agents.resolve_ingress(session, number) is None
        async with scoped_transaction(sessions, scope_b) as session:  # released: reusable
            await agents.assign_phone_number(
                session, actor_b, agent_id=agent_b, phone_number=number, provider="plivo"
            )

    run(scenario)

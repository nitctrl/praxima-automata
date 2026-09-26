"""Step 5 against a real Postgres: contacts, consents, work items, tasks, conversations.

Runs only when PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (admin_id) are not redefinitions.
# ruff: noqa: F811
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from support.queries import count_queries
from test_agents_catalog_db import draft, workspace
from test_engagement_pure import make_vault
from test_iam_tenancy_db import (  # noqa: F401
    URL,
    Sessions,
    admin_id,
    identity,
    login,
    migrated,
    run,
)

from praxima.modules import agents, catalog, engagement, iam
from praxima.modules.engagement.infrastructure.models import CallEvent, Conversation
from praxima.shared.db.engine import scoped_transaction
from praxima.shared.db.pagination import PageRequest
from praxima.shared.errors import Conflict, PermissionDenied, ValidationFailed

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")
PHONE = "+919876543210"


async def crm_workspace(sessions: Sessions, admin: uuid.UUID, slug: str):  # type: ignore[no-untyped-def]
    scope, actor, ws = await workspace(sessions, admin, slug)
    async with scoped_transaction(sessions, scope) as session:
        await engagement.install_work_item_kinds(session, actor, ws)
    return scope, actor, ws


def test_work_item_kinds_install_from_the_pack(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await crm_workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            assert await engagement.install_work_item_kinds(session, actor, ws) == []
            kinds = await engagement.work_item_kinds(session)
        assert [(k.key, k.initial_stage) for k in kinds] == [
            ("appointment_request", "new"),
            ("callback_request", "new"),
        ]

    run(scenario)


def test_contacts_are_encrypted_revealed_with_audit_and_erasable(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        vault = make_vault()
        scope, actor, ws = await crm_workspace(sessions, admin_id, "one")
        scope_b, actor_b, ws_b = await crm_workspace(sessions, admin_id, "two")
        new = engagement.ContactDraft("Asha Verma", PHONE, "hi-IN")
        async with scoped_transaction(sessions, scope) as session:
            contact = await engagement.create_contact(
                session, actor, vault, workspace_id=ws, draft=new
            )
            raw = (
                await session.execute(
                    text(
                        "SELECT display_name_ciphertext, phone_ciphertext FROM engagement.contacts"
                    )
                )
            ).one()
            assert b"Asha" not in raw[0] and b"9876543210" not in raw[1]  # never in clear
            with pytest.raises(ValidationFailed):
                await engagement.create_contact(
                    session,
                    actor,
                    vault,
                    workspace_id=ws,
                    draft=engagement.ContactDraft(phone="98765 43210"),
                )
        with pytest.raises(Conflict):  # one contact per phone per workspace
            async with scoped_transaction(sessions, scope) as session:
                await engagement.create_contact(session, actor, vault, workspace_id=ws, draft=new)
        async with scoped_transaction(sessions, scope_b) as session:  # other tenant: fine
            await engagement.create_contact(session, actor_b, vault, workspace_id=ws_b, draft=new)

        async with scoped_transaction(sessions, scope) as session:
            found = await engagement.find_contact_by_phone(session, vault, ws, PHONE)
            assert found is not None and found.id == contact and found.has_personal_details
            with pytest.raises(PermissionDenied):
                await engagement.reveal_contact(
                    session, iam.Actor(actor.user_id, "viewer"), vault, contact_id=contact
                )
            revealed = await engagement.reveal_contact(
                session, iam.Actor(actor.user_id, "staff"), vault, contact_id=contact
            )
            assert (revealed.display_name, revealed.phone) == ("Asha Verma", PHONE)
            await session.flush()
            reveals = await session.scalar(
                text("SELECT count(*) FROM audit.audit_log WHERE action = 'pii.reveal'")
            )
            assert reveals == 1
            with pytest.raises(PermissionDenied):
                await engagement.erase_contact(
                    session, iam.Actor(actor.user_id, "manager"), contact_id=contact
                )
            await engagement.erase_contact(session, actor, contact_id=contact)
            erased = await engagement.reveal_contact(session, actor, vault, contact_id=contact)
            assert erased.erased and erased.phone is None
            assert await engagement.find_contact_by_phone(session, vault, ws, PHONE) is None

    run(scenario)


def test_work_item_lifecycle(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        vault = make_vault()
        scope, actor, ws = await crm_workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            doctor = await catalog.create_entity(
                session,
                actor,
                workspace_id=ws,
                draft=draft("doctor", "dr-a", "Dr A", specialization="ENT"),
            )
            place = await catalog.create_entity(
                session,
                actor,
                workspace_id=ws,
                draft=draft("location", "main", "Main", address="1 Road", city="Patna"),
            )
            request = engagement.WorkItemDraft(
                kind="appointment_request",
                idempotency_key="call-123:appointment",
                payload={"preferred_date": "2026-10-20", "is_new_patient": True},
                entity_id=doctor,
                subject_name="Ravi Kumar",
                callback_number=PHONE,
            )
            item = await engagement.create_work_item(
                session, actor, vault, workspace_id=ws, draft=request
            )
            again = await engagement.create_work_item(  # retried call: same item
                session, actor, vault, workspace_id=ws, draft=request
            )
            assert again == item
            for bad, field in (
                ({"payload": {"notes": "chest pain"}}, "payload.notes"),  # no free text
                ({"entity_id": place}, "entity_id"),  # appointments are about doctors/services
                ({"kind": "pizza_order"}, "kind"),
            ):
                changed = engagement.WorkItemDraft(
                    **{**request.__dict__, "idempotency_key": f"k-{field}", **bad}
                )
                with pytest.raises(ValidationFailed) as error:
                    await engagement.create_work_item(
                        session, actor, vault, workspace_id=ws, draft=changed
                    )
                assert field in {e.field for e in error.value.errors}

            await engagement.move_work_item(
                session, actor, work_item_id=item, row_version=1, stage="contacted"
            )
            with pytest.raises(Conflict):  # stale
                await engagement.move_work_item(
                    session, actor, work_item_id=item, row_version=1, stage="closed"
                )
            await engagement.move_work_item(
                session, actor, work_item_id=item, row_version=2, stage="closed"
            )
            with pytest.raises(Conflict, match="closed"):
                await engagement.move_work_item(
                    session, actor, work_item_id=item, row_version=3, stage="new"
                )
            view, history = await engagement.get_work_item(session, item)
        assert (view.kind, view.stage, view.open, view.has_personal_details) == (
            "appointment_request",
            "closed",
            False,
            True,
        )
        assert [(e.from_stage, e.to_stage) for e in history] == [
            (None, "new"),
            ("new", "contacted"),
            ("contacted", "closed"),
        ]

        async with scoped_transaction(sessions, scope) as session:
            revealed = await engagement.reveal_work_item(session, actor, vault, work_item_id=item)
            assert (revealed.subject_name, revealed.callback_number) == ("Ravi Kumar", PHONE)
            await engagement.update_work_item(
                session,
                actor,
                vault,
                work_item_id=item,
                row_version=3,
                changes=engagement.WorkItemChanges(staff_note="Called back, confirmed Tue."),
            )
            note = await engagement.reveal_work_item(session, actor, vault, work_item_id=item)
            assert note.staff_note == "Called back, confirmed Tue."

    run(scenario)


def test_assignment_filters_and_list_cost(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        vault = make_vault()
        scope, actor, ws = await crm_workspace(sessions, admin_id, "one")
        outsider = await login(sessions, identity("sub-x", "x@elsewhere.test"))

        async def add(n: int) -> uuid.UUID:
            async with scoped_transaction(sessions, scope) as session:
                return await engagement.create_work_item(
                    session,
                    actor,
                    vault,
                    workspace_id=ws,
                    draft=engagement.WorkItemDraft(
                        "callback_request",
                        f"key-callback-{n}",
                        {"reason_category": "fees"},
                    ),
                )

        first = await add(0)
        async with scoped_transaction(sessions, scope) as session:
            with pytest.raises(ValidationFailed):  # not a member of this workspace
                await engagement.assign_work_item(
                    session,
                    actor,
                    work_item_id=first,
                    row_version=1,
                    assignee_user_id=outsider.user_id,
                )
            with pytest.raises(PermissionDenied):  # staff can't assign
                await engagement.assign_work_item(
                    session,
                    iam.Actor(actor.user_id, "staff"),
                    work_item_id=first,
                    row_version=1,
                    assignee_user_id=actor.user_id,
                )
            await engagement.assign_work_item(
                session, actor, work_item_id=first, row_version=1, assignee_user_id=actor.user_id
            )
            await session.flush()
            with count_queries(engine) as one:
                await engagement.work_items_page(session, PageRequest(), open_only=True)
        for n in range(1, 5):
            await add(n)
        async with scoped_transaction(sessions, scope) as session:
            await engagement.move_work_item(
                session, actor, work_item_id=first, row_version=2, stage="closed"
            )
            await session.flush()
            with count_queries(engine) as five:
                open_items = await engagement.work_items_page(
                    session, PageRequest(), open_only=True
                )
            mine = await engagement.work_items_page(
                session, PageRequest(), assignee_user_id=actor.user_id
            )
        assert len(one) == len(five) == 1
        assert len(open_items.items) == 4 and first not in [i.id for i in open_items.items]
        assert [i.id for i in mine.items] == [first]

    run(scenario)


def test_history_is_append_only(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        vault = make_vault()
        scope, actor, ws = await crm_workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            contact = await engagement.create_contact(
                session, actor, vault, workspace_id=ws, draft=engagement.ContactDraft(phone=PHONE)
            )
            await engagement.record_consent(
                session,
                actor,
                workspace_id=ws,
                contact_id=contact,
                consent_type="profile_reuse",
                action="granted",
                notice_version="2026-09",
                source="voice",
            )
            assert (await engagement.get_contact(session, contact)).consent_status == "granted"
            for table in ("consents", "work_item_events"):
                changed = await session.execute(
                    text(f"UPDATE engagement.{table} SET workspace_id = workspace_id")
                )
                deleted = await session.execute(text(f"DELETE FROM engagement.{table}"))
                assert changed.rowcount == 0 and deleted.rowcount == 0  # type: ignore[attr-defined]
            assert await session.scalar(text("SELECT count(*) FROM engagement.consents")) == 1

    run(scenario)


def test_conversations_timeline_review_filter_and_isolation(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await crm_workspace(sessions, admin_id, "one")
        scope_b, _, _ = await crm_workspace(sessions, admin_id, "two")
        messages = {
            "greeting_message": "Namaste.",
            "emergency_message": "Call 112.",
            "fallback_message": "We will call you back.",
        }
        async with scoped_transaction(sessions, scope) as session:
            agent = await agents.create_agent(
                session,
                actor,
                workspace_id=ws,
                draft=agents.AgentDraft("Desk", "desk", **messages),
            )
            # The voice runtime will write these (a later step); seeded directly here.
            ok = Conversation(
                workspace_id=ws,
                agent_id=agent,
                provider="plivo",
                provider_call_id="c-1",
                status="completed",
            )
            failed = Conversation(
                workspace_id=ws,
                agent_id=agent,
                provider="plivo",
                provider_call_id="c-2",
                status="failed",
                failure_code="tool_timeout",
            )
            test = Conversation(
                workspace_id=ws,
                agent_id=agent,
                provider="plivo",
                provider_call_id="c-3",
                status="failed",
                failure_code="x",
                is_test=True,
            )
            session.add_all([ok, failed, test])
            await session.flush()
            session.add_all(
                CallEvent(
                    workspace_id=ws, conversation_id=failed.id, event_key=key, event_type=kind
                )
                for key, kind in (("e1", "started"), ("e2", "tool_failed"))
            )
            await session.flush()
            review = await engagement.conversations_page(session, PageRequest(), needs_review=True)
            everything = await engagement.conversations_page(
                session, PageRequest(), include_tests=True
            )
            _, timeline = await engagement.get_conversation(session, failed.id)
        assert [c.id for c in review.items] == [failed.id]
        assert len(everything.items) == 3
        assert [e.event_type for e in timeline] == ["started", "tool_failed"]
        async with scoped_transaction(sessions, scope_b) as session:
            assert (await engagement.conversations_page(session, PageRequest())).items == []
            events = await session.scalar(text("SELECT count(*) FROM engagement.call_events"))
            assert events == 0

    run(scenario)


def test_tasks(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await crm_workspace(sessions, admin_id, "one")
        async with scoped_transaction(sessions, scope) as session:
            task = await engagement.create_task(
                session,
                actor,
                workspace_id=ws,
                title="Call Ravi back",
                assignee_user_id=actor.user_id,
            )
            await engagement.set_task_status(
                session, actor, task_id=task, row_version=1, status="done"
            )
            with pytest.raises(Conflict):
                await engagement.set_task_status(
                    session, actor, task_id=task, row_version=1, status="open"
                )
            view = await engagement.get_task(session, task)
            open_tasks = await engagement.tasks_page(session, PageRequest(), status="open")
        assert view.status == "done" and view.completed_at is not None
        assert open_tasks.items == []

    run(scenario)

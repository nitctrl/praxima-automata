"""Step 3 against a real Postgres: documents, review, publish, search, FAQs, announcements.

Runs only when PRAXIMA_TEST_DATABASE_URL points at a disposable database.
"""

# Test parameters named after imported pytest fixtures (admin_id) are not redefinitions.
# ruff: noqa: F811
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_agents_catalog_db import workspace
from test_iam_tenancy_db import URL, Sessions, admin_id, migrated, run  # noqa: F401

from praxima.modules import iam, knowledge
from praxima.modules.knowledge.application.ports import IndexedChunk
from praxima.shared.db.engine import scoped_transaction
from praxima.shared.db.pagination import PageRequest
from praxima.shared.errors import Conflict, PermissionDenied, ValidationFailed

pytestmark = pytest.mark.skipif(not URL, reason="Set PRAXIMA_TEST_DATABASE_URL to run.")

DOC = b"""# About us

Shanti Clinic has served the city since 1998.

## Timings

We open at nine in the morning. The clinic is closed on Sundays.
"""


class FakeIndex:
    """Records calls; `semantic` is what search returns; `fail` simulates an outage."""

    model = "fake-model"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.upserted: list[IndexedChunk] = []
        self.removed: list[uuid.UUID] = []
        self.semantic: list[uuid.UUID] = []

    async def upsert(self, chunks):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("qdrant down")
        self.upserted += chunks

    async def remove_version(self, workspace_id, document_version_id):  # type: ignore[no-untyped-def]
        self.removed.append(document_version_id)

    async def search(self, workspace_id, query, limit):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("qdrant down")
        return self.semantic[:limit]


async def upload(sessions, scope, actor, ws, data=DOC, name="about.md", **kw):  # type: ignore[no-untyped-def]
    async with scoped_transaction(sessions, scope) as session:
        return await knowledge.upload_document(
            session, actor, workspace_id=ws, filename=name, data=data, category="about", **kw
        )


async def publish(sessions, scope, actor, version_id, index=None):  # type: ignore[no-untyped-def]
    async with scoped_transaction(sessions, scope) as session:
        version, _ = await knowledge.get_version(session, version_id)
        await knowledge.set_version_status(
            session,
            actor,
            version_id=version_id,
            row_version=version.row_version,
            status="published",
            index=index,
        )


def test_upload_review_and_publish_a_document(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await workspace(sessions, admin_id, "one")
        v1 = await upload(sessions, scope, actor, ws)
        async with scoped_transaction(sessions, scope) as session:
            version, sections = await knowledge.get_version(session, v1)
        assert (version.status, version.version_no, version.title) == (
            "needs_review",
            1,
            "About us",
        )
        assert [s.heading for s in sections] == ["About us", "Timings"]

        with pytest.raises(Conflict):  # same file twice
            await upload(sessions, scope, actor, ws)
        with pytest.raises(ValidationFailed):  # not a category of the clinic pack
            async with scoped_transaction(sessions, scope) as session:
                await knowledge.upload_document(
                    session,
                    actor,
                    workspace_id=ws,
                    filename="x.md",
                    data=b"# X\n\nY",
                    category="recipes",
                )
        with pytest.raises(ValidationFailed, match="docx or .md"):
            await upload(sessions, scope, actor, ws, data=b"hello", name="virus.exe")

        edited = [
            knowledge.SectionDraft("About us", "Shanti Clinic has served the city since 1998."),
            knowledge.SectionDraft("Timings", "Open 9 to 5, Monday to Saturday."),
        ]
        async with scoped_transaction(sessions, scope) as session:
            await knowledge.replace_sections(
                session, actor, version_id=v1, row_version=version.row_version, sections=edited
            )
            with pytest.raises(Conflict):  # stale row_version
                await knowledge.replace_sections(
                    session,
                    actor,
                    version_id=v1,
                    row_version=version.row_version,
                    sections=edited,
                )

        index = FakeIndex()
        await publish(sessions, scope, actor, v1, index)
        assert [c.text for c in index.upserted] == [
            "About us\nShanti Clinic has served the city since 1998.",
            "Timings\nOpen 9 to 5, Monday to Saturday.",
        ]
        async with scoped_transaction(sessions, scope) as session:
            current, _ = await knowledge.get_version(session, v1)
            with pytest.raises(Conflict, match="under review"):  # published versions are frozen
                await knowledge.replace_sections(
                    session,
                    actor,
                    version_id=v1,
                    row_version=current.row_version,
                    sections=edited,
                )
            docs = await knowledge.documents_page(session, PageRequest())
            models = await session.scalars(text("SELECT embedding_model FROM knowledge.chunks"))
            assert set(models) == {"fake-model"}
        assert [(d.title, d.published_version_no) for d in docs.items] == [("About us", 1)]

        # Version 2 replaces version 1 on publish; v1's vectors are removed.
        document_id = docs.items[0].id
        v2 = await upload(
            sessions,
            scope,
            actor,
            ws,
            data=DOC + b"\nWe accept walk-ins.\n",
            replaces=document_id,
        )
        await publish(sessions, scope, actor, v2, index)
        async with scoped_transaction(sessions, scope) as session:
            _, versions = await knowledge.get_document(session, document_id)
        assert [(v.version_no, v.status) for v in versions] == [(2, "published"), (1, "superseded")]
        assert index.removed == [v1]

    run(scenario)


def test_publishing_never_waits_for_qdrant(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await workspace(sessions, admin_id, "one")
        version = await upload(sessions, scope, actor, ws)
        await publish(sessions, scope, actor, version, FakeIndex(fail=True))
        async with scoped_transaction(sessions, scope) as session:
            hits = await knowledge.search_knowledge(
                session, ws, "closed on sundays", index=FakeIndex(fail=True)
            )
            models = set(
                await session.scalars(text("SELECT embedding_model FROM knowledge.chunks"))
            )
        assert models == {None}  # not indexed, yet published and searchable by keywords
        assert [h.heading for h in hits] == ["Timings"]

    run(scenario)


def test_search_is_hybrid_live_only_and_tenant_scoped(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope_a, actor_a, ws_a = await workspace(sessions, admin_id, "one")
        scope_b, actor_b, ws_b = await workspace(sessions, admin_id, "two")
        index = FakeIndex()
        published = await upload(sessions, scope_a, actor_a, ws_a)
        await publish(sessions, scope_a, actor_a, published, index)
        draft_only = await upload(
            sessions,
            scope_a,
            actor_a,
            ws_a,
            data=b"# Draft\n\nSecret draft about fees.",
            name="draft.md",
        )
        other = await upload(
            sessions,
            scope_b,
            actor_b,
            ws_b,
            data=b"# Fees\n\nConsultation fees are low.",
            name="fees.md",
        )
        await publish(sessions, scope_b, actor_b, other, index)
        chunk_of = {c.document_version_id: c.id for c in index.upserted}

        async with scoped_transaction(sessions, scope_a) as session:
            assert [
                h.heading for h in await knowledge.search_knowledge(session, ws_a, "sundays")
            ] == ["Timings"]
            # Caller-style questions: filler words don't have to appear, plurals match.
            for question in ("Are you open on Sunday?", "Is the clinic closed on sunday"):
                hits = await knowledge.search_knowledge(session, ws_a, question)
                assert hits and hits[0].heading == "Timings", question
            assert await knowledge.search_knowledge(session, ws_a, "when do you") == []
            assert await knowledge.search_knowledge(session, ws_a, "secret draft") == []
            # Even if the vector store returned another tenant's chunk, it is dropped.
            index.semantic = [chunk_of[other]]
            assert await knowledge.search_knowledge(session, ws_a, "fees", index=index) == []
            # A semantic-only match (no shared keywords) is found through the index.
            timings = next(c.id for c in index.upserted if c.text.startswith("Timings"))
            index.semantic = [timings]
            hits = await knowledge.search_knowledge(session, ws_a, "when do you open", index=index)
            assert [h.chunk_id for h in hits] == [timings]
        assert draft_only

    run(scenario)


def test_faqs_and_announcements(admin_id):
    async def scenario(engine: AsyncEngine, sessions: Sessions) -> None:
        scope, actor, ws = await workspace(sessions, admin_id, "one")
        now = datetime.now(timezone.utc)
        async with scoped_transaction(sessions, scope) as session:
            faq = await knowledge.create_faq(
                session,
                actor,
                workspace_id=ws,
                draft=knowledge.FaqDraft("Do you take insurance?", "Yes, most major insurers."),
            )
            await knowledge.update_faq(
                session,
                actor,
                faq_id=faq,
                row_version=1,
                changes=knowledge.FaqChanges(approved_answer="Yes, all major insurers."),
                status="published",
            )
            with pytest.raises(Conflict):
                await knowledge.update_faq(
                    session,
                    actor,
                    faq_id=faq,
                    row_version=1,
                    changes=knowledge.FaqChanges(),
                    status="archived",
                )
            with pytest.raises(PermissionDenied):
                await knowledge.update_faq(
                    session,
                    iam.Actor(actor.user_id, "staff"),
                    faq_id=faq,
                    row_version=2,
                    changes=knowledge.FaqChanges(),
                    status="archived",
                )
            view = await knowledge.get_faq(session, faq)
            assert (view.approved_answer, view.publication_status, view.row_version) == (
                "Yes, all major insurers.",
                "published",
                2,
            )

            closure = await knowledge.create_announcement(
                session,
                actor,
                workspace_id=ws,
                draft=knowledge.AnnouncementDraft(
                    "closure",
                    "Closed today for Diwali.",
                    now - timedelta(hours=1),
                    now + timedelta(hours=5),
                ),
            )
            await knowledge.create_announcement(
                session,
                actor,
                workspace_id=ws,
                draft=knowledge.AnnouncementDraft(
                    "information",
                    "New lab opens next month.",
                    now + timedelta(days=20),
                    now + timedelta(days=30),
                ),
            )
            for bad in (
                knowledge.AnnouncementDraft("party", "x", now, now + timedelta(hours=1)),
                knowledge.AnnouncementDraft("closure", "x", now, now - timedelta(hours=1)),
            ):
                with pytest.raises(ValidationFailed):
                    await knowledge.create_announcement(session, actor, workspace_id=ws, draft=bad)
            active = await knowledge.announcements_page(session, PageRequest(), active_at=now)
            assert [a.id for a in active.items] == [closure]
            await knowledge.update_announcement(
                session,
                actor,
                announcement_id=closure,
                row_version=1,
                changes=knowledge.AnnouncementChanges(ends_at=now - timedelta(minutes=1)),
            )
            active = await knowledge.announcements_page(session, PageRequest(), active_at=now)
            assert active.items == []

    run(scenario)

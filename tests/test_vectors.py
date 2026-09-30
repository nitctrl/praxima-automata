"""Multi-tenant vector isolation and version-scoped reindexing against a fake Qdrant."""

import asyncio
import json
import logging
from uuid import UUID

import httpx
import pytest
from test_structured_knowledge import content  # noqa: F401

from clinic import vectors as vectors_module
from clinic.rag import HybridRetriever
from clinic.snapshot import DocumentSection
from clinic.vectors import VectorScope, VectorSearch

A, B = UUID(int=0xA), UUID(int=0xB)
V1, V2, V3 = UUID(int=1), UUID(int=2), UUID(int=3)
COLLECTION = "test_sections"


def section(number: int, text: str = "Registration needs a photo ID.") -> DocumentSection:
    return DocumentSection(
        id=UUID(int=number),
        document_id=UUID(int=1000 + number),
        document_title="policy.md",
        document_version=1,
        topic="registration",
        heading="Registration",
        text=text,
        doctor_id=None,
        keywords=(),
    )


def matches(condition, payload) -> bool:
    if "is_empty" in condition:
        return payload.get(condition["is_empty"]["key"]) in (None, [], "")
    value = payload.get(condition["key"])
    if "match" in condition:
        return value == condition["match"]["value"]
    if "range" in condition:
        return value is not None and value < condition["range"]["lt"]
    raise AssertionError(f"unsupported condition {condition}")


def selected(query, payload) -> bool:
    return (
        all(matches(c, payload) for c in query.get("must", []))
        and not any(matches(c, payload) for c in query.get("must_not", []))
        and (not query.get("should") or any(matches(c, payload) for c in query["should"]))
    )


class FakeQdrant:
    """Evaluates the subset of Qdrant filter syntax the adapter emits."""

    def __init__(self, *, ignore_filters: bool = False) -> None:
        self.exists = False
        self.config: dict = {}
        self.indexes: dict[str, dict] = {}
        self.points: dict[str, dict] = {}
        self.ignore_filters = ignore_filters
        self.searches: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(f"/collections/{COLLECTION}")
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path == "":
            if not self.exists:
                return httpx.Response(404)
            params = {"vectors": {"size": 2, "distance": "Cosine"}}
            return httpx.Response(
                200, json={"result": {"config": {"params": params}, "payload_schema": self.indexes}}
            )
        if request.method == "PUT" and path == "":
            self.exists, self.config = True, body
        elif path == "/index":
            self.indexes[body["field_name"]] = body["field_schema"]
        elif path == "/points":
            for point in body["points"]:
                self.points[point["id"]] = point
        elif path == "/points/payload":
            for point in self.points.values():
                if selected(body["filter"], point["payload"]):
                    point["payload"].update(body["payload"])
        elif path == "/points/delete":
            self.points = {
                key: point for key, point in self.points.items()
                if not selected(body["filter"], point["payload"])
            }
        elif path == "/points/search":
            self.searches.append(body)
            rows = [
                {"id": key, "score": 1.0, "payload": point["payload"]}
                for key, point in self.points.items()
                if self.ignore_filters or selected(body["filter"], point["payload"])
            ]
            return httpx.Response(200, json={"result": rows[: body["limit"]]})
        else:
            raise AssertionError(f"unexpected {request.method} {path}")
        return httpx.Response(200, json={"result": True})


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(vectors_module.time, "time", lambda: now[0])
    return now


def adapter(qdrant: FakeQdrant, **options) -> VectorSearch:
    search = VectorSearch(
        url="http://qdrant.test", collection=COLLECTION, model_name="m", **options
    )
    search._client = httpx.AsyncClient(
        base_url="http://qdrant.test", transport=httpx.MockTransport(qdrant)
    )

    async def embed(texts, *, query):
        return [[1.0, 0.0] for _ in texts]

    search._embed_many = embed  # type: ignore[method-assign]
    return search


def test_new_collection_uses_tenant_payload_partitioning(clock):
    qdrant = FakeQdrant()
    asyncio.run(adapter(qdrant).index(VectorScope(A, V1), [section(1)]))
    assert qdrant.config["hnsw_config"] == {"payload_m": 16, "m": 0}
    assert qdrant.indexes["clinic_id"] == {"type": "keyword", "is_tenant": True}
    assert {"version_id", "superseded_at"} <= set(qdrant.indexes)


def test_clinic_a_search_never_returns_clinic_b_sections(clock):
    qdrant = FakeQdrant()
    search = adapter(qdrant)

    async def exercise():
        # Overlapping text and section numbering across tenants.
        await search.index(VectorScope(A, V1), [section(1), section(2)])
        await search.index(VectorScope(B, V1), [section(3), section(4)])
        found_a = await search.search("photo", scope=VectorScope(A, V1), doctor_id=None, limit=9)
        found_b = await search.search("photo", scope=VectorScope(B, V1), doctor_id=None, limit=9)
        return found_a, found_b

    found_a, found_b = asyncio.run(exercise())
    assert set(found_a) == {UUID(int=1), UUID(int=2)}
    assert set(found_b) == {UUID(int=3), UUID(int=4)}
    must = qdrant.searches[0]["filter"]["must"]
    assert {"key": "clinic_id", "match": {"value": str(A)}} in must
    assert {"key": "version_id", "match": {"value": str(V1)}} in must


def test_foreign_points_are_blocked_even_if_the_store_ignores_filters(clock, caplog):
    qdrant = FakeQdrant()
    search = adapter(qdrant)

    async def exercise():
        await search.index(VectorScope(A, V1), [section(1)])
        await search.index(VectorScope(B, V1), [section(9)])
        qdrant.ignore_filters = True  # simulated misconfiguration or store bug
        return await search.search("x", scope=VectorScope(A, V1), doctor_id=None, limit=10)

    with caplog.at_level(logging.ERROR, logger="clinic.vectors"):
        found = asyncio.run(exercise())
    assert found == [UUID(int=1)]
    assert "tenant_leak_blocked" in caplog.text


def test_publishing_a_new_version_keeps_in_flight_version_searchable(clock):
    qdrant = FakeQdrant()
    search = adapter(qdrant, version_grace_seconds=600)

    async def exercise():
        await search.index(VectorScope(A, V1), [section(1), section(2)])
        clock[0] += 60
        # Section 1 carries over into V2 with the same section ID: no overwrite of V1.
        await search.index(VectorScope(A, V2), [section(1), section(5)])
        old = await search.search("x", scope=VectorScope(A, V1), doctor_id=None, limit=10)
        new = await search.search("x", scope=VectorScope(A, V2), doctor_id=None, limit=10)
        return old, new

    old, new = asyncio.run(exercise())
    assert set(old) == {UUID(int=1), UUID(int=2)}
    assert set(new) == {UUID(int=1), UUID(int=5)}
    superseded = {p["payload"]["version_id"] for p in qdrant.points.values()
                  if p["payload"].get("superseded_at")}
    assert superseded == {str(V1)}


def test_superseded_versions_are_pruned_only_after_the_grace_window(clock):
    qdrant = FakeQdrant()
    search = adapter(qdrant, version_grace_seconds=600)

    async def exercise():
        await search.index(VectorScope(A, V1), [section(1)])
        await search.index(VectorScope(B, V1), [section(7)])
        clock[0] += 10
        await search.index(VectorScope(A, V2), [section(2)])  # V1 superseded at t+10
        clock[0] += 100
        await search.index(VectorScope(A, V3), [section(3)])  # V1 still inside grace
        assert {p["payload"]["version_id"] for p in qdrant.points.values()
                if p["payload"]["clinic_id"] == str(A)} == {str(V1), str(V2), str(V3)}
        clock[0] += 1000
        await search.index(VectorScope(A, V3), [section(3)])  # republish after grace

    asyncio.run(exercise())
    versions = {(p["payload"]["clinic_id"], p["payload"]["version_id"])
                for p in qdrant.points.values()}
    # Clinic B is never touched by clinic A's reindex.
    assert versions == {(str(A), str(V3)), (str(B), str(V1))}


def test_removing_every_document_still_retires_old_versions(clock):
    qdrant = FakeQdrant()
    search = adapter(qdrant, version_grace_seconds=0)

    async def exercise():
        await search.index(VectorScope(A, V1), [section(1)])
        clock[0] += 1
        assert await search.index(VectorScope(A, V2), []) == 0
        clock[0] += 1
        await search.index(VectorScope(A, V3), [])

    asyncio.run(exercise())
    assert qdrant.points == {}


def test_slow_vector_search_falls_back_to_lexical(content):  # noqa: F811
    from clinic.snapshot import Snapshot

    payload = dict(content)
    payload["schema_version"] = 3
    payload["clinic_id"] = str(A)
    payload["document_sections"] = [section(1).model_dump(mode="json")]
    snapshot = Snapshot.model_validate(payload)
    search = VectorSearch(url="http://q", collection="c", model_name="m", search_timeout=0.01)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)
        return []

    search._search = slow  # type: ignore[method-assign]
    result = asyncio.run(HybridRetriever(snapshot, V1, search).result("photo ID registration"))
    assert result["status"] == "success"
    assert "photo ID" in str(result["data"]["passages"])

"""Knowledge rules and the Qdrant adapter, without a database or a real Qdrant."""

import asyncio
import json
import uuid

import httpx
import pytest

from praxima.modules.knowledge.application.ports import IndexedChunk, IndexUnavailable
from praxima.modules.knowledge.domain.chunking import chunk_sections, reciprocal_rank_fusion
from praxima.modules.knowledge.infrastructure.qdrant_index import QdrantKnowledgeIndex


def test_chunks_respect_sentences_sections_and_size():
    long = " ".join(f"Sentence {n} is here." for n in range(80))
    chunks = chunk_sections(
        [(0, "Timings", "Open at nine.\n\nक्लिनिक सुबह खुलता है। डॉक्टर उपलब्ध हैं।"), (1, None, long)],
        max_chars=200,
    )
    assert chunks[0].heading == "Timings" and chunks[0].section_position == 0
    assert chunks[0].text.endswith("डॉक्टर उपलब्ध हैं।")  # Hindi sentences stay whole
    assert all(len(c.text) <= 200 for c in chunks)
    assert {c.section_position for c in chunks[1:]} == {1}  # never crosses sections
    assert " ".join(c.text for c in chunks[1:]) == " ".join(long.split())


def test_a_single_huge_word_is_still_split():
    [only, *rest] = chunk_sections([(0, None, "x" * 1000)], max_chars=300)
    assert len(only.text) == 300 and sum(len(c.text) for c in [only, *rest]) == 1000


def test_rank_fusion_prefers_items_found_by_both():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert reciprocal_rank_fusion([[a, b], [b, c]], limit=3)[0] == b
    assert reciprocal_rank_fusion([[a], []], limit=5) == [a]
    assert reciprocal_rank_fusion([], limit=5) == []


class FakeEmbedder:
    model = "fake-3d"

    async def embed(self, texts):  # type: ignore[no-untyped-def]
        return [[float(len(t)), 1.0, 0.0] for t in texts]


def qdrant(handler, **kwargs):  # type: ignore[no-untyped-def]
    return QdrantKnowledgeIndex(
        url="http://qdrant.test",
        collection="praxima_knowledge",
        embedder=FakeEmbedder(),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_qdrant_scopes_every_point_and_search_to_the_workspace():
    calls: list[tuple[str, str, dict]] = []  # type: ignore[type-arg]
    workspace, version, chunk = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(404)
        if request.url.path.endswith("/points/search"):
            return httpx.Response(200, json={"result": [{"id": str(chunk), "score": 0.9}]})
        return httpx.Response(200, json={"result": {}})

    index = qdrant(handler, api_key="secret")

    async def scenario() -> list[uuid.UUID]:
        await index.upsert([IndexedChunk(chunk, workspace, version, "Doctor hours")])
        await index.remove_version(workspace, version)
        return await index.search(workspace, "hours", 5)

    assert asyncio.run(scenario()) == [chunk]
    created = [c for c in calls if c[1] == "/collections/praxima_knowledge" and c[0] == "PUT"]
    assert created[0][2] == {"vectors": {"size": 3, "distance": "Cosine"}}
    [upsert] = [c for c in calls if c[1].endswith("/points")]
    assert upsert[2]["points"][0]["payload"] == {
        "workspace_id": str(workspace),
        "document_version_id": str(version),
    }
    for method, path, body in calls:
        if path.endswith(("/points/search", "/points/delete")):
            must = body["filter"]["must"]
            assert {"key": "workspace_id", "match": {"value": str(workspace)}} in must


def test_qdrant_failures_become_index_unavailable():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(IndexUnavailable):
        asyncio.run(qdrant(down).search(uuid.uuid4(), "hours", 5))

    def wrong_size(request: httpx.Request) -> httpx.Response:
        config = {"result": {"config": {"params": {"vectors": {"size": 384}}}}}
        return httpx.Response(200, json=config)

    with pytest.raises(IndexUnavailable, match="dimension"):
        asyncio.run(qdrant(wrong_size).search(uuid.uuid4(), "hours", 5))


def test_index_is_off_without_configuration(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    assert QdrantKnowledgeIndex.from_environment() is None
    monkeypatch.setenv("QDRANT_URL", "ftp://nope")
    assert QdrantKnowledgeIndex.from_environment() is None


def test_search_terms_drop_filler_and_stay_tsquery_safe():
    from praxima.modules.knowledge.domain.search import search_terms, tsquery_text

    assert search_terms("Are you open on Sunday?") == ["open", "sunday"]
    assert search_terms("Dr. Sharma kab milte hain?") == ["sharma", "milte"]
    assert search_terms("when do you") == []  # only filler: semantic search alone
    assert search_terms("fees & (timings) | ecg:* 'x'") == ["fees", "timings", "ecg"]
    assert tsquery_text(["open", "sunday"]) == "'open':* | 'sunday':*"

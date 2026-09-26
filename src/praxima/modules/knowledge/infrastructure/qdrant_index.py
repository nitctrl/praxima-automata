"""Qdrant vector index for published knowledge chunks (REST over httpx).

Its own collection, separate from the voice agent's. Qdrant has no row-level security, so
every point carries its workspace_id and every search filters on it. Chunk text is sent
only for embedding (locally, via fastembed) and never logged.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from praxima.modules.knowledge.application.ports import IndexedChunk, IndexUnavailable

logger = logging.getLogger(__name__)
DEFAULT_COLLECTION = "praxima_knowledge"
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    model: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class FastEmbedEmbedder:
    """Local embeddings (optional `semantic` extra). CPU work runs in a thread."""

    model: str
    _engine: Any = field(default=None, repr=False)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        def run() -> list[list[float]]:
            if self._engine is None:
                from fastembed import TextEmbedding

                self._engine = TextEmbedding(model_name=self.model)
            return [list(map(float, vector)) for vector in self._engine.embed(list(texts))]

        return await asyncio.to_thread(run)


@dataclass
class QdrantKnowledgeIndex:
    url: str
    collection: str
    embedder: Embedder
    api_key: str | None = field(default=None, repr=False)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    _ready: bool = field(default=False, repr=False)

    @property
    def model(self) -> str:
        return self.embedder.model

    @classmethod
    def from_environment(cls) -> "QdrantKnowledgeIndex | None":
        """None when Qdrant or the embedding library isn't configured (keyword search only)."""
        url = os.environ.get("QDRANT_URL", "").strip().rstrip("/")
        if not url:
            return None
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            logger.warning("Invalid QDRANT_URL; knowledge search uses keywords only")
            return None
        try:
            import fastembed  # noqa: F401
        except ImportError:
            logger.warning("fastembed not installed; knowledge search uses keywords only")
            return None
        return cls(
            url=url,
            collection=os.environ.get("PRAXIMA_KNOWLEDGE_COLLECTION", DEFAULT_COLLECTION),
            embedder=FastEmbedEmbedder(os.environ.get("QDRANT_EMBEDDING_MODEL", DEFAULT_MODEL)),
            api_key=os.environ.get("QDRANT_API_KEY") or None,
        )

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"api-key": self.api_key} if self.api_key else {}
        try:
            async with httpx.AsyncClient(
                base_url=self.url, timeout=10, transport=self.transport, headers=headers
            ) as client:
                response = await client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise IndexUnavailable(type(exc).__name__) from None
        if response.status_code == 404 and method == "GET":
            return None
        if response.status_code >= 400:
            raise IndexUnavailable(f"HTTP {response.status_code}")
        return response.json()

    async def _ensure_collection(self, dimensions: int) -> None:
        if self._ready:
            return
        path = f"/collections/{self.collection}"
        existing = await self._call("GET", path)
        if existing is None:
            await self._call("PUT", path, {"vectors": {"size": dimensions, "distance": "Cosine"}})
            for field_name in ("workspace_id", "document_version_id"):
                await self._call(
                    "PUT",
                    f"{path}/index?wait=true",
                    {"field_name": field_name, "field_schema": "keyword"},
                )
        else:
            size = existing["result"]["config"]["params"]["vectors"]["size"]
            if size != dimensions:
                raise IndexUnavailable("collection dimension does not match the model")
        self._ready = True

    async def upsert(self, chunks: Sequence[IndexedChunk]) -> None:
        if not chunks:
            return
        vectors = await self.embedder.embed([c.text for c in chunks])
        await self._ensure_collection(len(vectors[0]))
        points = [
            {
                "id": str(chunk.id),
                "vector": vector,
                "payload": {
                    "workspace_id": str(chunk.workspace_id),
                    "document_version_id": str(chunk.document_version_id),
                },
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        await self._call(
            "PUT", f"/collections/{self.collection}/points?wait=true", {"points": points}
        )

    async def remove_version(self, workspace_id: uuid.UUID, document_version_id: uuid.UUID) -> None:
        body = {"filter": _must(workspace_id, document_version_id=str(document_version_id))}
        await self._call("POST", f"/collections/{self.collection}/points/delete?wait=true", body)

    async def search(self, workspace_id: uuid.UUID, query: str, limit: int) -> list[uuid.UUID]:
        [vector] = await self.embedder.embed([query])
        await self._ensure_collection(len(vector))
        result = await self._call(
            "POST",
            f"/collections/{self.collection}/points/search",
            {
                "vector": vector,
                "limit": limit,
                "filter": _must(workspace_id),
                "with_payload": False,
            },
        )
        return [uuid.UUID(str(hit["id"])) for hit in result.get("result", [])]


def _must(workspace_id: uuid.UUID, **extra: str) -> dict[str, Any]:
    """Always filter by workspace: the tenant boundary inside Qdrant."""
    conditions = {"workspace_id": str(workspace_id), **extra}
    return {"must": [{"key": key, "match": {"value": value}} for key, value in conditions.items()]}

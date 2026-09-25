"""Optional local Qdrant search over reviewed, published document sections.

The agent always keeps a lexical fallback. This module is enabled only when a
Qdrant URL is configured and the optional ``fastembed`` package is installed.
It never indexes raw uploads or reaches the database.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from praxima.modules.releases.domain.snapshot import DocumentSection

logger = logging.getLogger(__name__)


@dataclass
class VectorSearch:
    """Minimal Qdrant REST adapter; Qdrant itself remains optional."""

    url: str
    collection: str
    model_name: str
    api_key: str | None = None
    _model: Any = field(default=None, init=False, repr=False)

    @classmethod
    def from_environment(cls) -> VectorSearch | None:
        url = os.environ.get("QDRANT_URL", "").strip().rstrip("/")
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            logger.warning("Invalid QDRANT_URL; using lexical document search only")
            return None
        try:
            import fastembed  # noqa: F401 - optional dependency availability check
        except ImportError:
            logger.warning("fastembed is unavailable; using lexical document search only")
            return None
        return cls(
            url=url,
            collection=os.environ.get("QDRANT_COLLECTION", "clinic_document_sections"),
            model_name=os.environ.get("QDRANT_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
            api_key=os.environ.get("QDRANT_API_KEY") or None,
        )

    def _embedder(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    async def warm(self) -> None:
        """Load the local embedding model before a caller asks a question."""
        await asyncio.to_thread(self._embedder)

    async def _embed_many(self, texts: Iterable[str], *, query: bool) -> list[list[float]]:
        values = tuple(texts)

        def generate() -> list[list[float]]:
            model = self._embedder()
            embeddings = model.query_embed(values) if query else model.embed(values)
            return [[float(number) for number in row] for row in embeddings]

        return await asyncio.to_thread(generate)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.request(
                method, f"{self.url}{path}", headers=self._headers(), **kwargs
            )
        response.raise_for_status()
        return response

    async def _ensure_collection(self, dimensions: int) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{self.url}/collections/{self.collection}", headers=self._headers()
            )
        if response.status_code == 404:
            await self._request(
                "PUT",
                f"/collections/{self.collection}",
                json={"vectors": {"size": dimensions, "distance": "Cosine"}},
            )
            return
        response.raise_for_status()
        parameters = response.json().get("result", {}).get("config", {}).get("params", {})
        vectors = parameters.get("vectors")
        if not isinstance(vectors, dict) or vectors.get("size") != dimensions:
            raise ValueError("Qdrant collection does not match the configured embedding model")

    async def index(self, clinic: UUID, version: UUID, sections: Iterable[DocumentSection]) -> int:
        """Replace one published snapshot's reviewed sections in Qdrant."""
        rows = tuple(sections)
        if not rows:
            return 0
        vectors = await self._embed_many(
            (f"{section.heading}\n{section.text}" for section in rows), query=False
        )
        await self._ensure_collection(len(vectors[0]))
        snapshot_filter = {
            "must": [
                {"key": "clinic_id", "match": {"value": str(clinic)}},
                {"key": "version_id", "match": {"value": str(version)}},
            ]
        }
        await self._request(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            json={"filter": snapshot_filter},
        )
        points = [
            {
                "id": str(section.id),
                "vector": vector,
                "payload": {
                    "clinic_id": str(clinic),
                    "version_id": str(version),
                    "doctor_id": str(section.doctor_id) if section.doctor_id else None,
                },
            }
            for section, vector in zip(rows, vectors, strict=True)
        ]
        await self._request(
            "PUT", f"/collections/{self.collection}/points?wait=true", json={"points": points}
        )
        return len(points)

    async def search(
        self, question: str, *, clinic: UUID, version: UUID, doctor_id: UUID | None, limit: int
    ) -> list[UUID]:
        """Return reviewed section IDs in semantic rank order for one published snapshot."""
        vector = (await self._embed_many((question,), query=True))[0]
        filters: dict[str, Any] = {
            "must": [
                {"key": "clinic_id", "match": {"value": str(clinic)}},
                {"key": "version_id", "match": {"value": str(version)}},
            ]
        }
        if doctor_id is not None:
            filters["should"] = [
                {"key": "doctor_id", "match": {"value": str(doctor_id)}},
                {"is_null": {"key": "doctor_id"}},
            ]
        response = await self._request(
            "POST",
            f"/collections/{self.collection}/points/search",
            json={"vector": vector, "limit": max(1, min(limit, 20)), "filter": filters},
        )
        identifiers: list[UUID] = []
        for row in response.json().get("result", []):
            try:
                identifiers.append(UUID(str(row["id"])))
            except (KeyError, TypeError, ValueError):
                continue
        return identifiers

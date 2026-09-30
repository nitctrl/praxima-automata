"""Multi-tenant Qdrant search over reviewed, published document sections.

Tenancy model (payload-based partitioning, one shared collection):

* Every point carries ``clinic_id`` and ``version_id``. ``clinic_id`` is a keyword
  payload index with ``is_tenant`` so Qdrant co-locates each clinic's vectors and
  builds per-tenant HNSW graphs (``payload_m``) instead of one global graph (``m=0``).
* Callers address vectors only through ``VectorScope`` built from a backend-resolved
  clinic and its pinned configuration version. No public method accepts a raw filter.
* Every search result is re-checked against the scope; a mismatch is dropped and
  logged as ``tenant_leak_blocked``.

Reindexing (version-scoped, blue/green):

* Point IDs derive from ``(clinic, version, section)``, so indexing a new version never
  overwrites the points an in-flight call pinned to an older version is searching.
* After a new version is indexed, older versions of that clinic are only marked
  ``superseded_at``; they are deleted on a later publish once the grace window (longer
  than the maximum call duration) has passed.

The agent always keeps a lexical fallback. This module is enabled only when a Qdrant
URL is configured and the optional ``fastembed`` package is installed. It never
indexes raw uploads or reaches the database.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import httpx

from clinic.snapshot import DocumentSection

logger = logging.getLogger(__name__)

# Stable namespace for version-scoped point IDs. Never change: it would orphan points.
POINT_NAMESPACE = UUID("5f0c7d2e-8a4b-4c1e-9d3a-2b6f1e7c9a10")
TENANT_FIELD = "clinic_id"
PAYLOAD_INDEXES: dict[str, dict[str, Any]] = {
    TENANT_FIELD: {"type": "keyword", "is_tenant": True},
    "version_id": {"type": "keyword"},
    "superseded_at": {"type": "float"},
}

_EMBEDDERS: dict[str, Any] = {}
_EMBEDDER_LOCK = threading.Lock()


def load_embedder(model_name: str) -> Any:
    """One embedding model per worker process, shared by every call it serves."""
    with _EMBEDDER_LOCK:
        model = _EMBEDDERS.get(model_name)
        if model is None:
            from fastembed import TextEmbedding

            model = _EMBEDDERS[model_name] = TextEmbedding(model_name=model_name)
        return model


@dataclass(frozen=True)
class VectorScope:
    """The only handle for reading or writing vectors: one clinic, one pinned version.

    Build it from a backend-resolved clinic (SIP destination or authorized dashboard
    session), never from model output or caller speech.
    """

    clinic_id: UUID
    version_id: UUID

    def must(self) -> list[dict[str, Any]]:
        return [
            {"key": TENANT_FIELD, "match": {"value": str(self.clinic_id)}},
            {"key": "version_id", "match": {"value": str(self.version_id)}},
        ]

    def point_id(self, section_id: UUID) -> str:
        return str(uuid5(POINT_NAMESPACE, f"{self.clinic_id}:{self.version_id}:{section_id}"))

    def owns(self, payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and payload.get(TENANT_FIELD) == str(self.clinic_id)
            and payload.get("version_id") == str(self.version_id)
        )


def _float_env(name: str, default: float | None) -> float | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Ignoring invalid %s", name)
        return default


@dataclass
class VectorSearch:
    """Minimal Qdrant REST adapter; Qdrant itself remains optional."""

    url: str
    collection: str
    model_name: str
    api_key: str | None = None
    # Upper bound for embedding + search on the call path; lexical rank is used beyond it.
    search_timeout: float = 0.8
    # Cosine similarity floor for semantic hits; None keeps every ranked hit.
    score_threshold: float | None = None
    # Superseded versions stay searchable at least this long (> maximum call duration).
    version_grace_seconds: float = 1800
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

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
            search_timeout=_float_env("QDRANT_SEARCH_TIMEOUT_SECONDS", 0.8) or 0.8,
            score_threshold=_float_env("QDRANT_SCORE_THRESHOLD", None),
            version_grace_seconds=_float_env("QDRANT_VERSION_GRACE_SECONDS", 1800) or 1800,
        )

    def preload(self) -> None:
        """Load the embedding model synchronously, e.g. in the worker prewarm hook."""
        load_embedder(self.model_name)

    async def warm(self) -> None:
        """Load the local embedding model before a caller asks a question."""
        await asyncio.to_thread(load_embedder, self.model_name)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _embed_many(self, texts: Iterable[str], *, query: bool) -> list[list[float]]:
        values = tuple(texts)

        def generate() -> list[list[float]]:
            model = load_embedder(self.model_name)
            embeddings = model.query_embed(values) if query else model.embed(values)
            return [[float(number) for number in row] for row in embeddings]

        return await asyncio.to_thread(generate)

    def _http(self) -> httpx.AsyncClient:
        # One keep-alive client per adapter avoids a TCP/TLS handshake on every search.
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["api-key"] = self.api_key
            self._client = httpx.AsyncClient(base_url=self.url, headers=headers, timeout=10)
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._http().request(method, path, **kwargs)
        response.raise_for_status()
        return response

    async def _collection(self) -> dict[str, Any] | None:
        response = await self._http().get(f"/collections/{self.collection}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        result: dict[str, Any] = response.json().get("result", {})
        return result

    async def _ensure_collection(self, dimensions: int) -> None:
        info = await self._collection()
        if info is None:
            await self._request(
                "PUT",
                f"/collections/{self.collection}",
                json={
                    "vectors": {"size": dimensions, "distance": "Cosine"},
                    # Per-tenant graphs only; every search is filtered by clinic_id.
                    "hnsw_config": {"payload_m": 16, "m": 0},
                },
            )
            existing: dict[str, Any] = {}
        else:
            vectors = info.get("config", {}).get("params", {}).get("vectors")
            if not isinstance(vectors, dict) or vectors.get("size") != dimensions:
                raise ValueError("Qdrant collection does not match the configured embedding model")
            existing = info.get("payload_schema") or {}
        for name, schema in PAYLOAD_INDEXES.items():
            if name not in existing:
                await self._request(
                    "PUT",
                    f"/collections/{self.collection}/index?wait=true",
                    json={"field_name": name, "field_schema": schema},
                )

    async def index(self, scope: VectorScope, sections: Iterable[DocumentSection]) -> int:
        """Index one published version, then retire and prune older versions of the clinic."""
        rows = tuple(sections)
        now = time.time()
        if rows:
            vectors = await self._embed_many(
                (f"{section.heading}\n{section.text}" for section in rows), query=False
            )
            await self._ensure_collection(len(vectors[0]))
            points = [
                {
                    "id": scope.point_id(section.id),
                    "vector": vector,
                    "payload": {
                        TENANT_FIELD: str(scope.clinic_id),
                        "version_id": str(scope.version_id),
                        "section_id": str(section.id),
                        "doctor_id": str(section.doctor_id) if section.doctor_id else None,
                        "embedding_model": self.model_name,
                        "indexed_at": now,
                    },
                }
                for section, vector in zip(rows, vectors, strict=True)
            ]
            await self._request(
                "PUT", f"/collections/{self.collection}/points?wait=true", json={"points": points}
            )
        elif await self._collection() is None:
            return 0
        await self._retire(scope, now)
        return len(rows)

    async def _retire(self, scope: VectorScope, now: float) -> None:
        """Mark this clinic's other versions superseded; delete ones past the grace window."""
        clinic = {"key": TENANT_FIELD, "match": {"value": str(scope.clinic_id)}}
        await self._request(
            "POST",
            f"/collections/{self.collection}/points/payload?wait=true",
            json={
                "payload": {"superseded_at": now},
                "filter": {
                    "must": [clinic, {"is_empty": {"key": "superseded_at"}}],
                    "must_not": [
                        {"key": "version_id", "match": {"value": str(scope.version_id)}}
                    ],
                },
            },
        )
        await self._request(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            json={
                "filter": {
                    "must": [
                        clinic,
                        {
                            "key": "superseded_at",
                            "range": {"lt": now - self.version_grace_seconds},
                        },
                    ],
                    "must_not": [
                        {"key": "version_id", "match": {"value": str(scope.version_id)}}
                    ],
                }
            },
        )

    async def search(
        self, question: str, *, scope: VectorScope, doctor_id: UUID | None, limit: int
    ) -> list[UUID]:
        """Return reviewed section IDs in semantic rank order for one scoped version."""
        return await asyncio.wait_for(
            self._search(question, scope=scope, doctor_id=doctor_id, limit=limit),
            self.search_timeout,
        )

    async def _search(
        self, question: str, *, scope: VectorScope, doctor_id: UUID | None, limit: int
    ) -> list[UUID]:
        vector = (await self._embed_many((question,), query=True))[0]
        filters: dict[str, Any] = {"must": scope.must()}
        if doctor_id is not None:
            filters["should"] = [
                {"key": "doctor_id", "match": {"value": str(doctor_id)}},
                {"is_empty": {"key": "doctor_id"}},
            ]
        body: dict[str, Any] = {
            "vector": vector,
            "limit": max(1, min(limit, 20)),
            "filter": filters,
            "with_payload": [TENANT_FIELD, "version_id", "section_id"],
        }
        if self.score_threshold is not None:
            body["score_threshold"] = self.score_threshold
        response = await self._request(
            "POST", f"/collections/{self.collection}/points/search", json=body
        )
        identifiers: list[UUID] = []
        for row in response.json().get("result", []):
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict) or not scope.owns(payload):
                # Defense in depth: the filter above should make this unreachable.
                logger.error(
                    "tenant_leak_blocked clinic=%s version=%s", scope.clinic_id, scope.version_id
                )
                continue
            try:
                # Points written before version-scoped IDs used the section ID directly.
                section = UUID(str(payload.get("section_id") or row["id"]))
            except (KeyError, TypeError, ValueError):
                continue
            if section not in identifiers:
                identifiers.append(section)
        return identifiers

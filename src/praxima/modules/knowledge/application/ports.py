"""What the knowledge services need from a vector index (implemented by Qdrant, or fakes)."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class IndexedChunk:
    id: uuid.UUID
    workspace_id: uuid.UUID
    document_version_id: uuid.UUID
    text: str


class KnowledgeIndex(Protocol):
    """Semantic index of published chunks. Every call is scoped to one workspace."""

    @property
    def model(self) -> str:
        """The embedding model name, recorded on each indexed chunk."""
        ...

    async def upsert(self, chunks: Sequence[IndexedChunk]) -> None: ...

    async def remove_version(
        self, workspace_id: uuid.UUID, document_version_id: uuid.UUID
    ) -> None: ...

    async def search(self, workspace_id: uuid.UUID, query: str, limit: int) -> list[uuid.UUID]: ...


class IndexUnavailable(RuntimeError):
    """The vector index could not be reached; keyword search still works."""

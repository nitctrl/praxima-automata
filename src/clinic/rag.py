"""Hybrid-RRF retrieval over reviewed uploads and unexpired live updates."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from clinic.documents import DocumentIndex, excerpt, tokens
from clinic.snapshot import DocumentSection, Notice, Snapshot
from clinic.vectors import VectorSearch

logger = logging.getLogger(__name__)


def _notice_text(snapshot: Snapshot, notice: Notice) -> str:
    zone = ZoneInfo(snapshot.timezone)
    start = notice.starts_at.astimezone(zone).isoformat(timespec="minutes")
    end = notice.expires_at.astimezone(zone).isoformat(timespec="minutes")
    return f"Applies from {start} until {end} in {snapshot.timezone}. {notice.public_message}"


def snapshot_sections(snapshot: Snapshot) -> tuple[DocumentSection, ...]:
    """Return reviewed documents plus current and scheduled published updates."""
    rows = list(snapshot.document_sections)
    now = datetime.now(timezone.utc)
    for notice in snapshot.temporary_notices:
        if notice.expires_at <= now:
            continue
        state = "Current" if notice.starts_at <= now else "Scheduled"
        rows.append(DocumentSection(
            id=notice.id,
            document_id=notice.id,
            document_title="Live update",
            document_version=1,
            topic="live_update",
            heading=f"{state} live update",
            text=_notice_text(snapshot, notice),
            doctor_id=notice.doctor_id,
            keywords=("clinic", "open", "closed", "closure", "hours", "schedule"),
        ))
    return tuple(rows)


class HybridRetriever:
    """The shared retrieval implementation used by dashboard and phone calls."""

    def __init__(self, snapshot: Snapshot, version: UUID | None, vectors: VectorSearch | None):
        self.snapshot = snapshot
        self.version = version
        self.sections = snapshot_sections(snapshot)
        self.index = DocumentIndex(self.sections)
        self.vectors = vectors

    async def search(self, question: str, *, limit: int = 4) -> list[tuple[float, DocumentSection]]:
        semantic: tuple[UUID, ...] = ()
        if self.vectors is not None and self.version is not None:
            try:
                semantic = tuple(await self.vectors.search(
                    question,
                    clinic=self.snapshot.clinic_id,
                    version=self.version,
                    doctor_id=None,
                    limit=max(limit * 3, 10),
                ))
            except Exception as exc:
                logger.warning(
                    "Semantic retrieval unavailable (%s); using lexical rank",
                    type(exc).__name__,
                )
        return self.index.search(question, semantic=semantic, limit=limit)

    async def result(self, question: str, *, limit: int = 4) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            return {"status": "unavailable", "data": {"passages": []}}
        hits = await self.search(question, limit=limit)
        wanted = tokens(question)
        passages = [
            {
                "source": section.document_title,
                "topic": section.topic,
                "heading": section.heading,
                "text": excerpt(section.text, wanted),
            }
            for _, section in hits
        ]
        return {
            "status": "success" if passages else "unavailable",
            "retrieval": "hybrid_rrf" if self.vectors is not None else "lexical_fallback",
            "data": {"passages": passages},
        }


def active_quick_info(snapshot: Snapshot) -> tuple[str, ...]:
    now = datetime.now(timezone.utc)
    return tuple(
        row.public_message
        for row in sorted(snapshot.temporary_notices, key=lambda item: -item.priority)
        if row.starts_at <= now < row.expires_at
    )


def published_live_updates(snapshot: Snapshot) -> tuple[str, ...]:
    """Bounded prompt context for current and scheduled published changes."""
    now = datetime.now(timezone.utc)
    rows = sorted(snapshot.temporary_notices, key=lambda row: (-row.priority, row.starts_at))
    return tuple(_notice_text(snapshot, row) for row in rows if row.expires_at > now)[:50]

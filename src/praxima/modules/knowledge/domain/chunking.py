"""Pure knowledge rules: split reviewed sections into search chunks; fuse rankings (RRF)."""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

MAX_CHUNK_CHARS = 800
RRF_K = 60
_PARAGRAPHS = re.compile(r"\n\s*\n")
# Sentence ends, including the Devanagari danda.
_SENTENCES = re.compile(r"(?<=[.!?।])\s+")


@dataclass(frozen=True)
class ChunkDraft:
    section_position: int
    heading: str | None
    text: str


def _pieces(text: str, limit: int) -> list[str]:
    """Paragraphs, then sentences, then words: every piece fits within `limit`."""
    pieces: list[str] = []
    for paragraph in _PARAGRAPHS.split(text.strip()):
        for sentence in _SENTENCES.split(paragraph.strip()):
            sentence = " ".join(sentence.split())
            while len(sentence) > limit:
                cut = sentence.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                pieces.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                pieces.append(sentence)
    return pieces


def chunk_sections(
    sections: Sequence[tuple[int, str | None, str]], max_chars: int = MAX_CHUNK_CHARS
) -> list[ChunkDraft]:
    """Greedily pack each section's pieces into chunks; chunks never cross sections."""
    chunks: list[ChunkDraft] = []
    for position, heading, text in sections:
        current = ""
        for piece in _pieces(text, max_chars):
            if current and len(current) + 1 + len(piece) > max_chars:
                chunks.append(ChunkDraft(position, heading, current))
                current = piece
            else:
                current = f"{current} {piece}" if current else piece
        if current:
            chunks.append(ChunkDraft(position, heading, current))
    return chunks


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[uuid.UUID]], limit: int, k: int = RRF_K
) -> list[uuid.UUID]:
    """Merge ranked lists (e.g. keyword and semantic): score = Σ 1 / (k + rank)."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], str(item)))[:limit]

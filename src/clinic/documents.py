"""Staff-reviewed clinic prose: extraction, tagging and fast in-memory retrieval.

Uploads are parsed here, never rendered. Nothing in this module reaches a caller until a
manager reviews the text and publishes a configuration version.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from clinic.snapshot import DocumentSection, normalize

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000
MAX_DOCUMENT_CHARS = 100_000
MAX_SECTION_CHARS = 1200
MAX_SECTIONS = 200
EXCERPT_CHARS = 600
RRF_K = 60

CATEGORIES = (
    "about",
    "vision",
    "story",
    "achievements",
    "doctor_bio",
    "facilities",
    "policies",
    "registration",
    "other",
)
MIME_TYPES = {
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_STOPWORDS = frozenset(
    """a an and are as at be by do does for from has have how i in is it its me my of on or our
    tell that the their there they this to was we what when where which who why will with you your
    ka ke ki ko kya kaun kaha kab hai hain ho me mein se aur ya bhi ye yeh wo woh
    का के की को क्या कौन कहाँ कब है हैं हो में से और या भी यह वह""".split()
)
_TITLES = frozenset("dr drs doctor prof professor mr mrs ms डॉ डा डॉक्टर श्री श्रीमती".split())
_TAG = re.compile(r"<[^>\n]{0,200}>")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"[*_`~]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BRACKET = re.compile(r"\([^)]*\)")


class DocumentRejected(ValueError):
    """The upload cannot be accepted; the message is shown to staff."""


class DraftSection(BaseModel):
    """A reviewable section before publication. Staff may edit or delete any of these."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    position: int = Field(ge=0, le=MAX_SECTIONS)
    heading: str = Field(max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    doctor_id: UUID | None = None
    keywords: tuple[str, ...] = ()


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    mime_type: str
    checksum: str
    sections: tuple[DraftSection, ...]
    warnings: tuple[str, ...]


def tokens(text: str) -> list[str]:
    # normalize() already separates words and keeps combining marks, so Devanagari words
    # survive intact; a letter-class regex would split them at every matra.
    return [t for t in normalize(text).split() if t not in _STOPWORDS and len(t) > 1]


def _person(name: str) -> str:
    """Normalised personal name without bracketed notes or honorifics."""
    return " ".join(w for w in normalize(_BRACKET.sub(" ", name)).split() if w not in _TITLES)


def _clean(line: str) -> str:
    line = _IMAGE.sub(" ", line)
    line = _LINK.sub(r"\1", line)
    line = _TAG.sub(" ", line)
    return re.sub(r"\s+", " ", _MARKUP.sub("", line)).strip()


def _sections(blocks: Iterable[tuple[str, str]], fallback: str) -> tuple[DraftSection, ...]:
    """Group (heading, paragraph) pairs into bounded sections that keep document order."""
    grouped: list[tuple[str, list[str]]] = []
    for heading, body in blocks:
        current = grouped[-1] if grouped else None
        full = current is not None and sum(map(len, current[1])) > MAX_SECTION_CHARS
        if current is None or current[0] != heading or full:
            grouped.append((heading, [body]))
        else:
            current[1].append(body)
    drafts = []
    for position, (heading, bodies) in enumerate(grouped[:MAX_SECTIONS]):
        text = "\n".join(bodies)[:4000]
        drafts.append(
            DraftSection(
                id=uuid5(NAMESPACE_URL, f"clinic-document-section:{fallback}:{position}"),
                position=position,
                heading=heading[:200],
                text=text,
                keywords=tuple(dict.fromkeys(tokens(heading)))[:12],
            )
        )
    return tuple(drafts)


def _markdown(data: bytes) -> tuple[list[tuple[str, str]], list[str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DocumentRejected("Markdown files must be saved as UTF-8 text.") from None
    warnings: list[str] = []
    blocks: list[tuple[str, str]] = []
    heading = ""
    fenced = False
    for raw in text[:MAX_DOCUMENT_CHARS].splitlines():
        if raw.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _HEADING.match(raw)
        if match:
            heading = _clean(match.group(2))
            continue
        body = _clean(raw)
        if body:
            blocks.append((heading, body))
    if len(text) > MAX_DOCUMENT_CHARS:
        warnings.append("The document was truncated to the supported length.")
    return blocks, warnings


def _paragraphs(document: Any) -> Iterator[tuple[str, str]]:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    heading = ""
    body = document.element.body
    for child in body.iterchildren():
        tag = str(child.tag).rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = _clean(paragraph.text)
            if not text:
                continue
            style = getattr(paragraph.style, "name", "") or ""
            if str(style).lower().startswith("heading"):
                heading = text
            else:
                yield heading, text
        elif tag == "tbl":
            for row in Table(child, document).rows:
                cells = [_clean(cell.text) for cell in row.cells]
                line = " — ".join(dict.fromkeys(cell for cell in cells if cell))
                if line:
                    yield heading, line


def _docx(data: bytes) -> tuple[list[tuple[str, str]], list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except zipfile.BadZipFile:
        raise DocumentRejected("The file is not a readable .docx package.") from None
    if len(entries) > MAX_ARCHIVE_ENTRIES or sum(e.file_size for e in entries) > (
        MAX_UNCOMPRESSED_BYTES
    ):
        raise DocumentRejected("The document expands to an unsupported size.")
    if any(entry.flag_bits & 0x1 for entry in entries):
        raise DocumentRejected("Password-protected documents cannot be reviewed.")
    names = {entry.filename for entry in entries}
    if "word/document.xml" not in names:
        raise DocumentRejected("The file is not a Word document.")
    if any(name.endswith((".bin", ".emf", ".wmf")) for name in names):
        raise DocumentRejected("Remove macros and embedded objects before uploading.")
    markup = archive.read("word/document.xml")[:MAX_UNCOMPRESSED_BYTES]
    if b"<!DOCTYPE" in markup or b"<!ENTITY" in markup:
        raise DocumentRejected("The document contains unsupported XML declarations.")
    try:
        from docx import Document

        document = Document(io.BytesIO(data))
    except Exception:
        raise DocumentRejected("The document could not be read.") from None
    warnings: list[str] = []
    if any(name.startswith("word/media/") for name in names):
        warnings.append("Images are ignored; text inside pictures is not read.")
    if b"<w:ins " in markup or b"<w:del " in markup:
        warnings.append("Tracked changes are read as final text; confirm the wording.")
    blocks: list[tuple[str, str]] = []
    length = 0
    for heading, body in _paragraphs(document):
        length += len(body)
        if length > MAX_DOCUMENT_CHARS:
            warnings.append("The document was truncated to the supported length.")
            break
        blocks.append((heading, body))
    return blocks, warnings


def extract(filename: str, data: bytes) -> Extraction:
    """Parse an upload into reviewable sections. Raises DocumentRejected for unusable files."""
    if not data:
        raise DocumentRejected("The upload is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentRejected("Upload documents smaller than 5 MB.")
    name = PurePosixPath(filename.replace("\\", "/")).name[:200]
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in MIME_TYPES:
        raise DocumentRejected("Upload a .docx or .md document.")
    checksum = sha256(data).hexdigest()
    blocks, warnings = _markdown(data) if suffix == ".md" else _docx(data)
    if not blocks:
        raise DocumentRejected("No readable text was found in the document.")
    sections = _sections(blocks, checksum)
    title = next((heading for heading, _ in blocks if heading), name)[:200]
    return Extraction(
        title=title,
        mime_type=MIME_TYPES[suffix],
        checksum=checksum,
        sections=sections,
        warnings=tuple(warnings),
    )


def tag_doctors(
    sections: Sequence[DraftSection], doctors: Sequence[tuple[UUID, Sequence[str]]]
) -> tuple[DraftSection, ...]:
    """Attach a doctor to sections that clearly name exactly one doctor.

    Full names are tried first: a shared surname alias must not cancel out a section that
    names one doctor in full. Names are compared without honorifics or bracketed notes.
    """
    full = [(d, _person(names[0])) for d, names in doctors if names and len(_person(names[0])) > 2]
    aliases = [
        (d, _person(alias))
        for d, names in doctors
        for alias in names[1:]
        if len(_person(alias)) > 2
    ]
    tagged = []
    for section in sections:
        if section.doctor_id is None:
            haystack = f" {_person(f'{section.heading} {section.text}')} "
            matches = {d for d, name in full if f" {name} " in haystack}
            matches = matches or {d for d, name in aliases if f" {name} " in haystack}
            if len(matches) == 1:
                section = section.model_copy(update={"doctor_id": matches.pop()})
        tagged.append(section)
    return tuple(tagged)


def excerpt(text: str, wanted: Sequence[str]) -> str:
    """Return a bounded window of the section around the first matching term."""
    if len(text) <= EXCERPT_CHARS:
        return text
    lowered = text.casefold()
    positions = [lowered.find(word) for word in wanted if lowered.find(word) >= 0]
    start = max(0, min(positions, default=0) - 120)
    window = text[start : start + EXCERPT_CHARS]
    return ("…" if start else "") + window.rsplit(" ", 1)[0].strip() + "…"


class DocumentIndex:
    """Lexical index over published sections. Built once per session, no I/O per lookup."""

    def __init__(self, sections: Sequence[DocumentSection]) -> None:
        self.sections = tuple(sections)
        self._terms: list[dict[str, int]] = []
        self._documents: dict[str, int] = {}
        for section in self.sections:
            counts: dict[str, int] = {}
            for word in tokens(f"{section.heading} {section.text}"):
                counts[word] = counts.get(word, 0) + 1
            for word in tokens(" ".join((*section.keywords, section.topic, section.heading))):
                counts[word] = counts.get(word, 0) + 3
            # Callers rarely use the document's exact word form, so short prefixes also count.
            for word in list(counts):
                if len(word) >= 5:
                    counts["~" + word[:5]] = counts.get("~" + word[:5], 0) + counts[word]
            self._terms.append(counts)
            for word in counts:
                self._documents[word] = self._documents.get(word, 0) + 1
        total = len(self.sections) or 1
        self._weight = {w: 1.0 + (total / c) ** 0.5 for w, c in self._documents.items()}

    def _lexical(
        self, wanted: Sequence[str], doctor_id: UUID | None, topic: str
    ) -> list[tuple[float, DocumentSection]]:
        subject = normalize(topic)
        # A word carried by most sections ("clinic") cannot make a section relevant on its own.
        common = max(1.0, len(self.sections) * 0.5)
        scored: list[tuple[float, DocumentSection]] = []
        for counts, section in zip(self._terms, self.sections, strict=True):
            if doctor_id is not None and section.doctor_id not in (None, doctor_id):
                continue
            matches = [
                word
                for word in wanted
                if counts.get(word) or (len(word) >= 5 and counts.get("~" + word[:5]))
            ]
            if not any(
                self._documents.get(word, self._documents.get("~" + word[:5], 0)) <= common
                for word in matches
            ):
                continue
            score = sum(self._weight.get(w, 1.0) * min(counts.get(w, 0), 3) for w in wanted)
            score += sum(
                0.4 * self._weight.get("~" + w[:5], 1.0) * min(counts.get("~" + w[:5], 0), 3)
                for w in wanted
                if len(w) >= 5
            )
            if score > 0:
                # The requested topic is a hint from the caller's words, never a hard filter.
                if subject and subject in normalize(f"{section.topic} {section.heading}"):
                    score *= 1.5
                if doctor_id is not None and section.doctor_id == doctor_id:
                    score *= 1.25
                scored.append((score / (len(counts) + 20) ** 0.5, section))
        scored.sort(key=lambda row: (-row[0], str(row[1].id)))
        best = scored[0][0] if scored else 0.0
        return [row for row in scored[:5] if row[0] >= best * 0.45]

    def topics(self) -> list[str]:
        """Short human-readable document headings for the clinic overview tool."""
        return list(dict.fromkeys(section.heading or section.topic for section in self.sections))

    def search(
        self,
        question: str,
        *,
        doctor_id: UUID | None = None,
        topic: str = "",
        semantic: Sequence[UUID] = (),
        limit: int = 4,
    ) -> list[tuple[float, DocumentSection]]:
        """Rank sections by fusing the lexical ranking with optional semantic section ids."""
        wanted = tokens(question)
        if not self.sections or (not wanted and not semantic):
            return []
        lexical = [section for _, section in self._lexical(wanted, doctor_id, topic)]
        known = {s.id: s for s in self.sections}
        nearest = [
            known[i]
            for i in dict.fromkeys(semantic)
            if i in known
            and (doctor_id is None or known[i].doctor_id in (None, doctor_id))
        ][:5]
        # Reciprocal rank fusion: either retriever can carry a section, neither can dominate.
        fused: dict[UUID, float] = {}
        for ranking in (lexical, nearest):
            for position, section in enumerate(ranking):
                fused[section.id] = fused.get(section.id, 0.0) + 1.0 / (RRF_K + position + 1)
        order = sorted(fused.items(), key=lambda row: (-row[1], str(row[0])))
        return [(score, known[i]) for i, score in order[:limit]]

"""Request and response models for knowledge endpoints."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from praxima.modules import knowledge

Status = Literal["draft", "published", "archived"]
Phrase = Annotated[str, Field(min_length=1, max_length=500)]
Keyword = Annotated[str, Field(min_length=1, max_length=60)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    category: str | None
    status: str
    published_version_id: uuid.UUID | None
    published_version_no: int | None
    row_version: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: knowledge.DocumentView) -> "DocumentOut":
        return cls(**view.__dict__)


class VersionOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    status: str
    title: str
    original_filename: str
    extraction_warnings: list[str]
    row_version: int
    created_at: datetime
    published_at: datetime | None

    @classmethod
    def of(cls, view: knowledge.VersionView) -> "VersionOut":
        return cls(**view.__dict__)


class DocumentDetailOut(BaseModel):
    document: DocumentOut
    versions: list[VersionOut]


class SectionOut(BaseModel):
    id: uuid.UUID
    position: int
    heading: str | None
    text: str
    entity_id: uuid.UUID | None
    keywords: list[str]


class VersionDetailOut(BaseModel):
    version: VersionOut
    sections: list[SectionOut]

    @classmethod
    def of(
        cls, version: knowledge.VersionView, sections: list[knowledge.SectionView]
    ) -> "VersionDetailOut":
        return cls(
            version=VersionOut.of(version),
            sections=[SectionOut(**s.__dict__) for s in sections],
        )


class SectionIn(Strict):
    heading: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    entity_id: uuid.UUID | None = None
    keywords: list[Keyword] = Field(default=[], max_length=20)


class SectionsIn(Strict):
    row_version: int = Field(ge=1)
    sections: list[SectionIn] = Field(min_length=1, max_length=200)

    def drafts(self) -> list[knowledge.SectionDraft]:
        return [
            knowledge.SectionDraft(s.heading, s.text, s.entity_id, tuple(s.keywords))
            for s in self.sections
        ]


class VersionStatusIn(Strict):
    row_version: int = Field(ge=1)
    status: Literal["published", "rejected", "archived"]


class FaqOut(BaseModel):
    id: uuid.UUID
    category: str | None
    canonical_question: str
    alternative_phrasings: list[str]
    approved_answer: str
    entity_id: uuid.UUID | None
    publication_status: str
    row_version: int
    created_at: datetime

    @classmethod
    def of(cls, view: knowledge.FaqView) -> "FaqOut":
        return cls(**view.__dict__)


class FaqIn(Strict):
    canonical_question: str = Field(min_length=1, max_length=500)
    approved_answer: str = Field(min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=80)
    alternative_phrasings: list[Phrase] = Field(default=[], max_length=20)
    entity_id: uuid.UUID | None = None

    def draft(self) -> knowledge.FaqDraft:
        values = self.model_dump()
        values["alternative_phrasings"] = tuple(values["alternative_phrasings"])
        return knowledge.FaqDraft(**values)


class FaqPatch(Strict):
    row_version: int = Field(ge=1)
    canonical_question: str | None = Field(default=None, min_length=1, max_length=500)
    approved_answer: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=80)
    alternative_phrasings: list[Phrase] | None = Field(default=None, max_length=20)
    publication_status: Status | None = None

    def changes(self) -> knowledge.FaqChanges:
        return knowledge.FaqChanges(
            canonical_question=self.canonical_question,
            approved_answer=self.approved_answer,
            category=self.category,
            alternative_phrasings=tuple(self.alternative_phrasings)
            if self.alternative_phrasings is not None
            else None,
        )


class AnnouncementOut(BaseModel):
    id: uuid.UUID
    kind: str
    public_message: str
    internal_note: str | None
    entity_id: uuid.UUID | None
    location_entity_id: uuid.UUID | None
    priority: int
    starts_at: datetime
    ends_at: datetime
    publication_status: str
    row_version: int
    created_at: datetime

    @classmethod
    def of(cls, view: knowledge.AnnouncementView) -> "AnnouncementOut":
        return cls(**view.__dict__)


class AnnouncementIn(Strict):
    kind: str = Field(min_length=2, max_length=63)
    public_message: str = Field(min_length=1, max_length=2000)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    internal_note: str | None = Field(default=None, max_length=2000)
    entity_id: uuid.UUID | None = None
    location_entity_id: uuid.UUID | None = None
    priority: int = Field(default=100, ge=0, le=1000)

    def draft(self) -> knowledge.AnnouncementDraft:
        return knowledge.AnnouncementDraft(**self.model_dump())


class AnnouncementPatch(Strict):
    row_version: int = Field(ge=1)
    public_message: str | None = Field(default=None, min_length=1, max_length=2000)
    internal_note: str | None = Field(default=None, max_length=2000)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    publication_status: Status | None = None

    def changes(self) -> knowledge.AnnouncementChanges:
        return knowledge.AnnouncementChanges(
            **self.model_dump(exclude={"row_version", "publication_status"})
        )


class SearchHitOut(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    heading: str | None
    text: str
    entity_id: uuid.UUID | None

    @classmethod
    def of(cls, hit: knowledge.SearchHit) -> "SearchHitOut":
        return cls(**hit.__dict__)

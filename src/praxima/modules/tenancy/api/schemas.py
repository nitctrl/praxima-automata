"""Request and response models for workspace endpoints."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from praxima.modules import tenancy

# BCP 47 style, e.g. "hi-IN", "en".
LanguageCode = Annotated[
    str, Field(min_length=2, max_length=20, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
]


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    slug: str
    name: str
    industry: str
    pack_key: str
    pack_version: str
    timezone: str
    default_language: str
    supported_languages: list[str]
    status: str
    row_version: int
    organization_status: str  # "suspended": members can't use it until reactivated

    @classmethod
    def of(cls, view: tenancy.WorkspaceView) -> "WorkspaceOut":
        return cls(**view.__dict__)


class WorkspacePatch(BaseModel):
    """Partial update. `row_version` must be the value last read (409 if it changed)."""

    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    default_language: LanguageCode | None = None
    supported_languages: list[LanguageCode] | None = Field(
        default=None, min_length=1, max_length=20
    )

    def changes(self) -> tenancy.WorkspaceChanges:
        return tenancy.WorkspaceChanges(
            name=self.name,
            timezone=self.timezone,
            default_language=self.default_language,
            supported_languages=tuple(self.supported_languages)
            if self.supported_languages is not None
            else None,
        )


class WorkspaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str = Field(min_length=1, max_length=200)
    pack_key: str = Field(min_length=2, max_length=63)
    pack_version: str = Field(min_length=5, max_length=20)
    timezone: str = Field(min_length=1, max_length=64)
    default_language: LanguageCode
    supported_languages: list[LanguageCode] = Field(min_length=1, max_length=20)
    industry: str = Field(default="", max_length=100)

    def draft(self) -> tenancy.WorkspaceDraft:
        values = self.model_dump()
        values["supported_languages"] = tuple(values["supported_languages"])
        return tenancy.WorkspaceDraft(**values)


class PackOut(BaseModel):
    key: str
    version: str
    name: str
    industry: str


class OrganizationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")


class OrganizationOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str


class EntityLabelOut(BaseModel):
    name: str
    plural_name: str


class AgentDefaultsOut(BaseModel):
    greeting_message: str
    emergency_message: str
    fallback_message: str


class PackBookingOut(BaseModel):
    """What can be booked by the slot (absent when the pack has no booking)."""

    label: str
    plural_label: str
    resource_types: list[str]
    subject_types: list[str]
    slot_minutes: int


class PackUpgradeOut(BaseModel):
    """A newer available version of the installed pack; `problems` empty = can upgrade."""

    version: str
    problems: list[str]


class PackDetailsOut(BaseModel):
    """The workspace's domain pack vocabulary, so UIs never hard-code an industry."""

    key: str
    version: str
    name: str
    industry: str
    entity_labels: dict[str, EntityLabelOut]
    document_categories: list[str]
    announcement_kinds: list[str]
    callback_kind: str | None
    agent_defaults: AgentDefaultsOut | None
    booking: PackBookingOut | None = None
    upgrades: list[PackUpgradeOut]


class PackUpgradeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=20)
    row_version: int = Field(ge=1)


# Platform admin area.
PackKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
PackVersionText = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=20)]


class PackVersionAdminOut(BaseModel):
    version: str
    status: str
    released_at: datetime


class PackCatalogOut(BaseModel):
    """A shipped or registered pack. Organizations see only `available` versions."""

    key: str
    name: str
    industry: str
    shipped_version: str | None
    shipped_invalid: bool
    needs_registration: bool
    files_changed_without_bump: bool
    versions: list[PackVersionAdminOut]

    @classmethod
    def of(cls, entry: tenancy.PackCatalogEntry) -> "PackCatalogOut":
        return cls(
            key=entry.key,
            name=entry.name,
            industry=entry.industry,
            shipped_version=entry.shipped_version,
            shipped_invalid=entry.shipped_invalid,
            needs_registration=entry.needs_registration,
            files_changed_without_bump=entry.files_changed_without_bump,
            versions=[
                PackVersionAdminOut(version=v.version, status=v.status, released_at=v.released_at)
                for v in entry.versions
            ],
        )


class PackRegistrationOut(BaseModel):
    key: str
    version: str
    registered: bool  # false: this version was already registered with the same files


class PackStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "deprecated", "withdrawn"]


class OrganizationWorkspaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    pack_key: str
    pack_version: str
    status: str


class OrganizationSummaryOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    created_at: datetime
    member_count: int
    workspaces: list[OrganizationWorkspaceOut]

    @classmethod
    def of(cls, org: tenancy.OrganizationSummary, member_count: int) -> "OrganizationSummaryOut":
        return cls(
            id=org.id,
            slug=org.slug,
            name=org.name,
            status=org.status,
            created_at=org.created_at,
            member_count=member_count,
            workspaces=[
                OrganizationWorkspaceOut(
                    id=w.id,
                    slug=w.slug,
                    name=w.name,
                    pack_key=w.pack_key,
                    pack_version=w.pack_version,
                    status=w.status,
                )
                for w in org.workspaces
            ],
        )


class OrganizationStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "suspended"]

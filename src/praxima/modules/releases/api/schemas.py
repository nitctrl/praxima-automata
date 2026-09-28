"""Request and response models for agent release endpoints."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from praxima.modules import releases


class PreviewOut(BaseModel):
    """What publishing now would do. Publish by sending back `digest`."""

    digest: str
    summary: dict[str, Any]
    changes: dict[str, Any]
    warnings: list[str]
    live_version_no: int | None
    unchanged: bool

    @classmethod
    def of(cls, preview: releases.Preview) -> "PreviewOut":
        return cls(**preview.__dict__)


class ReleaseOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    version_no: int
    status: str
    schema_version: int
    pack_key: str
    pack_version: str
    digest: str
    summary: dict[str, Any]
    source_release_id: uuid.UUID | None
    published_by: uuid.UUID | None
    published_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, view: releases.ReleaseView) -> "ReleaseOut":
        return cls(**view.__dict__)


class ReleaseDetailOut(BaseModel):
    release: ReleaseOut
    snapshot: dict[str, Any]


class PublishIn(BaseModel):
    """Either publish the previewed `digest`, or roll back to `source_release_id`."""

    model_config = ConfigDict(extra="forbid")

    digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_release_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "PublishIn":
        if (self.digest is None) == (self.source_release_id is None):
            raise ValueError("Send either digest (publish) or source_release_id (roll back).")
        return self

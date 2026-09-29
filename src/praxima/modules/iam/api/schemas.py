"""Request and response models for identity and access endpoints."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from praxima.modules import iam

Role = Literal["owner", "admin", "manager", "staff", "viewer"]
WorkspaceRole = Literal["manager", "staff", "viewer"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignIn(Strict):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=1, max_length=1024)


class Registration(Strict):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    # 72 bytes is bcrypt's limit at the identity provider.
    password: str = Field(min_length=8, max_length=72)
    display_name: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None


class MembershipOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    workspace_id: uuid.UUID | None
    role: str

    @classmethod
    def of(cls, view: iam.MembershipView) -> "MembershipOut":
        return cls(
            id=view.id,
            organization_id=view.organization_id,
            workspace_id=view.workspace_id,
            role=view.role,
        )


class SessionOut(BaseModel):
    """The signed-in user. `csrf` must be sent as X-CSRF-Token on every write."""

    csrf: str
    user: UserOut
    memberships: list[MembershipOut]
    is_platform_admin: bool = False  # may open the platform admin area


class RegistrationOut(BaseModel):
    """`signed_in` carries the new session; `confirmation_required` means check your email."""

    status: Literal["signed_in", "confirmation_required"]
    session: SessionOut | None = None


class MemberOut(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    status: str
    workspace_id: uuid.UUID | None
    created_at: datetime

    @classmethod
    def of(cls, view: iam.MemberView) -> "MemberOut":
        return cls(**view.__dict__)


class OrganizationRoleIn(Strict):
    role: Role


class WorkspaceRoleIn(Strict):
    role: WorkspaceRole


class GrantOut(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    workspace_id: uuid.UUID | None
    role: str


class PlatformAdminIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")


class PlatformAdminOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    granted_at: datetime

    @classmethod
    def of(cls, view: iam.PlatformAdminView) -> "PlatformAdminOut":
        return cls(
            user_id=view.user_id,
            email=view.email,
            display_name=view.display_name,
            granted_at=view.granted_at,
        )

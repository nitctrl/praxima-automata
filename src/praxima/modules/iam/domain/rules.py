"""Roles and permissions: one place maps each named action to the lowest role allowed."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from praxima.shared.errors import PermissionDenied

ROLE_RANK = {"viewer": 1, "staff": 2, "manager": 3, "admin": 4, "owner": 5}
ORG_WIDE_ROLES = frozenset({"owner", "admin"})

PERMISSIONS = {
    "workspace:read": "viewer",
    "workspace:update": "manager",
    "members:read": "manager",
    "members:manage": "admin",
    "workspace:create": "admin",
    "organization:update": "owner",
    "packs:install": "admin",
    "catalog:read": "viewer",
    "catalog:write": "manager",
    "catalog:publish": "manager",
    "agents:read": "viewer",
    "agents:write": "manager",
    "phone_numbers:manage": "admin",
    "knowledge:read": "viewer",
    "knowledge:write": "manager",
    "knowledge:publish": "manager",
    "crm:read": "viewer",
    "crm:write": "staff",
    "crm:assign": "manager",
    "pii:reveal": "staff",
    "pii:erase": "admin",
}


@dataclass(frozen=True)
class Principal:
    """An authenticated person (resolved from a verified login)."""

    user_id: uuid.UUID
    email: str
    display_name: str | None


@dataclass(frozen=True)
class Actor:
    """Who performs an action, with their effective role in the current scope."""

    user_id: uuid.UUID
    role: str | None
    is_platform_admin: bool = False


def effective_role(roles: Iterable[str]) -> str | None:
    """The strongest of several memberships (org-wide and workspace-specific)."""
    return max(roles, key=ROLE_RANK.__getitem__, default=None)


def allowed(actor: Actor, permission: str) -> bool:
    if actor.is_platform_admin:
        return True
    return actor.role is not None and ROLE_RANK[actor.role] >= ROLE_RANK[PERMISSIONS[permission]]


def require(actor: Actor, permission: str) -> None:
    if not allowed(actor, permission):
        raise PermissionDenied()


def require_can_grant(actor: Actor, role: str) -> None:
    """Nobody grants a role above their own; only owners create owners."""
    if actor.is_platform_admin:
        return
    if actor.role is None or ROLE_RANK[role] > ROLE_RANK[actor.role]:
        raise PermissionDenied("You can't grant a role higher than your own.")

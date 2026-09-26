"""Identity and access: users, identities, memberships, roles, API keys.

Public interface for other modules and entrypoints.
"""

from praxima.modules.iam.application.selectors import (
    MembershipView,
    MemberView,
    active_memberships,
    is_platform_admin,
    members_page,
    role_in,
)
from praxima.modules.iam.application.services import (
    VerifiedIdentity,
    login,
    revoke_membership,
    revoke_membership_of,
    set_membership,
)
from praxima.modules.iam.domain.rules import Actor, Principal, allowed, require

__all__ = [
    "Actor",
    "MemberView",
    "MembershipView",
    "Principal",
    "VerifiedIdentity",
    "active_memberships",
    "allowed",
    "is_platform_admin",
    "login",
    "members_page",
    "require",
    "revoke_membership",
    "revoke_membership_of",
    "role_in",
    "set_membership",
]

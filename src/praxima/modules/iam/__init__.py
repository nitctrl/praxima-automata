"""Identity and access: users, identities, memberships, roles, API keys.

Public interface for other modules and entrypoints.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.iam.application.selectors import (
        MembershipView,
        MemberView,
        PlatformAdminView,
        active_memberships,
        is_platform_admin,
        member_counts,
        members_page,
        platform_admins,
        role_in,
    )
    from praxima.modules.iam.application.services import (
        VerifiedIdentity,
        grant_platform_admin,
        login,
        revoke_membership,
        revoke_membership_of,
        revoke_platform_admin,
        set_membership,
    )
    from praxima.modules.iam.domain.rules import (
        Actor,
        Principal,
        allowed,
        require,
    )

_EXPORTS = {
    "MembershipView": "praxima.modules.iam.application.selectors",
    "MemberView": "praxima.modules.iam.application.selectors",
    "PlatformAdminView": "praxima.modules.iam.application.selectors",
    "active_memberships": "praxima.modules.iam.application.selectors",
    "is_platform_admin": "praxima.modules.iam.application.selectors",
    "member_counts": "praxima.modules.iam.application.selectors",
    "members_page": "praxima.modules.iam.application.selectors",
    "platform_admins": "praxima.modules.iam.application.selectors",
    "role_in": "praxima.modules.iam.application.selectors",
    "VerifiedIdentity": "praxima.modules.iam.application.services",
    "grant_platform_admin": "praxima.modules.iam.application.services",
    "login": "praxima.modules.iam.application.services",
    "revoke_membership": "praxima.modules.iam.application.services",
    "revoke_membership_of": "praxima.modules.iam.application.services",
    "revoke_platform_admin": "praxima.modules.iam.application.services",
    "set_membership": "praxima.modules.iam.application.services",
    "Actor": "praxima.modules.iam.domain.rules",
    "Principal": "praxima.modules.iam.domain.rules",
    "allowed": "praxima.modules.iam.domain.rules",
    "require": "praxima.modules.iam.domain.rules",
}
__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)
__all__ = [
    "Actor",
    "MemberView",
    "MembershipView",
    "PlatformAdminView",
    "Principal",
    "VerifiedIdentity",
    "active_memberships",
    "allowed",
    "grant_platform_admin",
    "is_platform_admin",
    "login",
    "member_counts",
    "members_page",
    "platform_admins",
    "require",
    "revoke_membership",
    "revoke_membership_of",
    "revoke_platform_admin",
    "role_in",
    "set_membership",
]

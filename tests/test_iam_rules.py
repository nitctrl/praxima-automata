"""Role and permission rules (pure, no database)."""

import pytest

from praxima.modules.iam.domain.rules import (
    PERMISSIONS,
    ROLE_RANK,
    Actor,
    allowed,
    effective_role,
    require,
    require_can_grant,
)
from praxima.shared.errors import PermissionDenied
from praxima.shared.kernel.ids import new_id


def actor(role: str | None, admin: bool = False) -> Actor:
    return Actor(new_id(), role, admin)


def test_effective_role_is_the_strongest():
    assert effective_role(["viewer", "manager", "staff"]) == "manager"
    assert effective_role([]) is None


@pytest.mark.parametrize(
    ("role", "permission", "expected"),
    [
        ("viewer", "workspace:read", True),
        ("viewer", "workspace:update", False),
        ("manager", "workspace:update", True),
        ("manager", "members:manage", False),
        ("admin", "members:manage", True),
        ("admin", "organization:update", False),
        ("owner", "organization:update", True),
        (None, "workspace:read", False),
    ],
)
def test_permissions(role, permission, expected):
    assert allowed(actor(role), permission) is expected


def test_platform_admin_is_allowed_everything():
    assert all(allowed(actor(None, admin=True), p) for p in PERMISSIONS)


def test_every_permission_names_a_real_role():
    assert set(PERMISSIONS.values()) <= set(ROLE_RANK)


def test_nobody_grants_above_their_own_role():
    require_can_grant(actor("admin"), "manager")
    require_can_grant(actor("owner"), "owner")
    with pytest.raises(PermissionDenied):
        require_can_grant(actor("admin"), "owner")
    with pytest.raises(PermissionDenied):
        require(actor("staff"), "members:manage")

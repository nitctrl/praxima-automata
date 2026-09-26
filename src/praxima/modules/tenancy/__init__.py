"""Tenant hierarchy: organizations, workspaces and installed domain packs.

Public interface for other modules and entrypoints.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.tenancy.application.selectors import (
        PackVersionView,
        WorkspaceView,
        available_packs,
        get_workspace,
        installed_pack,
        organization_exists,
        organization_of,
        visible_workspaces,
    )
    from praxima.modules.tenancy.application.services import (
        WorkspaceChanges,
        WorkspaceDraft,
        create_organization,
        create_workspace,
        update_workspace,
    )

_EXPORTS = {
    "PackVersionView": "praxima.modules.tenancy.application.selectors",
    "WorkspaceView": "praxima.modules.tenancy.application.selectors",
    "available_packs": "praxima.modules.tenancy.application.selectors",
    "get_workspace": "praxima.modules.tenancy.application.selectors",
    "installed_pack": "praxima.modules.tenancy.application.selectors",
    "organization_exists": "praxima.modules.tenancy.application.selectors",
    "organization_of": "praxima.modules.tenancy.application.selectors",
    "visible_workspaces": "praxima.modules.tenancy.application.selectors",
    "WorkspaceChanges": "praxima.modules.tenancy.application.services",
    "WorkspaceDraft": "praxima.modules.tenancy.application.services",
    "create_organization": "praxima.modules.tenancy.application.services",
    "create_workspace": "praxima.modules.tenancy.application.services",
    "update_workspace": "praxima.modules.tenancy.application.services",
}
__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)
__all__ = [
    "PackVersionView",
    "WorkspaceChanges",
    "WorkspaceDraft",
    "WorkspaceView",
    "available_packs",
    "create_organization",
    "create_workspace",
    "get_workspace",
    "installed_pack",
    "organization_exists",
    "organization_of",
    "update_workspace",
    "visible_workspaces",
]

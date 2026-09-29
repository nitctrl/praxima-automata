"""Tenant hierarchy: organizations, workspaces and installed domain packs.

Public interface for other modules and entrypoints.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.tenancy.application.selectors import (
        OrganizationSummary,
        PackCatalogEntry,
        PackUpgradeView,
        PackVersionView,
        RegisteredPackView,
        WorkspaceView,
        available_packs,
        get_workspace,
        installed_pack,
        organization_exists,
        organization_of,
        organization_status,
        organization_summary,
        organizations_page,
        pack_catalog,
        pack_upgrades,
        visible_workspaces,
    )
    from praxima.modules.tenancy.application.services import (
        WorkspaceChanges,
        WorkspaceDraft,
        create_organization,
        create_own_organization,
        create_workspace,
        register_shipped_pack,
        set_organization_status,
        set_pack_status,
        update_workspace,
        upgrade_workspace_pack,
    )

_EXPORTS = {
    "OrganizationSummary": "praxima.modules.tenancy.application.selectors",
    "PackCatalogEntry": "praxima.modules.tenancy.application.selectors",
    "PackUpgradeView": "praxima.modules.tenancy.application.selectors",
    "PackVersionView": "praxima.modules.tenancy.application.selectors",
    "RegisteredPackView": "praxima.modules.tenancy.application.selectors",
    "WorkspaceView": "praxima.modules.tenancy.application.selectors",
    "available_packs": "praxima.modules.tenancy.application.selectors",
    "get_workspace": "praxima.modules.tenancy.application.selectors",
    "installed_pack": "praxima.modules.tenancy.application.selectors",
    "organization_exists": "praxima.modules.tenancy.application.selectors",
    "organization_of": "praxima.modules.tenancy.application.selectors",
    "organization_status": "praxima.modules.tenancy.application.selectors",
    "organization_summary": "praxima.modules.tenancy.application.selectors",
    "organizations_page": "praxima.modules.tenancy.application.selectors",
    "pack_catalog": "praxima.modules.tenancy.application.selectors",
    "pack_upgrades": "praxima.modules.tenancy.application.selectors",
    "visible_workspaces": "praxima.modules.tenancy.application.selectors",
    "WorkspaceChanges": "praxima.modules.tenancy.application.services",
    "WorkspaceDraft": "praxima.modules.tenancy.application.services",
    "create_organization": "praxima.modules.tenancy.application.services",
    "create_own_organization": "praxima.modules.tenancy.application.services",
    "create_workspace": "praxima.modules.tenancy.application.services",
    "register_shipped_pack": "praxima.modules.tenancy.application.services",
    "set_organization_status": "praxima.modules.tenancy.application.services",
    "set_pack_status": "praxima.modules.tenancy.application.services",
    "update_workspace": "praxima.modules.tenancy.application.services",
    "upgrade_workspace_pack": "praxima.modules.tenancy.application.services",
}
__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)
__all__ = [
    "OrganizationSummary",
    "PackCatalogEntry",
    "PackUpgradeView",
    "PackVersionView",
    "RegisteredPackView",
    "WorkspaceChanges",
    "WorkspaceDraft",
    "WorkspaceView",
    "available_packs",
    "create_organization",
    "create_own_organization",
    "create_workspace",
    "get_workspace",
    "installed_pack",
    "organization_exists",
    "organization_of",
    "organization_status",
    "organization_summary",
    "organizations_page",
    "pack_catalog",
    "pack_upgrades",
    "register_shipped_pack",
    "set_organization_status",
    "set_pack_status",
    "update_workspace",
    "upgrade_workspace_pack",
    "visible_workspaces",
]

"""Immutable agent releases: build, preview, publish and rollback.

Public interface for other modules and entrypoints. Lazy: the voice worker imports
`releases.domain.snapshot` and must not load the platform code exported here.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.releases.application.selectors import (
        ReleaseView,
        get_release,
        live_release,
        releases_page,
    )
    from praxima.modules.releases.application.services import (
        Preview,
        build_snapshot,
        preview_release,
        publish_release,
        rollback_release,
    )

_EXPORTS = {
    "ReleaseView": "praxima.modules.releases.application.selectors",
    "get_release": "praxima.modules.releases.application.selectors",
    "live_release": "praxima.modules.releases.application.selectors",
    "releases_page": "praxima.modules.releases.application.selectors",
    "Preview": "praxima.modules.releases.application.services",
    "build_snapshot": "praxima.modules.releases.application.services",
    "preview_release": "praxima.modules.releases.application.services",
    "publish_release": "praxima.modules.releases.application.services",
    "rollback_release": "praxima.modules.releases.application.services",
}

__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)
__all__ = [
    "Preview",
    "ReleaseView",
    "build_snapshot",
    "get_release",
    "live_release",
    "preview_release",
    "publish_release",
    "releases_page",
    "rollback_release",
]

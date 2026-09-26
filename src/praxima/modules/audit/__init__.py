"""Append-only security and business audit trail.

Public interface: other modules call `record(...)` inside their own transaction.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.audit.application.services import (
        record,
    )

_EXPORTS = {
    "record": "praxima.modules.audit.application.services",
}
__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)
__all__ = [
    "record",
]

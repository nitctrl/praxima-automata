"""Append-only security and business audit trail.

Public interface: other modules call `record(...)` inside their own transaction.
"""

from praxima.modules.audit.application.services import record

__all__ = ["record"]

"""Scheduling: bookable slots from published hours, and bookings of them.

Public interface for other modules and entrypoints.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.scheduling.application.selectors import (
        BookingConfig,
        BookingView,
        SettingsView,
        booking_config,
        booking_settings,
        open_slots_for,
    )
    from praxima.modules.scheduling.application.services import (
        BookingDraft,
        RevealedBooking,
        SettingsChanges,
        book,
        booking_detail,
        cancel,
        confirm,
        list_bookings,
        reschedule,
        reveal,
        to_confirm,
        update_settings,
    )

_SELECTORS = "praxima.modules.scheduling.application.selectors"
_SERVICES = "praxima.modules.scheduling.application.services"
_EXPORTS = {
    "BookingConfig": _SELECTORS,
    "BookingView": _SELECTORS,
    "SettingsView": _SELECTORS,
    "booking_config": _SELECTORS,
    "booking_settings": _SELECTORS,
    "open_slots_for": _SELECTORS,
    "BookingDraft": _SERVICES,
    "RevealedBooking": _SERVICES,
    "SettingsChanges": _SERVICES,
    "book": _SERVICES,
    "booking_detail": _SERVICES,
    "cancel": _SERVICES,
    "confirm": _SERVICES,
    "list_bookings": _SERVICES,
    "reschedule": _SERVICES,
    "reveal": _SERVICES,
    "to_confirm": _SERVICES,
    "update_settings": _SERVICES,
}
__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)
__all__ = [
    "BookingConfig",
    "BookingDraft",
    "BookingView",
    "RevealedBooking",
    "SettingsChanges",
    "SettingsView",
    "book",
    "booking_config",
    "booking_detail",
    "booking_settings",
    "cancel",
    "confirm",
    "list_bookings",
    "open_slots_for",
    "reschedule",
    "reveal",
    "to_confirm",
    "update_settings",
]

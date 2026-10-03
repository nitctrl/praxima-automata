"""Scheduling: bookable slots from published hours, and bookings of them.

Public interface for other modules and entrypoints.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.scheduling.application.selectors import (
        BookingConfig,
        BookingView,
        CalendarConnectionView,
        SettingsView,
        booking_config,
        booking_settings,
        calendar_connection,
        open_slots_for,
    )
    from praxima.modules.scheduling.application.services import (
        BookingDraft,
        JobFailed,
        RevealedBooking,
        SettingsChanges,
        book,
        booking_detail,
        cancel,
        complete_calendar_connection,
        confirm,
        disconnect_calendar,
        list_bookings,
        note_job_failure,
        queue_busy_syncs,
        reschedule,
        reveal,
        run_job,
        start_calendar_connection,
        to_confirm,
        update_settings,
    )

_SELECTORS = "praxima.modules.scheduling.application.selectors"
_SERVICES = "praxima.modules.scheduling.application.services"
_EXPORTS = {
    "BookingConfig": _SELECTORS,
    "calendar_connection": _SELECTORS,
    "CalendarConnectionView": _SELECTORS,
    "BookingView": _SELECTORS,
    "SettingsView": _SELECTORS,
    "booking_config": _SELECTORS,
    "booking_settings": _SELECTORS,
    "open_slots_for": _SELECTORS,
    "BookingDraft": _SERVICES,
    "note_job_failure": _SERVICES,
    "queue_busy_syncs": _SERVICES,
    "start_calendar_connection": _SERVICES,
    "run_job": _SERVICES,
    "disconnect_calendar": _SERVICES,
    "complete_calendar_connection": _SERVICES,
    "JobFailed": _SERVICES,
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
    "CalendarConnectionView",
    "JobFailed",
    "RevealedBooking",
    "SettingsChanges",
    "SettingsView",
    "book",
    "booking_config",
    "booking_detail",
    "booking_settings",
    "calendar_connection",
    "cancel",
    "complete_calendar_connection",
    "confirm",
    "disconnect_calendar",
    "list_bookings",
    "note_job_failure",
    "open_slots_for",
    "queue_busy_syncs",
    "reschedule",
    "reveal",
    "run_job",
    "start_calendar_connection",
    "to_confirm",
    "update_settings",
]

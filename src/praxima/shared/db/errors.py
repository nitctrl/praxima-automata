"""Map database errors to application errors without leaking SQL, values or constraint text."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm.exc import StaleDataError

from praxima.shared.errors import Conflict, PermissionDenied, Unavailable, ValidationFailed

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"
CHECK_VIOLATION = "23514"
NOT_NULL_VIOLATION = "23502"
EXCLUSION_VIOLATION = "23P01"  # e.g. overlapping validity windows
INSUFFICIENT_PRIVILEGE = "42501"  # includes "new row violates row-level security policy"


@contextmanager
def translate_db_errors(
    duplicate: str = "This already exists.", reference: str = "A referenced item doesn't exist."
) -> Iterator[None]:
    """Wrap a flush/commit: constraint and RLS failures become safe, typed errors."""
    try:
        yield
    except StaleDataError:
        raise Conflict("This item was changed by someone else. Reload and try again.") from None
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None)
        if state in (UNIQUE_VIOLATION, EXCLUSION_VIOLATION):
            raise Conflict(duplicate) from None
        if state == FOREIGN_KEY_VIOLATION:
            raise ValidationFailed(reference) from None
        if state in (CHECK_VIOLATION, NOT_NULL_VIOLATION):
            raise ValidationFailed() from None
        if state == INSUFFICIENT_PRIVILEGE:
            raise PermissionDenied() from None
        raise Unavailable() from None

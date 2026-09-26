"""Application errors. Messages are shown to clients: keep them safe, never include values."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class FieldError:
    """One invalid input field. `message` must not echo the submitted value (may be PII)."""

    field: str
    message: str


class AppError(Exception):
    """Base for every error the API maps to a response (entrypoints/http/errors.py)."""

    status: int = 500
    slug: str = "internal"
    title: str = "Internal error"
    default_detail: str = "Something went wrong. Try again later."

    def __init__(self, detail: str | None = None, *, errors: Sequence[FieldError] = ()) -> None:
        self.detail = detail or self.default_detail
        self.errors = tuple(errors)
        super().__init__(self.detail)


class Unauthenticated(AppError):
    status, slug, title = 401, "unauthenticated", "Unauthenticated"
    default_detail = "Sign in to continue."


class PermissionDenied(AppError):
    status, slug, title = 403, "forbidden", "Forbidden"
    default_detail = "You don't have access to this action."


class NotFound(AppError):
    status, slug, title = 404, "not-found", "Not found"
    default_detail = "The requested resource was not found."


class Conflict(AppError):
    status, slug, title = 409, "conflict", "Conflict"
    default_detail = "The resource changed or already exists. Reload and try again."


class ValidationFailed(AppError):
    status, slug, title = 422, "validation-failed", "Validation failed"
    default_detail = "Some fields are invalid."


class RateLimited(AppError):
    status, slug, title = 429, "rate-limited", "Too many requests"
    default_detail = "Too many requests. Try again shortly."


class Unavailable(AppError):
    status, slug, title = 503, "unavailable", "Service unavailable"
    default_detail = "A required service is temporarily unavailable. Try again."

"""Global error handlers: every error becomes a Problem Details response, never a traceback."""

import logging
from collections.abc import Mapping, Sequence
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from praxima.entrypoints.http.responses import PROBLEM_JSON, Problem, ProblemField
from praxima.shared import context
from praxima.shared.errors import AppError, FieldError, ValidationFailed

logger = logging.getLogger(__name__)
TYPE_BASE = "https://praxima.dev/problems/"


def problem_response(
    status: int,
    slug: str,
    title: str,
    detail: str,
    errors: Sequence[FieldError] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        type=TYPE_BASE + slug,
        title=title,
        status=status,
        detail=detail,
        request_id=context.request_id.get(),
        errors=[ProblemField(field=e.field, message=e.message) for e in errors],
    )
    return JSONResponse(
        body.model_dump(),
        status_code=status,
        media_type=PROBLEM_JSON,
        headers=dict(headers) if headers else None,
    )


def from_app_error(error: AppError) -> JSONResponse:
    return problem_response(error.status, error.slug, error.title, error.detail, error.errors)


def internal_error() -> JSONResponse:
    return from_app_error(AppError())


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return from_app_error(exc)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = []
    for item in exc.errors():
        # Drop the location prefix ("body", "query" ...); never include the submitted input.
        location = [str(part) for part in item.get("loc", ())][1:] or ["request"]
        fields.append(FieldError(".".join(location), str(item.get("msg", "Invalid value."))))
    return from_app_error(ValidationFailed(errors=fields))


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = HTTPStatus(exc.status_code)
    # Legacy endpoints raise HTTPException with safe string details; keep those messages.
    detail = exc.detail if isinstance(exc.detail, str) else status.description
    slug = status.phrase.lower().replace(" ", "-")
    return problem_response(status.value, slug, status.phrase, detail, headers=exc.headers)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)

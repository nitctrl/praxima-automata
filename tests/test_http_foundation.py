"""Global error handling, response shapes, request ids and cursor paging (CLAUDE.md §6)."""

import logging
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from sqlalchemy import DateTime
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from praxima.entrypoints.http.deps import Paging
from praxima.entrypoints.http.responses import PROBLEM_JSON, Page
from praxima.entrypoints.http.setup import install
from praxima.shared.db.pagination import PageRequest, PageResult, decode_cursor, encode_cursor
from praxima.shared.errors import Conflict, FieldError, NotFound, ValidationFailed


class _Body(BaseModel):
    name: str = Field(max_length=5)
    phone: str = Field(pattern=r"^\+[0-9]+$")


def _app() -> FastAPI:
    app = FastAPI()
    install(app)

    @app.get("/missing")
    async def missing() -> None:
        raise NotFound("Workspace not found.")

    @app.get("/conflict")
    async def conflict() -> None:
        raise Conflict(errors=[FieldError("slug", "Already taken.")])

    @app.post("/items")
    async def create(body: _Body) -> _Body:
        return body

    @app.get("/legacy")
    async def legacy() -> None:
        raise HTTPException(403, "Operation denied or invalid; no changes confirmed.")

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret patient name in message")

    @app.get("/database-down")
    async def database_down() -> None:
        raise OperationalError("SELECT 1", None, ConnectionRefusedError("host db.secret:5432"))

    @app.get("/items")
    async def items(page: Paging) -> Page[int]:
        return Page.build(PageResult([1, 2], "abc"), [1, 2], page.limit)

    return app


client = TestClient(_app())


def _problem(response, status):  # type: ignore[no-untyped-def]
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert set(body) == {"type", "title", "status", "detail", "request_id", "errors"}
    assert body["status"] == status
    assert body["request_id"] == response.headers["x-request-id"]
    return body


def test_app_error_becomes_problem_details():
    body = _problem(client.get("/missing"), 404)
    assert body["detail"] == "Workspace not found."
    assert body["type"].endswith("/not-found")
    assert _problem(client.get("/conflict"), 409)["errors"] == [
        {"field": "slug", "message": "Already taken."}
    ]


def test_validation_lists_fields_without_echoing_input():
    response = client.post("/items", json={"name": "far too long", "phone": "98765 43210"})
    body = _problem(response, 422)
    assert {e["field"] for e in body["errors"]} == {"name", "phone"}
    assert "98765" not in response.text and "far too long" not in response.text


def test_legacy_http_exception_keeps_safe_detail():
    body = _problem(client.get("/legacy"), 403)
    assert body["detail"] == "Operation denied or invalid; no changes confirmed."


def test_unknown_route_is_a_problem():
    _problem(client.get("/nope"), 404)


def test_crash_is_generic_and_logs_only_the_type(caplog):
    with caplog.at_level(logging.ERROR):
        response = client.get("/crash")
    body = _problem(response, 500)
    assert body["detail"] == "Something went wrong. Try again later."
    assert "secret" not in response.text
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text


def test_database_outage_is_a_503_without_details(caplog):
    with caplog.at_level(logging.ERROR):
        response = client.get("/database-down")
    body = _problem(response, 503)
    assert body["detail"] == "The database is unavailable. Try again shortly."
    assert "db.secret" not in response.text
    assert "OperationalError" in caplog.text and "db.secret" not in caplog.text


def test_request_id_is_echoed_only_when_valid():
    assert (
        client.get("/missing", headers={"X-Request-ID": "abc12345"}).headers["x-request-id"]
        == "abc12345"
    )
    generated = client.get("/missing", headers={"X-Request-ID": "bad id\n"}).headers["x-request-id"]
    assert generated != "bad id\n" and len(generated) == 32


def test_list_shape_and_paging_limits():
    assert client.get("/items?limit=2").json() == {
        "data": [1, 2],
        "page": {"limit": 2, "next_cursor": "abc"},
    }
    assert _problem(client.get("/items?limit=101"), 422)["errors"][0]["field"] == "limit"
    with pytest.raises(ValidationFailed):
        PageRequest(limit=0)


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "rows"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def test_cursor_round_trip_and_tampering():
    keys = (_Row.created_at, _Row.id)
    values = [datetime(2026, 9, 26, 10, 30, tzinfo=timezone.utc), uuid.uuid4()]
    assert decode_cursor(encode_cursor(values), keys) == values
    for bad in ("not-base64!!", encode_cursor(["x"]), encode_cursor(["2026-01-01", "not-a-uuid"])):
        with pytest.raises(ValidationFailed, match="Invalid page cursor"):
            decode_cursor(bad, keys)

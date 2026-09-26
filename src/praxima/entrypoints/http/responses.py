"""Standard response shapes: `Page[T]` for lists and RFC 9457 `Problem` for every error."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from praxima.shared.db.pagination import PageResult

T = TypeVar("T")

PROBLEM_JSON = "application/problem+json"


class PageInfo(BaseModel):
    limit: int
    next_cursor: str | None


class Page(BaseModel, Generic[T]):
    """A list response: `{"data": [...], "page": {"limit": 50, "next_cursor": ...}}`."""

    data: list[T]
    page: PageInfo

    @classmethod
    def build(cls, result: PageResult, items: list[T], limit: int) -> "Page[T]":
        return cls(data=items, page=PageInfo(limit=limit, next_cursor=result.next_cursor))


class ProblemField(BaseModel):
    field: str
    message: str


class Problem(BaseModel):
    """RFC 9457 Problem Details. `detail` is always safe to show to users."""

    model_config = ConfigDict(json_schema_extra={"description": "Every error response."})

    type: str
    title: str
    status: int
    detail: str
    request_id: str | None = None
    errors: list[ProblemField] = []

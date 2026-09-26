"""Keyset pagination for selectors: stable, index-friendly, never OFFSET."""

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from praxima.shared.errors import ValidationFailed

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


@dataclass(frozen=True)
class PageRequest:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_LIMIT:
            raise ValidationFailed(f"limit must be between 1 and {MAX_LIMIT}.")


@dataclass(frozen=True)
class PageResult:
    items: list[Any]
    next_cursor: str | None


def encode_cursor(values: Sequence[Any]) -> str:
    raw = json.dumps([v.isoformat() if isinstance(v, datetime) else str(v) for v in values])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, keys: Sequence[InstrumentedAttribute[Any]]) -> list[Any]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(raw, list) or len(raw) != len(keys):
            raise ValueError
        return [_parse(value, key) for value, key in zip(raw, keys, strict=True)]
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ValidationFailed("Invalid page cursor. Start again from the first page.") from None


def _parse(value: object, key: InstrumentedAttribute[Any]) -> Any:
    if not isinstance(value, str):
        raise ValueError
    kind = key.type.python_type
    if kind is datetime:
        return datetime.fromisoformat(value)
    if kind is uuid.UUID:
        return uuid.UUID(value)
    return kind(value)


def _key_value(row: Any, key: InstrumentedAttribute[Any]) -> Any:
    """The key's value from a row of columns or of ORM entities (possibly joined)."""
    if key.key in row._mapping:
        return row._mapping[key.key]
    owner = key.parent.class_  # the mapped class, also for aliased keys
    for value in row:
        if isinstance(value, owner):
            return getattr(value, key.key)
    raise KeyError(f"page key {key.key} is not selected")


async def fetch_page(
    session: AsyncSession,
    statement: Select[Any],
    keys: Sequence[InstrumentedAttribute[Any]],
    page: PageRequest,
) -> PageResult:
    """Run `statement` newest-first by `keys` (e.g. created_at, id) and return one page.

    `keys` must be unique together and indexed. Rows selecting one ORM entity come back as
    entities; column selects come back as rows.
    """
    if page.cursor:
        statement = statement.where(tuple_(*keys) < tuple_(*decode_cursor(page.cursor, keys)))
    statement = statement.order_by(*(key.desc() for key in keys)).limit(page.limit + 1)
    rows = list((await session.execute(statement)).all())
    more = len(rows) > page.limit
    rows = rows[: page.limit]
    next_cursor = encode_cursor([_key_value(rows[-1], k) for k in keys]) if more else None
    entity = bool(rows) and len(rows[0]) == 1 and hasattr(rows[0][0], "__mapper__")
    return PageResult([row[0] for row in rows] if entity else rows, next_cursor)

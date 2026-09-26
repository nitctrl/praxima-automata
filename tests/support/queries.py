"""Count SQL statements, to prove list endpoints and selectors have no N+1 queries."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


@contextmanager
def count_queries(engine: AsyncEngine) -> Iterator[list[str]]:
    """Collect statements run on `engine`, ignoring the per-transaction scope settings."""
    statements: list[str] = []

    def before(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "set_config" not in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)

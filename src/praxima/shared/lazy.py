"""Lazy public interfaces: a module's names load on first use, not when the package imports.

The voice worker imports a few legacy files inside module packages. Lazy exports keep it
from also loading the platform code those packages expose. Standard library only.
"""

import importlib
from collections.abc import Callable
from typing import Any


def lazy_exports(
    package: str, exports: dict[str, str]
) -> tuple[Callable[[str], Any], Callable[[], list[str]]]:
    """Return a module-level (__getattr__, __dir__) pair; `exports` maps name → module."""

    def __getattr__(name: str) -> Any:
        module = exports.get(name)
        if module is None:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        return getattr(importlib.import_module(module), name)

    def __dir__() -> list[str]:
        return sorted(exports)

    return __getattr__, __dir__

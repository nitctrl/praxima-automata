"""Import every module's ORM models so `Base.metadata` is complete for Alembic and tests."""

import importlib
import importlib.util
import pkgutil

import praxima.modules


def import_models() -> list[str]:
    """Import `praxima.modules.<module>.infrastructure.models` wherever it exists."""
    loaded = []
    for module in pkgutil.iter_modules(praxima.modules.__path__):
        name = f"praxima.modules.{module.name}.infrastructure.models"
        if importlib.util.find_spec(name) is not None:
            importlib.import_module(name)
            loaded.append(name)
    return loaded

"""Enforce the CLAUDE.md dependency rules; known step-1 exceptions are listed exactly."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
IO_LIBRARIES = ("psycopg", "httpx", "qdrant_client", "google", "livekit", "fastembed", "sqlalchemy")

# Temporary violations left by the step-1 move (no behaviour change). Remove an entry when
# the code is fixed; the test fails if an entry is stale or a new violation appears.
KNOWN = {
    ("praxima.ai.worker.main", "praxima.ai.dev.sip_test"),
    ("praxima.ai.legacy.agent_knowledge", "praxima.dev.development"),
    # The dashboard's legacy grounded answers use the legacy voice prompt; both go together.
    ("praxima.integrations.llm.gemini", "praxima.ai.legacy.prompting"),
}
# The voice agent's release path imports only `praxima.ai`, `praxima.contracts` and
# `praxima.shared` (it meets the backend through the release snapshot and the database's
# runtime functions). The legacy path and dev tools still lean on backend modules; they are
# exempt until the legacy path is removed.
AI_LEGACY = ("praxima.ai.legacy", "praxima.ai.dev")
AI_ALLOWED = ("praxima.ai", "praxima.contracts", "praxima.shared")


def _imports() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in (SRC / "praxima").rglob("*.py"):
        name = ".".join(path.relative_to(SRC).with_suffix("").parts).removesuffix(".__init__")
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
        graph[name] = found
    return graph


def _violations() -> set[tuple[str, str]]:
    bad = set()
    for module, targets in _imports().items():
        for target in targets:
            prod = not module.startswith(("praxima.dev", "praxima.ai.dev"))
            if not module.startswith("praxima.ai") and target.startswith("praxima.ai"):
                bad.add((module, target))  # the backend never imports the voice agent
            if (
                module.startswith("praxima.ai")
                and not module.startswith(AI_LEGACY)
                and target.startswith("praxima.")
                and not target.startswith(AI_ALLOWED)
            ):
                bad.add((module, target))  # the agent's release path never imports the backend
            if prod and target.startswith("praxima.ai.dev"):
                bad.add((module, target))  # rule 7: production never imports dev tools
            in_modules = module.startswith("praxima.modules.")
            domain = ".domain." in f"{module}." and in_modules
            if prod and target.startswith("praxima.dev"):
                bad.add((module, target))  # rule 7: production never imports dev/
            router = in_modules and ".api." in f"{module}."
            http_plumbing = target.startswith("praxima.entrypoints.http")
            below = target.startswith(("praxima.ai", "praxima.entrypoints"))
            if in_modules and below and not (router and http_plumbing):
                bad.add((module, target))  # modules sit below the runtime and entrypoints
            if (
                target.startswith("praxima.entrypoints")
                and not module.startswith("praxima.entrypoints")
                and not (router and http_plumbing)
            ):
                bad.add((module, target))  # only module routers use the shared HTTP plumbing
            if module.startswith("praxima.entrypoints.http") and ".api." in f"{target}.":
                if not module.endswith(".v1"):
                    bad.add((module, target))  # plumbing never imports routers (no cycles)
            if domain and target.split(".")[0] in IO_LIBRARIES:
                bad.add((module, target))  # rule 1: domain does no I/O
            if domain and any(
                f".{layer}" in target for layer in (".application", ".infrastructure", ".api")
            ):
                bad.add((module, target))  # rule 1: domain depends on nothing above it
            api_side = module == "praxima.entrypoints.api" or ".api." in f"{module}."
            if api_side and target.startswith("livekit"):
                bad.add((module, target))  # rule 5
            if module.startswith("praxima.ai") and target.startswith("fastapi"):
                bad.add((module, target))  # rule 5
            layer = f"{module}."
            # entrypoints.http owns the per-request session that routers receive.
            orm_allowed = module.startswith(
                ("praxima.shared.db", "praxima.entrypoints.http")
            ) or any(f".{name}." in layer for name in ("application", "infrastructure"))
            if target.split(".")[0] in ("sqlalchemy", "alembic") and not orm_allowed:
                bad.add((module, target))  # §4.4 rule 3: SQLAlchemy only below the routers
            http_free = in_modules and any(f".{n}." in layer for n in ("application", "domain"))
            if http_free and target.split(".")[0] in ("fastapi", "starlette"):
                bad.add((module, target))  # services/selectors raise errors, never HTTP
    return bad


def test_no_new_dependency_rule_violations():
    assert _violations() - KNOWN == set()


def test_known_exceptions_are_not_stale():
    assert KNOWN - _violations() == set()


def test_no_legacy_clinic_package():
    assert not (SRC / "clinic").exists()
    assert "clinic" not in {m.split(".")[0] for targets in _imports().values() for m in targets}


PLATFORM_CODE = (
    ".infrastructure.models",
    ".application.services",
    ".application.selectors",
    ".api.",
    "praxima.packs",
    "praxima.shared.db.base",
    "praxima.shared.db.engine",
    "praxima.entrypoints.http",
)


def test_voice_worker_loads_no_platform_code():
    """The voice agent must not depend on the new platform code (it can't break it)."""
    import json
    import os
    import subprocess
    import sys

    probe = (
        "import json, sys; import praxima.ai.worker.main; "
        "prefixes = ('praxima', 'sqlalchemy'); "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith(prefixes))))"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    leaked = [m for m in loaded if m.startswith("sqlalchemy") or any(p in m for p in PLATFORM_CODE)]
    assert leaked == []


def test_module_interfaces_are_lazy_and_complete():
    import importlib

    for path in sorted((SRC / "praxima" / "modules").glob("*/__init__.py")):
        package = importlib.import_module(f"praxima.modules.{path.parent.name}")
        exports = getattr(package, "_EXPORTS", None)
        if exports is None:
            continue
        assert package.__all__ == sorted(exports)
        typed = {}
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING":
                for imp in node.body:
                    assert isinstance(imp, ast.ImportFrom)
                    typed.update({alias.name: imp.module for alias in imp.names})
        assert typed == exports, path  # type-checker imports match the lazy map
        for name in exports:
            assert getattr(package, name) is not None

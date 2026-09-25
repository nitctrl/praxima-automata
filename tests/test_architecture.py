"""Enforce the CLAUDE.md dependency rules; known step-1 exceptions are listed exactly."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
IO_LIBRARIES = ("psycopg", "httpx", "qdrant_client", "google", "livekit", "fastembed")

# Temporary violations left by the step-1 move (no behaviour change). Remove an entry when
# the code is fixed; the test fails if an entry is stale or a new violation appears.
KNOWN = {
    ("praxima.entrypoints.voice_worker", "praxima.dev.sip_test"),
    ("praxima.runtime.tools.agent_knowledge", "praxima.dev.development"),
    ("praxima.modules.engagement.domain.requests", "praxima.runtime.policy.safety"),
    ("praxima.modules.engagement.application.sessions", "praxima.runtime.policy.safety"),
}


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
            prod = not module.startswith("praxima.dev")
            in_modules = module.startswith("praxima.modules.")
            domain = ".domain." in f"{module}." and in_modules
            if prod and target.startswith("praxima.dev"):
                bad.add((module, target))  # rule 7: production never imports dev/
            if in_modules and target.startswith(("praxima.runtime", "praxima.entrypoints")):
                bad.add((module, target))  # modules sit below the runtime and entrypoints
            if target.startswith("praxima.entrypoints") and not module.startswith(
                "praxima.entrypoints"
            ):
                bad.add((module, target))  # nothing depends on an entrypoint
            if domain and target.split(".")[0] in IO_LIBRARIES:
                bad.add((module, target))  # rule 1: domain does no I/O
            if domain and any(
                f".{layer}" in target for layer in (".application", ".infrastructure", ".api")
            ):
                bad.add((module, target))  # rule 1: domain depends on nothing above it
            api_side = module == "praxima.entrypoints.api" or ".api." in f"{module}."
            if api_side and target.startswith("livekit"):
                bad.add((module, target))  # rule 5
            if module.startswith("praxima.runtime") and target.startswith("fastapi"):
                bad.add((module, target))  # rule 5
    return bad


def test_no_new_dependency_rule_violations():
    assert _violations() - KNOWN == set()


def test_known_exceptions_are_not_stale():
    assert KNOWN - _violations() == set()


def test_no_legacy_clinic_package():
    assert not (SRC / "clinic").exists()
    assert "clinic" not in {m.split(".")[0] for targets in _imports().values() for m in targets}

"""Migration tooling guard tests; never connect or provision secrets."""

import importlib.util
from pathlib import Path

import pytest

from praxima.shared.db.settings import ConfigurationError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/database.py"
SPEC = importlib.util.spec_from_file_location("database_script", SCRIPT)
assert SPEC and SPEC.loader
database = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(database)


def test_project_confirmation_required():
    with pytest.raises(ConfigurationError):
        database.settings("")


def test_migration_drift_blocks_mutation():
    class Connection:
        def execute(self, query):
            return self

        def fetchall(self):
            return [("unknown.sql", "wrong-checksum")]

    with pytest.raises(ConfigurationError):
        database.verify_migration_state(Connection())


def test_provision_refuses_existing_login_before_creating_secret():
    class Connection:
        def execute(self, query):
            return self

        def fetchone(self):
            return (True,)

    with pytest.raises(ConfigurationError):
        database.provision_runtime(Connection(), None)

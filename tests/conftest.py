from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values

from praxima.shared.db.settings import ConfigurationError, DatabaseSettings

ROOT = Path(__file__).resolve().parents[1]


def pytest_addoption(parser):
    parser.addoption(
        "--development-project", default="", help="Explicit Supabase DEV project opt-in"
    )


@pytest.fixture(scope="session")
def database_settings(request):
    confirmation = request.config.getoption("--development-project")
    if not confirmation:
        pytest.skip("Real database test requires --development-project=<dedicated DEV project ref>")
    env = dotenv_values(ROOT / ".env")
    if confirmation != env.get("SUPABASE_PROJECT_REF"):
        pytest.fail("Development project confirmation does not match local configuration.")
    try:
        return DatabaseSettings.validate(env.get("MIGRATION_DATABASE_URL") or "", confirmation)
    except ConfigurationError:
        pytest.fail("Invalid explicit development database configuration.")


@pytest.fixture(scope="session")
def runtime_settings(database_settings):
    env = dotenv_values(ROOT / ".env.runtime")
    return DatabaseSettings.validate(env.get("DATABASE_URL") or "", database_settings.project_ref)


@pytest.fixture(scope="module")
def admin_connection(database_settings):
    try:
        conn = psycopg.connect(database_settings.dsn, connect_timeout=10, autocommit=True)
    except psycopg.Error:
        pytest.fail("Development database connection failed; details suppressed.")
    with conn:
        with conn.transaction(force_rollback=True):
            conn.execute("SET LOCAL statement_timeout = '10s'")
            yield conn


@pytest.fixture
def db(admin_connection):
    with admin_connection.transaction(force_rollback=True):
        yield admin_connection

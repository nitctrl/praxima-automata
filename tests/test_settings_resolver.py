import asyncio
import inspect
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from praxima.modules.agents.application.resolver import (
    ClinicResolver,
    ClinicUnavailable,
    ConfigurationRepository,
    InboundDestination,
)
from praxima.shared.db.settings import ConfigurationError, DatabaseSettings

REF = "a" * 20
VALID = f"postgresql://postgres.{REF}:example@aws-0-example.pooler.supabase.com:5432/postgres?sslmode=require"


def test_valid_settings_redact_connection():
    config = DatabaseSettings.validate(VALID, REF)
    assert "example@" not in repr(config)
    assert "postgresql" not in repr(config)


@pytest.mark.parametrize(
    "suffix",
    [
        "&host=",
        "&user=",
        "&dbname=",
        "&password=",
        "&options=",
        "&sslmode=require",
        "&sslmode=",
        "&port=",
    ],
)
def test_blank_or_duplicate_libpq_overrides_rejected(suffix):
    with pytest.raises(ConfigurationError):
        DatabaseSettings.validate(VALID + suffix, REF)


@pytest.mark.parametrize(
    "dsn",
    [
        "",
        "postgresql://localhost/postgres",
        VALID.replace("5432", "6543"),
        VALID.replace("postgres?", "postgresappend%20?"),
        VALID.replace("require", "disable"),
        VALID.replace("example@", "bad@password@"),
        VALID.replace("example@", "%ZZ@"),
        VALID + "&host=localhost",
        VALID.replace(f"postgres.{REF}", "postgres.wrong"),
        VALID.replace("aws-0-example.pooler.supabase.com", "pooler.supabase.com.attacker.test"),
        VALID.replace("example@", "[password]@"),
    ],
)
def test_invalid_settings_fail_safely(dsn):
    with pytest.raises(ConfigurationError) as error:
        DatabaseSettings.validate(dsn, REF)
    assert "example@" not in str(error.value)


def test_unknown_destination_does_not_fallback():
    class MissingRepository:
        async def resolve(self, destination):
            return None

    with pytest.raises(ClinicUnavailable):
        asyncio.run(
            ClinicResolver(MissingRepository()).resolve(
                InboundDestination("+12025550101", "fixture-only", "test")
            )
        )


def test_scope_is_frozen_and_not_caller_selected():
    clinic_id = uuid4()

    class Repository:
        async def resolve(self, destination):
            return {
                "clinic_id": clinic_id,
                "phone_number_id": uuid4(),
                "configuration_version_id": uuid4(),
                "timezone": "Asia/Kolkata",
                "supported_languages": ["hi-IN"],
            }

    scope = asyncio.run(
        ClinicResolver(Repository()).resolve(
            InboundDestination("+12025550101", "fixture-only", "test")
        )
    )
    assert scope.clinic_id == clinic_id
    with pytest.raises(FrozenInstanceError):
        scope.clinic_id = uuid4()
    assert "clinic_id" not in inspect.signature(ClinicResolver.resolve).parameters
    assert "clinic_id" not in inspect.signature(ConfigurationRepository.load).parameters
    with pytest.raises(TypeError):
        InboundDestination("+12025550101", "fixture-only", clinic_id=uuid4())


@pytest.mark.parametrize(
    "number,trunk",
    [
        ("caller says Clinic B", "trunk"),
        ("123", "trunk"),
        ("+12025550101", ""),
        ("+12025550101", "x' OR 1=1"),
    ],
)
def test_untrusted_routing_fields_rejected(number, trunk):
    with pytest.raises(ClinicUnavailable):
        InboundDestination(number, trunk)

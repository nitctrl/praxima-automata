"""Tenant resolution on the live call path: trusted SIP destination only, fail closed."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from livekit import rtc
from test_structured_knowledge import content  # noqa: F401

from clinic import agent_knowledge
from clinic.cache import clear_local
from clinic.ingress import trusted_destination
from clinic.resolver import ClinicScope, ClinicUnavailable, InboundDestination
from clinic.settings import DatabaseSettings

SIP = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
CLINIC_B = UUID(int=0xB)
VERSION_B = UUID(int=0xB1)


def attributes(**overrides):
    values = {
        "sip.trunkPhoneNumber": "+918000000002",
        "sip.trunkID": "ST_clinicB",
        "sip.callID": "SCL_abc123",
        "sip.phoneNumber": "+919999999999",
    }
    values.update(overrides)
    return values


def test_destination_comes_from_livekit_trunk_attributes(monkeypatch):
    monkeypatch.delenv("TELEPHONY_PROVIDER", raising=False)
    ingress = trusted_destination(SIP, attributes())
    assert ingress.destination == InboundDestination("+918000000002", "ST_clinicB", "plivo")
    assert ingress.call_id == "SCL_abc123"


def test_caller_id_never_selects_the_clinic():
    one = trusted_destination(SIP, attributes(**{"sip.phoneNumber": "+911111111111"}))
    two = trusted_destination(SIP, attributes(**{"sip.phoneNumber": "+912222222222"}))
    assert one.destination == two.destination


@pytest.mark.parametrize(
    "kind,overrides",
    [
        (rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, {}),
        (SIP, {"sip.callID": ""}),
        (SIP, {"sip.callID": "bad id; DROP"}),
        (SIP, {"sip.trunkPhoneNumber": ""}),
        (SIP, {"sip.trunkPhoneNumber": "8000000002"}),
        (SIP, {"sip.trunkID": ""}),
        (SIP, {"sip.trunkID": "ST/../x"}),
    ],
)
def test_untrusted_or_incomplete_ingress_fails_closed(kind, overrides):
    with pytest.raises(ClinicUnavailable):
        trusted_destination(kind, attributes(**overrides))


def test_unknown_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("TELEPHONY_PROVIDER", "unknown")
    with pytest.raises(ClinicUnavailable):
        trusted_destination(SIP, attributes())


class FakeDatabase:
    def __init__(self, settings):
        self.closed = False

    async def open(self):
        pass

    def open_in_background(self):
        pass

    async def ready(self):
        pass

    async def close(self):
        self.closed = True


def patch_database(monkeypatch, resolve):
    monkeypatch.delenv("REDIS_URL", raising=False)
    clear_local()
    monkeypatch.setattr(DatabaseSettings, "validate", staticmethod(lambda *_: object()))
    monkeypatch.setattr(agent_knowledge, "RuntimeDatabase", FakeDatabase)
    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=resolve))
    monkeypatch.setattr(agent_knowledge, "ClinicResolver", lambda repository: resolver)
    monkeypatch.setattr(agent_knowledge, "VectorSearch", SimpleNamespace(
        from_environment=lambda: None
    ))
    return resolver


def test_sip_call_loads_the_resolved_clinic_and_pins_its_version(
    monkeypatch, tmp_path, content  # noqa: F811
):
    content["clinic_id"] = str(CLINIC_B)
    scope = ClinicScope(CLINIC_B, UUID(int=1), VERSION_B, "Asia/Kolkata", ("en-IN",))
    resolver = patch_database(monkeypatch, lambda destination: scope)

    class Repository:
        def __init__(self, database, given):
            assert given is scope

        async def load(self):
            return content

    monkeypatch.setattr(agent_knowledge, "ConfigurationRepository", Repository)
    destination = InboundDestination("+918000000002", "ST_clinicB")
    knowledge = asyncio.run(agent_knowledge.load_agent_knowledge(tmp_path, destination))
    resolver.resolve.assert_awaited_once_with(destination)
    assert knowledge.snapshot is not None
    assert knowledge.snapshot.clinic_id == CLINIC_B
    assert knowledge.version == VERSION_B


def test_snapshot_from_another_clinic_is_refused(monkeypatch, tmp_path, content):  # noqa: F811
    # Defense in depth: even a wrong row returned for the pinned version is not served.
    scope = ClinicScope(CLINIC_B, UUID(int=1), VERSION_B, "Asia/Kolkata", ("en-IN",))
    patch_database(monkeypatch, lambda destination: scope)

    class Repository:
        def __init__(self, database, given):
            pass

        async def load(self):
            return content  # belongs to a different clinic

    monkeypatch.setattr(agent_knowledge, "ConfigurationRepository", Repository)
    destination = InboundDestination("+918000000002", "ST_clinicB")
    knowledge = asyncio.run(agent_knowledge.load_agent_knowledge(tmp_path, destination))
    assert knowledge.snapshot is None
    assert len(knowledge.function_tools()) == 1  # no booking tools without a clinic


def test_unknown_number_gets_no_clinic_knowledge(monkeypatch, tmp_path):
    def unknown(destination):
        raise ClinicUnavailable("No active published clinic for this destination.")

    patch_database(monkeypatch, unknown)
    destination = InboundDestination("+918000000009", "ST_unknown")
    knowledge = asyncio.run(agent_knowledge.load_agent_knowledge(tmp_path, destination))
    assert knowledge.snapshot is None
    assert knowledge.failure == "not_configured"
    result = asyncio.run(knowledge.search_clinic_knowledge("who are the doctors"))
    assert result["status"] == "unavailable"


def test_plan_limit_is_reported_as_busy(monkeypatch, tmp_path):
    from clinic.calls import CallLimitReached

    def limited(destination):
        raise CallLimitReached("Clinic call limit reached")

    patch_database(monkeypatch, limited)
    destination = InboundDestination("+918000000002", "ST_clinicB")
    knowledge = asyncio.run(agent_knowledge.load_agent_knowledge(tmp_path, destination))
    assert knowledge.failure == "busy"


def test_database_outage_is_reported_as_unavailable(monkeypatch, tmp_path):
    def outage(destination):
        raise OSError("connection refused")

    patch_database(monkeypatch, outage)
    destination = InboundDestination("+918000000002", "ST_clinicB")
    knowledge = asyncio.run(agent_knowledge.load_agent_knowledge(tmp_path, destination))
    assert knowledge.failure == "unavailable"


def test_console_without_a_configured_clinic_gets_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("CONSOLE_CLINIC_ID", raising=False)
    patch_database(monkeypatch, lambda destination: None)
    knowledge = asyncio.run(agent_knowledge.load_agent_knowledge(tmp_path))
    assert knowledge.snapshot is None
    assert knowledge.failure == "not_configured"


def test_database_url_prefers_the_process_environment(monkeypatch, tmp_path):
    (tmp_path / ".env.runtime").write_text("DATABASE_URL=postgresql://file\n")
    seen = []
    monkeypatch.setattr(
        DatabaseSettings, "validate", staticmethod(lambda dsn, project: seen.append(dsn))
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://env")
    agent_knowledge.database_settings(tmp_path)
    monkeypatch.delenv("DATABASE_URL")
    agent_knowledge.database_settings(tmp_path)
    assert seen == ["postgresql://env", "postgresql://file"]

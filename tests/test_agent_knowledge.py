import asyncio
import copy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from test_structured_knowledge import content  # noqa: F401

from clinic.agent_knowledge import AgentKnowledge, load_agent_knowledge
from clinic.prompt import ENVIRONMENT
from clinic.resolver import ClinicUnavailable
from clinic.snapshot import Snapshot

CLINIC = UUID(int=0xA)


@pytest.fixture
def knowledge(content):  # noqa: F811
    content["clinic_id"] = str(CLINIC)
    return AgentKnowledge(Snapshot.model_validate(content), uuid4(), clinic=CLINIC)


def with_document(source):
    payload = copy.deepcopy(source)
    payload["schema_version"] = 3
    payload["clinic_id"] = str(CLINIC)
    payload["document_sections"] = [{
        "id": str(UUID(int=41)),
        "document_id": str(UUID(int=90)),
        "document_title": "mahto.md",
        "document_version": 1,
        "topic": "doctor_bio",
        "heading": "Dr Suresh Kumar Mahto",
        "text": "Dr Suresh Kumar Mahto has an MBBS and over 30 years of medical experience.",
        "doctor_id": None,
        "keywords": [],
    }]
    return payload


def test_agent_exposes_knowledge_booking_and_callback_tools(knowledge):
    tools = knowledge.function_tools()
    assert {tool.__name__ for tool in tools} == {
        "search_clinic_knowledge",
        "list_available_slots",
        "book_appointment_slot",
        "request_callback",
    }
    assert "search_clinic_knowledge" in knowledge.instructions
    assert "request_callback" in knowledge.instructions
    assert "WhatsApp" not in knowledge.instructions
    assert "out YYYY-MM-DD, HH:MM, references" in knowledge.instructions
    assert "Published doctors (use these exact names in tool calls):" in knowledge.instructions
    assert "specific date or date range" in knowledge.instructions
    assert "Current clinic-local time:" in knowledge.instructions
    assert "do not shorten or reinterpret" in knowledge.instructions
    assert "overrides conflicting document text" in knowledge.instructions
    assert "open/closed question has no date or time" in knowledge.instructions


def test_phone_prompt_avoids_language_announcements_filler_and_numbered_markup(knowledge):
    instructions = knowledge.instructions
    assert "Switch languages silently" in instructions
    assert "Call tools silently by default" in instructions
    assert "not repeated filler" in instructions
    assert "not Markdown, bullets or numbered lists" in instructions
    assert "First, cardiology. Second, dermatology." in instructions


def test_booking_tools_without_a_database_are_unavailable(knowledge):
    assert asyncio.run(knowledge.list_available_slots("2026-09-23"))["status"] == "unavailable"
    result = asyncio.run(knowledge.book_appointment_slot("2026-09-23", "10:00", "Asha Rao"))
    assert result["status"] == "unavailable"


def test_callback_without_a_call_record_is_unavailable(knowledge):
    knowledge.caller_number = "+919999999999"
    result = asyncio.run(knowledge.request_callback("Asha Rao"))
    assert result == {"status": "unavailable", "data": {}, "next_action": "ask_to_call_again"}
    assert asyncio.run(knowledge.request_callback(""))["next_action"] == "ask_name_and_number"


def test_callback_is_stored_encrypted_and_idempotent(knowledge, monkeypatch):
    from clinic.privacy import PiiCipher

    stored = []

    class Record:
        clinic_id = CLINIC
        session_id = UUID(int=5)

        async def create_callback(self, request_id, name, phone, key_version, details):
            stored.append((request_id, name, phone, key_version, details))
            return request_id

    async def record():
        return Record()

    knowledge.cipher = PiiCipher({"v1": b"k" * 32}, "v1")
    knowledge.caller_number = "+919999999999"
    monkeypatch.setattr(knowledge, "call_record", record)
    first = asyncio.run(knowledge.request_callback("Asha  Rao", "appointment"))
    again = asyncio.run(knowledge.request_callback("Asha Rao", "appointment"))
    assert first["status"] == "success"
    assert first["data"]["confirmed_by_staff"] is False
    assert first == again
    request_id, name, phone, key_version, details = stored[0]
    assert b"Asha" not in name and b"9999" not in phone
    assert knowledge.cipher.decrypt(phone, "v1", CLINIC, request_id, "phone") == "+919999999999"
    assert details == {"reason_category": "appointment"}


def test_prompt_template_accepts_previous_worker_variable_names():
    rendered = ENVIRONMENT.get_template("agent_system_prompt.j2").render(
        clinic_name="Test clinic",
        timezone="Asia/Kolkata",
        supported_languages=["en-IN"],
        emergency_message="Call emergency services.",
        current_local_time="2026-09-22T18:00+05:30",
        quick_info=["Reception closes early."],
        doctors=[],
        services=[],
        locations=[],
    )
    assert "Reception closes early" in rendered
    assert "Published doctors (use these exact names in tool calls): none" in rendered


def test_only_documents_are_stable_rag_knowledge(content):  # noqa: F811
    knowledge = AgentKnowledge(
        Snapshot.model_validate(with_document(content)), uuid4(), clinic=CLINIC
    )

    async def exercise():
        removed = await knowledge.search_clinic_knowledge("Tell me about Dr Sharma")
        assert removed["status"] == "unavailable"
        assert "Published clinic information" not in str(removed)
        qualification = await knowledge.search_clinic_knowledge("Mahto qualification")
        assert "MBBS" in str(qualification)

    asyncio.run(exercise())


def test_jinja_prompt_includes_active_quick_daily_info(content):  # noqa: F811
    payload = copy.deepcopy(content)
    payload["clinic_id"] = str(CLINIC)
    payload["temporary_notices"] = [{
        "id": str(UUID(int=71)),
        "location_id": None,
        "doctor_id": None,
        "service_id": None,
        "notice_type": "information",
        "public_message": "Reception closes early today.",
        "starts_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2027-01-01T00:00:00+00:00",
        "priority": 50,
    }]
    knowledge = AgentKnowledge(Snapshot.model_validate(payload), uuid4(), clinic=CLINIC)
    assert "Published current and scheduled live updates" in knowledge.instructions
    assert "Reception closes early today" in knowledge.instructions
    result = asyncio.run(knowledge.search_clinic_knowledge("Does reception close early today?"))
    assert "Reception closes early today" in str(result)


def test_scheduled_live_update_is_searchable_before_it_starts(content):  # noqa: F811
    payload = with_document(content)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    end = start + timedelta(days=1)
    payload["temporary_notices"] = [{
        "id": str(UUID(int=72)),
        "location_id": None,
        "doctor_id": None,
        "service_id": None,
        "notice_type": "closure",
        "public_message": "Clinic will be closed because of Devi Pujan.",
        "starts_at": start.isoformat(),
        "expires_at": end.isoformat(),
        "priority": 100,
    }]
    knowledge = AgentKnowledge(Snapshot.model_validate(payload), uuid4(), clinic=CLINIC)
    assert "Published current and scheduled live updates" in knowledge.instructions
    assert "Devi Pujan" in knowledge.instructions
    local = start.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
    result = asyncio.run(knowledge.search_clinic_knowledge(
        f"Will the clinic be open on {local}?"
    ))
    passage = result["data"]["passages"][0]
    assert passage["source"] == "Live update"
    assert passage["heading"] == "Scheduled live update"
    assert "Devi Pujan" in passage["text"]
    assert local in passage["text"]


def test_other_clinic_rejected(content):  # noqa: F811
    with pytest.raises(ClinicUnavailable):
        AgentKnowledge(Snapshot.model_validate(content), clinic=CLINIC)


def test_snapshot_without_resolved_clinic_rejected(content):  # noqa: F811
    content["clinic_id"] = str(CLINIC)
    with pytest.raises(ClinicUnavailable):
        AgentKnowledge(Snapshot.model_validate(content))


def test_missing_configuration_returns_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async def exercise():
        knowledge = await load_agent_knowledge(tmp_path)
        assert knowledge.snapshot is None
        assert knowledge.failure == "unavailable"
        result = await knowledge.search_clinic_knowledge("who are the doctors")
        assert result["status"] == "unavailable"

    asyncio.run(exercise())


def test_snapshot_is_pinned(knowledge, content):  # noqa: F811
    content["name"] = "Changed unpublished name"
    assert knowledge.snapshot is not None
    assert knowledge.snapshot.name == "Fictional Clinic"
    # Booking needs a pool, but nothing here may reach the database on its own.
    assert knowledge.database is None

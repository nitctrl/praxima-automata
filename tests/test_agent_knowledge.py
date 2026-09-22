import asyncio
import copy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from test_structured_knowledge import content  # noqa: F401

from clinic.agent_knowledge import CLINIC, AgentKnowledge, load_agent_knowledge
from clinic.prompt import ENVIRONMENT
from clinic.resolver import ClinicUnavailable
from clinic.snapshot import Snapshot


@pytest.fixture
def knowledge(content):  # noqa: F811
    content["clinic_id"] = str(CLINIC)
    return AgentKnowledge(Snapshot.model_validate(content), uuid4())


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


def test_agent_exposes_one_rag_tool(knowledge):
    tools = knowledge.function_tools()
    assert len(tools) == 1
    assert "search_clinic_knowledge" in knowledge.instructions
    assert "specific date or date range" in knowledge.instructions
    assert "Current clinic-local time:" in knowledge.instructions
    assert "do not shorten or reinterpret" in knowledge.instructions
    assert "overrides conflicting document text" in knowledge.instructions
    assert "open/closed question has no date or time" in knowledge.instructions


def test_prompt_template_accepts_previous_worker_variable_names():
    rendered = ENVIRONMENT.get_template("agent_system_prompt.j2").render(
        clinic_name="Test clinic",
        timezone="Asia/Kolkata",
        supported_languages=["en-IN"],
        emergency_message="Call emergency services.",
        current_local_time="2026-09-22T18:00+05:30",
        quick_info=["Reception closes early."],
    )
    assert "Reception closes early" in rendered


def test_only_documents_are_stable_rag_knowledge(content):  # noqa: F811
    knowledge = AgentKnowledge(Snapshot.model_validate(with_document(content)), uuid4())

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
    knowledge = AgentKnowledge(Snapshot.model_validate(payload), uuid4())
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
    knowledge = AgentKnowledge(Snapshot.model_validate(payload), uuid4())
    assert "Published current and scheduled live updates" in knowledge.instructions
    assert "Devi Pujan" in knowledge.instructions
    result = asyncio.run(knowledge.search_clinic_knowledge(
        f"Will the clinic be open on {start.date().isoformat()}?"
    ))
    passage = result["data"]["passages"][0]
    assert passage["source"] == "Live update"
    assert passage["heading"] == "Scheduled live update"
    assert "Devi Pujan" in passage["text"]
    assert start.date().isoformat() in passage["text"]


def test_other_clinic_rejected(content):  # noqa: F811
    with pytest.raises(ClinicUnavailable):
        AgentKnowledge(Snapshot.model_validate(content))


def test_missing_configuration_returns_unavailable(tmp_path):
    async def exercise():
        knowledge = await load_agent_knowledge(tmp_path)
        assert knowledge.snapshot is None
        result = await knowledge.search_clinic_knowledge("who are the doctors")
        assert result["status"] == "unavailable"

    asyncio.run(exercise())


def test_snapshot_is_pinned(knowledge, content):  # noqa: F811
    content["name"] = "Changed unpublished name"
    assert knowledge.snapshot is not None
    assert knowledge.snapshot.name == "Fictional Clinic"
    assert not hasattr(knowledge, "database")

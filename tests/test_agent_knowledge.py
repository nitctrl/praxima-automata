import asyncio
import copy
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from test_structured_knowledge import content  # noqa: F401

from clinic.agent_knowledge import CLINIC, AgentKnowledge, load_agent_knowledge
from clinic.resolver import ClinicUnavailable
from clinic.snapshot import Snapshot


@pytest.fixture
def knowledge(content):  # noqa: F811
    content["clinic_id"] = str(CLINIC)
    result = AgentKnowledge(Snapshot.model_validate(content), uuid4())
    result._clock = lambda: datetime.fromisoformat("2026-09-21T05:00:00+00:00")
    return result


def test_doctor_clarification_then_schedule(knowledge):
    async def exercise():
        first = await knowledge.find_doctors(name="Sharma")
        assert first["status"] == "ambiguous"
        assert len(first["data"]["doctors"]) == 2
        answer = await knowledge.get_doctor_availability(doctor="Dr Anaya Sharma")
        assert answer["status"] == "success"
        assert answer["data"]["appointment_confirmed"] is False
        assert answer["data"]["hours"]
    asyncio.run(exercise())


def test_fees_location_service_and_faq(knowledge):
    async def exercise():
        fee = await knowledge.get_consultation_fee(doctor="Dr Anaya Sharma")
        assert fee["data"]["amount"] == "450.00"
        location = await knowledge.get_clinic_location()
        assert location["data"]["address"] == "Fictional address"
        info = await knowledge.get_clinic_information()
        assert info["data"]["approved_faqs"][0]["answer"].startswith("No.")
        assert info["data"]["services"][0]["name"] == "Consultation"
        assert (await knowledge.get_service_information("Consultation"))["status"] == "success"
    asyncio.run(exercise())


def test_other_clinic_rejected(content):  # noqa: F811
    with pytest.raises(ClinicUnavailable):
        AgentKnowledge(Snapshot.model_validate(content))


def test_missing_configuration_returns_unavailable_not_generic_facts(tmp_path):
    async def exercise():
        knowledge = await load_agent_knowledge(tmp_path)
        assert knowledge.snapshot is None
        result = await knowledge.get_doctor_availability("Sharma")
        assert result["status"] == "unavailable"
        assert (await knowledge.get_clinic_information())["status"] == "unavailable"
    asyncio.run(exercise())


def test_snapshot_is_pinned_and_not_a_live_database_dependency(knowledge, content):  # noqa: F811
    content["name"] = "Changed unpublished name"
    assert knowledge.snapshot.name == "Fictional Clinic"
    assert not hasattr(knowledge, "database")
    assert "session_id" not in knowledge.instructions


def sections(content):  # noqa: F811
    def make(number, doctor, heading, text):
        return {
            "id": str(UUID(int=number)),
            "document_id": str(UUID(int=90)),
            "document_title": "Clinic story",
            "document_version": 1,
            "topic": "about",
            "heading": heading,
            "text": text,
            "doctor_id": doctor,
            "keywords": [],
        }

    content["schema_version"] = 3
    content["document_sections"] = [
        make(41, str(UUID(int=2)), "Dr Anaya Sharma", "She trained in paediatrics for ten years."),
        make(42, str(UUID(int=3)), "Dr Dev Sharma", "He studied sports medicine abroad."),
        make(43, None, "Our story", "The clinic began in a two room building in 1998."),
    ]
    return content


@pytest.fixture
def prose(content):  # noqa: F811
    content = sections(copy.deepcopy(content))
    content["clinic_id"] = str(CLINIC)
    return AgentKnowledge(Snapshot.model_validate(content), uuid4())


def test_document_search_answers_background_questions(prose):
    async def exercise():
        story = await prose.search_clinic_documents("how did the clinic begin")
        assert story["status"] == "success"
        assert "two room" in story["data"]["passages"][0]["text"]
        assert story["data"]["passages"][0]["document"] == "Clinic story"
        overview = await prose.get_clinic_information()
        assert "Our story" in overview["data"]["document_topics"]
    asyncio.run(exercise())


def test_document_search_never_returns_another_doctors_background(prose):
    async def exercise():
        answer = await prose.search_clinic_documents("what is her training", doctor="Anaya")
        assert answer["status"] == "success"
        assert all("sports medicine" not in p["text"] for p in answer["data"]["passages"])
        assert answer["data"]["passages"][0]["doctor"] == "Dr Anaya Sharma"
        ambiguous = await prose.search_clinic_documents("training", doctor="Sharma")
        assert ambiguous["status"] == "ambiguous"
    asyncio.run(exercise())


def test_document_search_declines_instead_of_inventing(prose, knowledge):
    async def exercise():
        missing = await prose.search_clinic_documents("do you sell health insurance")
        assert missing["status"] == "unavailable"
        assert missing["data"]["reason"] == "insufficient_information"
        # A published version 2 configuration simply has no document knowledge.
        assert knowledge.snapshot.document_sections == ()
        assert (await knowledge.search_clinic_documents("our story"))["status"] == "unavailable"
        assert (await knowledge.get_clinic_location())["status"] == "success"
    asyncio.run(exercise())


def test_routing_and_speech_guardrails_are_instructed(prose):
    assert "search_clinic_documents" in prose.instructions
    assert "the structured tool wins" in prose.instructions
    assert prose.vectors is None  # Semantic fallback stays off until Qdrant is configured.
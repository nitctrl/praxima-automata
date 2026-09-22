"""One published hybrid-RRF knowledge tool for the voice receptionist."""

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import dotenv_values
from livekit.agents import function_tool

from clinic.db import RuntimeDatabase
from clinic.development import fixture_id
from clinic.prompt import render_prompt
from clinic.rag import HybridRetriever
from clinic.resolver import ClinicUnavailable
from clinic.snapshot import Snapshot
from clinic.vectors import VectorSearch

CLINIC = fixture_id("A")
logger = logging.getLogger(__name__)
KNOWLEDGE_LOAD_TIMEOUT_SECONDS = 20
KNOWLEDGE_CLOSE_TIMEOUT_SECONDS = 2


class AgentKnowledge:
    """Pinned public snapshot exposed to the model through one retrieval tool."""

    def __init__(self, snapshot: Snapshot | None, version: UUID | None = None) -> None:
        if snapshot is not None and snapshot.clinic_id != CLINIC:
            raise ClinicUnavailable("Wrong clinic")
        self.snapshot = snapshot
        self.version = version
        vectors = VectorSearch.from_environment() if snapshot is not None else None
        self.retriever = HybridRetriever(snapshot, version, vectors) if snapshot else None
        self.vectors = vectors
        self._warm: asyncio.Task[None] | None = None

    @property
    def instructions(self) -> str:
        if self.snapshot is None:
            return (
                "Clinic knowledge is unavailable. Do not invent clinic facts. "
                "Explain briefly that published information cannot be reached right now."
            )
        return render_prompt(self.snapshot)

    def function_tools(self) -> list[Any]:
        return [self.search_clinic_knowledge]

    @function_tool()
    async def search_clinic_knowledge(self, question: str) -> dict[str, object]:
        """Hybrid-search all published clinic facts and reviewed uploaded documents.

        Call this for every clinic-information question, including doctors, availability,
        hours, fees, locations, services, FAQs, qualifications, and daily changes.
        """
        if self.retriever is None:
            return {"status": "unavailable", "data": {"passages": []}}
        return await self.retriever.result(question)


async def load_agent_knowledge(root: Path) -> AgentKnowledge:
    """Load and pin the one published clinic snapshot for this call."""
    from clinic.settings import DatabaseSettings

    async def load() -> AgentKnowledge:
        project = dotenv_values(root / ".env").get("SUPABASE_PROJECT_REF") or ""
        runtime = dotenv_values(root / ".env.runtime")
        settings = DatabaseSettings.validate(runtime.get("DATABASE_URL") or "", project)
        database = RuntimeDatabase(settings)
        try:
            await database.open()
            async with database.connection(CLINIC) as conn:
                await conn.execute("SET TRANSACTION READ ONLY")
                rows = await (await conn.execute(
                    "SELECT id,snapshot FROM public.configuration_versions "
                    "WHERE clinic_id=%s AND status='published'", (CLINIC,),
                )).fetchall()
            if len(rows) != 1:
                raise ClinicUnavailable("One published configuration required")
            knowledge = AgentKnowledge(Snapshot.model_validate(rows[0]["snapshot"]), rows[0]["id"])
            if knowledge.vectors is not None:
                knowledge._warm = asyncio.create_task(knowledge.vectors.warm())
            return knowledge
        finally:
            try:
                await asyncio.wait_for(database.close(), KNOWLEDGE_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("Clinic knowledge database cleanup did not finish")

    try:
        return await asyncio.wait_for(load(), KNOWLEDGE_LOAD_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Clinic knowledge load failed (%s)", type(exc).__name__)
        return AgentKnowledge(None)

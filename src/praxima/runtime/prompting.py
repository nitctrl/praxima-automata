"""Jinja-rendered voice policy with published quick daily information."""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from praxima.modules.knowledge.application.retrieval import published_live_updates
from praxima.modules.releases.domain.snapshot import Snapshot

if TYPE_CHECKING:
    from praxima.modules.releases.domain.agent_snapshot import AgentSnapshot

PROMPT_VERSION = "clinic-hybrid-rag-v1"
TEMPLATES = Path(__file__).resolve().parents[1] / "packs" / "clinic" / "prompts"
ENVIRONMENT = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=False,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_prompt(snapshot: Snapshot) -> str:
    return ENVIRONMENT.get_template("agent_system_prompt.j2").render(
        clinic_name=snapshot.name,
        timezone=snapshot.timezone,
        supported_languages=snapshot.supported_languages,
        emergency_message=snapshot.emergency_message,
        live_updates=published_live_updates(snapshot),
        current_local_time=datetime.now(ZoneInfo(snapshot.timezone)).isoformat(timespec="minutes"),
    ).strip()


def render_release_prompt(snapshot: "AgentSnapshot", now: datetime | None = None) -> str:
    """System prompt for a call pinned to an agent release (schema version 4)."""
    from collections import Counter

    from praxima.runtime.release.lookup import live_update_lines

    local = (now or datetime.now(ZoneInfo(snapshot.workspace.timezone))).astimezone(
        ZoneInfo(snapshot.workspace.timezone)
    )
    counts = Counter(e.type.replace("_", " ") for e in snapshot.entities)
    directory = ", ".join(
        f"{n} {kind}{'' if n == 1 else 's'}" for kind, n in sorted(counts.items())
    )
    return ENVIRONMENT.get_template("release_system_prompt.j2").render(
        agent_name=snapshot.agent.name,
        business_name=snapshot.workspace.name,
        persona=snapshot.agent.persona,
        fallback_message=snapshot.agent.fallback_message,
        emergency_message=snapshot.agent.emergency_message,
        tools={t.key for t in snapshot.tools},
        timezone=snapshot.workspace.timezone,
        current_local_time=local.isoformat(timespec="minutes"),
        weekday=local.strftime("%A"),
        supported_languages=snapshot.workspace.supported_languages,
        directory_summary=directory or "nothing published",
        live_updates=live_update_lines(snapshot, local),
    ).strip()

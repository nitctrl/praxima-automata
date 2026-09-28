"""Jinja-rendered voice policy with published quick daily information."""

import re
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


PACKS = Path(__file__).resolve().parents[1] / "packs"
PACK_KEY = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RELEASE_TEMPLATE = "prompts/release_system_prompt.j2"
RELEASE_ENVIRONMENT = Environment(
    loader=FileSystemLoader(PACKS),
    autoescape=False,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def release_template(pack_key: str) -> str:
    """The pack's own release prompt if it ships one, else the domain-neutral default."""
    if PACK_KEY.fullmatch(pack_key) and (PACKS / pack_key / RELEASE_TEMPLATE).is_file():
        return f"{pack_key}/{RELEASE_TEMPLATE}"
    return f"_template/{RELEASE_TEMPLATE}"


def render_release_prompt(snapshot: "AgentSnapshot", now: datetime | None = None) -> str:
    """System prompt for a call pinned to an agent release (schema version 4)."""
    from collections import Counter

    from praxima.runtime.release.lookup import live_update_lines, request_kinds

    local = (now or datetime.now(ZoneInfo(snapshot.workspace.timezone))).astimezone(
        ZoneInfo(snapshot.workspace.timezone)
    )
    counts = Counter(e.type.replace("_", " ") for e in snapshot.entities)
    enabled = {t.key for t in snapshot.tools}
    # "create_work_item" allows every request type; "request_callback" only the pack's
    # callback type.
    callback = snapshot.pack.callback_kind
    requestable: set[str] | None = (
        None
        if "create_work_item" in enabled
        else {callback}
        if "request_callback" in enabled and callback
        else set()
    )
    directory = ", ".join(
        f"{n} {kind}{'' if n == 1 else 's'}" for kind, n in sorted(counts.items())
    )
    return RELEASE_ENVIRONMENT.get_template(release_template(snapshot.pack.key)).render(
        entity_names=[t.name for t in snapshot.entity_types],
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
        request_types=request_kinds(snapshot, requestable) if requestable != set() else [],
    ).strip()

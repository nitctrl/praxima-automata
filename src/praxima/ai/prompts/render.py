"""The system prompt for a call, rendered from its pinned release snapshot.

One template per pack in `templates/<pack key>.j2`; packs without one use `default.j2`
(domain-neutral). Templates are AI behaviour, so they live here, not with the pack data.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

if TYPE_CHECKING:
    from praxima.contracts.agent_snapshot import AgentSnapshot

TEMPLATES = Path(__file__).resolve().parent / "templates"
PACK_KEY = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
DEFAULT_TEMPLATE = "default.j2"
RELEASE_ENVIRONMENT = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=False,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def release_template(pack_key: str) -> str:
    """The pack's own call prompt if there is one, else the domain-neutral default."""
    if PACK_KEY.fullmatch(pack_key) and (TEMPLATES / f"{pack_key}.j2").is_file():
        return f"{pack_key}.j2"
    return DEFAULT_TEMPLATE


def render_release_prompt(snapshot: "AgentSnapshot", now: datetime | None = None) -> str:
    """System prompt for a call pinned to an agent release (schema version 4)."""
    from collections import Counter

    from praxima.ai.release.lookup import live_update_lines, request_kinds

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

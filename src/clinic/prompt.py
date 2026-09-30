"""Jinja-rendered voice policy with published quick daily information."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from clinic.rag import published_live_updates
from clinic.snapshot import Snapshot

PROMPT_VERSION = "clinic-hybrid-rag-v2"
# Bounds prompt size; larger directories still resolve through the tools.
MAXIMUM_NAMES = 40
TEMPLATES = Path(__file__).parent / "templates"
ENVIRONMENT = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=False,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _named(name: str, aliases: tuple[str, ...]) -> str:
    return f"{name} (also: {', '.join(aliases)})" if aliases else name


def render_prompt(snapshot: Snapshot) -> str:
    now = datetime.now(ZoneInfo(snapshot.timezone))
    today = now.date()
    return ENVIRONMENT.get_template("agent_system_prompt.j2").render(
        clinic_name=snapshot.name,
        timezone=snapshot.timezone,
        supported_languages=snapshot.supported_languages,
        emergency_message=snapshot.emergency_message,
        live_updates=published_live_updates(snapshot),
        current_local_time=f"{now:%A} {now.isoformat(timespec='minutes')}",
        doctors=[
            _named(row.display_name, row.aliases)
            for row in snapshot.doctors
            if row.effective(today)
        ][:MAXIMUM_NAMES],
        services=[
            _named(row.name, row.aliases) for row in snapshot.services if row.effective(today)
        ][:MAXIMUM_NAMES],
        locations=[row.name for row in snapshot.locations if row.effective(today)][:MAXIMUM_NAMES],
    ).strip()

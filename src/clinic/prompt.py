"""Jinja-rendered voice policy with published quick daily information."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from clinic.rag import active_quick_info
from clinic.snapshot import Snapshot

PROMPT_VERSION = "clinic-hybrid-rag-v1"
TEMPLATES = Path(__file__).parent / "templates"
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
        quick_info=active_quick_info(snapshot),
    ).strip()

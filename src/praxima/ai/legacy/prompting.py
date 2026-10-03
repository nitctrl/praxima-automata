"""The legacy single-clinic voice prompt (PRAXIMA_VOICE_SOURCE=legacy).

Also used by the dashboard's legacy grounded test answers (integrations/llm/gemini.py), the one
listed place the backend imports `praxima.ai`. Remove both with the legacy path.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from praxima.contracts.clinic_snapshot import Snapshot
from praxima.modules.knowledge.application.retrieval import published_live_updates

PROMPT_VERSION = "clinic-hybrid-rag-v1"
TEMPLATES = Path(__file__).resolve().parent / "templates"
ENVIRONMENT = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=False,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_prompt(snapshot: Snapshot) -> str:
    return (
        ENVIRONMENT.get_template("agent_system_prompt.j2")
        .render(
            clinic_name=snapshot.name,
            timezone=snapshot.timezone,
            supported_languages=snapshot.supported_languages,
            emergency_message=snapshot.emergency_message,
            live_updates=published_live_updates(snapshot),
            current_local_time=datetime.now(ZoneInfo(snapshot.timezone)).isoformat(
                timespec="minutes"
            ),
        )
        .strip()
    )

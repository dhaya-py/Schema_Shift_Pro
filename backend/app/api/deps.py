from fastapi import BackgroundTasks

from app.core import database
from app.integrations.n8n import emit_events


def queue_events(background: BackgroundTasks, events: list[tuple[str, dict]]) -> None:
    """Send n8n events after the response; failures are recorded, never raised."""
    if events:
        background.add_task(emit_events, database.SessionLocal, events)


def paginate(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, 500)), max(0, offset)

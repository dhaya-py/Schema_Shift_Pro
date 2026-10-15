"""Optional n8n integration: outbound webhook events.

The core pipeline never depends on this module. Events are sent after the
database transaction commits (via FastAPI background tasks). Any failure
(no URL, timeout, HTTP error) is recorded in webhook_deliveries and swallowed,
so a webhook problem can never undo or fail a successful processing operation.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.models import WebhookDelivery

log = logging.getLogger("schema_shift.n8n")

EVENT_TYPES = {
    "dataset.uploaded",
    "schema.breaking_change",
    "processing.completed",
    "quarantine.created",
    "mapping.corrected",
    "reprocessing.completed",
    "integration.test",
}

# Tests can inject an httpx transport (e.g. MockTransport) here.
_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def build_envelope(event_type: str, data: dict) -> dict:
    return {
        "event": event_type,
        "source": "schema-shift",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


def send_event(db: Session, event_type: str, data: dict, settings: Settings | None = None) -> WebhookDelivery:
    settings = settings or get_settings()
    envelope = build_envelope(event_type, data)
    delivery = WebhookDelivery(event_type=event_type, status="skipped", payload=envelope)
    if not settings.n8n_webhook_url:
        delivery.error = "SCHEMA_SHIFT_N8N_WEBHOOK_URL is not set; event not sent."
    else:
        body = json.dumps(envelope, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Schema-Shift-Event": event_type}
        if settings.n8n_webhook_secret:
            sig = hmac.new(settings.n8n_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Schema-Shift-Signature"] = f"sha256={sig}"
        try:
            with httpx.Client(timeout=settings.n8n_timeout_seconds, transport=_transport) as client:
                resp = client.post(settings.n8n_webhook_url, content=body, headers=headers)
            delivery.http_status = resp.status_code
            if resp.is_success:
                delivery.status = "delivered"
            else:
                delivery.status = "failed"
                delivery.error = f"HTTP {resp.status_code}: {resp.text[:300]}"
        except httpx.HTTPError as exc:
            delivery.status = "failed"
            delivery.error = f"{type(exc).__name__}: {exc}"[:500]
        except Exception as exc:  # never let the integration break the caller
            delivery.status = "failed"
            delivery.error = f"Unexpected error: {type(exc).__name__}: {exc}"[:500]
    db.add(delivery)
    db.commit()
    if delivery.status == "failed":
        log.warning("n8n webhook %s failed: %s", event_type, delivery.error)
    return delivery


def emit_events(session_factory: sessionmaker, events: list[tuple[str, dict]]) -> None:
    """Background-task entry point. Uses its own session; never raises."""
    if not events:
        return
    try:
        with session_factory() as db:
            for event_type, data in events:
                send_event(db, event_type, data)
    except Exception:  # pragma: no cover - defensive
        log.exception("Failed to record webhook deliveries")

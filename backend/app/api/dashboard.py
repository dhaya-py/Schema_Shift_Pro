from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.integrations.n8n import send_event
from app.models import WebhookDelivery
from app.schemas import WebhookDeliveryOut
from app.services import dashboard, demo

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
def summary(db: Session = Depends(get_db)):
    return dashboard.summary(db)


@router.get("/dashboard/data-quality")
def data_quality(db: Session = Depends(get_db)):
    return dashboard.data_quality(db)


@router.get("/dashboard/schema-changes")
def schema_changes(db: Session = Depends(get_db)):
    return dashboard.schema_changes(db)


@router.get("/integrations/n8n", summary="n8n configuration state and recent deliveries")
def n8n_status(db: Session = Depends(get_db)):
    s = get_settings()
    rows = db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id.desc()).limit(20)).all()
    return {
        "configured": bool(s.n8n_webhook_url),
        "signing_enabled": bool(s.n8n_webhook_secret),
        "timeout_seconds": s.n8n_timeout_seconds,
        "recent_deliveries": [WebhookDeliveryOut.model_validate(r) for r in rows],
    }


@router.post("/integrations/n8n/test", response_model=WebhookDeliveryOut, summary="Send a test event now")
def n8n_test(db: Session = Depends(get_db)):
    return send_event(db, "integration.test", {"message": "Test event from Schema Shift."})


@router.post("/demo/load", summary="Reset and reload the demo sources only")
def load_demo(db: Session = Depends(get_db)):
    """Deletes data that belongs to sources flagged as demo, then loads the
    sample files and runs the scripted scenarios. Other sources are untouched."""
    return demo.load_demo(db)

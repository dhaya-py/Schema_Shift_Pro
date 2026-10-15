import os
import tempfile
from pathlib import Path

# Point the app at a throwaway database before any app module is imported.
_TMP = Path(tempfile.mkdtemp(prefix="schema_shift_tests_"))
os.environ["SCHEMA_SHIFT_DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["SCHEMA_SHIFT_N8N_WEBHOOK_URL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import database  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import Base, init_db  # noqa: E402
from app.integrations import n8n  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(database.engine)
    init_db()
    get_settings.cache_clear()
    n8n.set_transport(None)
    yield
    n8n.set_transport(None)
    get_settings.cache_clear()


@pytest.fixture
def db():
    with database.SessionLocal() as session:
        yield session


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, filename: str, content: str | bytes, source_name: str | None = None, source_id: int | None = None):
    data = {}
    if source_name:
        data["source_name"] = source_name
    if source_id is not None:
        data["source_id"] = str(source_id)
    body = content.encode() if isinstance(content, str) else content
    return client.post("/api/v1/datasets/upload", files={"file": (filename, body)}, data=data)


def upload_sample(client, filename: str, source_name: str):
    return upload(client, filename, (SAMPLES / filename).read_bytes(), source_name=source_name)

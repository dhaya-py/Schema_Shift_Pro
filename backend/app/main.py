from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import dashboard, datasets, mappings, processing, sources
from app.core.config import get_settings
from app.core.database import init_db
from app.services.ingestion import IngestionError
from app.services.mappings import MappingError
from app.services.processing import ProcessingError

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Schema Shift API",
        version="0.1.0",
        description=(
            "Prototype API for self-adapting data pipelines: ingestion, schema profiling, drift detection, "
            "mapping, validation, normalization, quarantine, recovery and lineage. Not production-hardened."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(IngestionError)
    @app.exception_handler(MappingError)
    @app.exception_handler(ProcessingError)
    async def _domain_error(_request: Request, exc: Exception):
        return JSONResponse(status_code=getattr(exc, "status_code", 400), content={"detail": str(exc)})

    for module in (sources, datasets, mappings, processing, dashboard):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get("/api/health", tags=["health"])
    def health():
        return {"message": "app is running successfully", "n8n_configured": bool(get_settings().n8n_webhook_url)}

    return app


app = create_app()

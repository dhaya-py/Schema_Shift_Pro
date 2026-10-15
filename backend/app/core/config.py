"""Environment-based configuration. No secrets are hardcoded; see .env.example."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_DIR / ".env", BACKEND_DIR / ".env"),
        env_prefix="SCHEMA_SHIFT_",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{(BACKEND_DIR / 'schema_shift.db').as_posix()}"
    max_upload_mb: float = 5.0
    max_records_per_upload: int = 50_000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    samples_dir: str = str(PROJECT_DIR / "data" / "samples")

    # Optional n8n integration. Empty URL = disabled.
    n8n_webhook_url: str = ""
    n8n_webhook_secret: str = ""
    n8n_timeout_seconds: float = 3.0

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    app_name: str = Field("Inbox Steward", description="Display name for the web UI")
    environment: str = Field("development", description="Environment name")
    log_level: str = Field("INFO", description="Python logging level")

    database_url: str = Field(
        "postgresql+psycopg2://inbox_steward:inbox_steward@db:5432/inbox_steward",
        description="SQLAlchemy connection string",
    )

    redis_url: str = Field("redis://redis:6379/0", description="Redis URL for caching")

    imap_host: str = Field(..., env="IMAP_HOST")
    imap_port: int = Field(993, env="IMAP_PORT")
    imap_username: str = Field(..., env="IMAP_USERNAME")
    imap_password: str = Field(..., env="IMAP_PASSWORD")
    imap_use_ssl: bool = Field(True, env="IMAP_USE_SSL")
    imap_mailbox: str = Field("INBOX", env="IMAP_MAILBOX")

    timezone: str = Field("America/Vancouver", env="TIMEZONE")

    poll_interval_seconds: int = Field(120, env="POLL_INTERVAL_SECONDS")
    full_sort_interval_minutes: int = Field(180, env="FULL_SORT_INTERVAL_MINUTES")

    ollama_model: str = Field("mistral", env="OLLAMA_MODEL")
    ollama_endpoint: str = Field("http://ollama:11434", env="OLLAMA_ENDPOINT")

    ha_base_url: Optional[str] = Field(None, env="HOME_ASSISTANT_BASE_URL")
    ha_token: Optional[str] = Field(None, env="HOME_ASSISTANT_TOKEN")
    ha_mobile_target: Optional[str] = Field(None, env="HOME_ASSISTANT_MOBILE_TARGET")

    web_admin_password: str = Field("change-me", env="WEB_ADMIN_PASSWORD")
    secret_key: str = Field("super-secret", env="SECRET_KEY")

    pdf_temp_dir: Path = Field(Path("/tmp/pdf-cache"), env="PDF_TEMP_DIR")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False

    @validator("pdf_temp_dir", pre=True)
    def _coerce_pdf_path(cls, value: str | Path) -> Path:
        return Path(value)


@lru_cache()
def get_settings() -> Settings:
    return Settings()  # type: ignore[arg-type]


settings = get_settings()

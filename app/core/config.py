from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field, validator
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

    mail_backend: str = Field(
        "IMAP",
        validation_alias=AliasChoices("MAIL_BACKEND", "MAIL_PROVIDER", "EMAIL_BACKEND"),
        description="Mail provider backend: IMAP or EXCHANGE",
    )

    imap_host: str = Field(..., env="IMAP_HOST")
    imap_port: int = Field(993, env="IMAP_PORT")
    imap_username: str = Field(..., env="IMAP_USERNAME")
    imap_password: str = Field(..., env="IMAP_PASSWORD")
    imap_encryption: str = Field(
        "SSL",
        validation_alias=AliasChoices("IMAP_ENCRYPTION", "IMAP_SECURITY", "IMAP_USE_SSL"),
        description="IMAP transport security mode: SSL, STARTTLS, or NONE",
    )
    imap_auth_type: str = Field(
        "LOGIN",
        validation_alias=AliasChoices("IMAP_AUTH_TYPE", "IMAP_AUTH_METHOD", "IMAP_AUTH"),
        description="IMAP authentication mechanism: LOGIN or XOAUTH2",
    )
    imap_oauth2_token: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("IMAP_OAUTH2_TOKEN", "IMAP_OAUTH_TOKEN", "IMAP_AUTH_TOKEN"),
        description="Bearer token for XOAUTH2 authentication",
    )
    imap_mailbox: str = Field("INBOX", env="IMAP_MAILBOX")

    exchange_tenant_id: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("EXCHANGE_TENANT_ID", "AZURE_TENANT_ID", "O365_TENANT_ID"),
        description="Azure AD tenant ID or directory hint for Exchange OAuth",
    )
    exchange_client_id: Optional[str] = Field(
        None,
        validation_alias=AliasChoices("EXCHANGE_CLIENT_ID", "AZURE_CLIENT_ID", "O365_CLIENT_ID"),
        description="Azure AD application (client) ID",
    )
    exchange_client_secret: Optional[str] = Field(
        None,
        validation_alias=AliasChoices(
            "EXCHANGE_CLIENT_SECRET",
            "AZURE_CLIENT_SECRET",
            "O365_CLIENT_SECRET",
        ),
        description="Azure AD application client secret",
    )
    exchange_user_id: Optional[str] = Field(
        None,
        validation_alias=AliasChoices(
            "EXCHANGE_USER_ID",
            "EXCHANGE_PRIMARY_SMTP",
            "EXCHANGE_MAILBOX",
            "EXCHANGE_USERNAME",
        ),
        description="Mailbox user principal name or primary SMTP address",
    )
    exchange_authority: str = Field(
        "https://login.microsoftonline.com",
        validation_alias=AliasChoices("EXCHANGE_AUTHORITY", "AZURE_AUTHORITY"),
        description="Azure AD authority base URL",
    )
    exchange_scope: str = Field(
        "https://graph.microsoft.com/.default",
        validation_alias=AliasChoices("EXCHANGE_SCOPE", "EXCHANGE_SCOPES"),
        description="Space separated OAuth scopes for Exchange Graph access",
    )
    exchange_login_mode: str = Field(
        "CLIENT",
        validation_alias=AliasChoices(
            "EXCHANGE_LOGIN_MODE",
            "EXCHANGE_AUTH_MODE",
            "EXCHANGE_AUTH_FLOW",
        ),
        description="Exchange auth flow: CLIENT (application) or DELEGATED (device login)",
    )
    exchange_token_cache: Path = Field(
        Path("/data/exchange/token.json"),
        validation_alias=AliasChoices(
            "EXCHANGE_TOKEN_CACHE",
            "EXCHANGE_TOKEN_PATH",
            "EXCHANGE_CACHE_PATH",
        ),
        description="Location to persist Exchange delegated auth tokens",
    )
    exchange_timeout: int = Field(30, env="EXCHANGE_TIMEOUT")

    timezone: str = Field("America/Vancouver", env="TIMEZONE")

    poll_interval_seconds: int = Field(120, env="POLL_INTERVAL_SECONDS")
    full_sort_interval_minutes: int = Field(180, env="FULL_SORT_INTERVAL_MINUTES")

    ollama_model: str = Field("gpt-oss:20b", env="OLLAMA_MODEL")
    ollama_endpoint: str = Field("http://ollama.local:11434", env="OLLAMA_ENDPOINT")

    ha_base_url: Optional[str] = Field(
        "http://homeassistant.local:8123",
        validation_alias=AliasChoices(
            "HOME_ASSISTANT_BASE_URL",
            "HOME_ASSISTANT_URL",
            "HA_BASE_URL",
            "HASS_BASE_URL",
        ),
    )
    ha_token: Optional[str] = Field(
        None,
        validation_alias=AliasChoices(
            "HOME_ASSISTANT_TOKEN",
            "HOME_ASSISTANT_LONG_LIVED_TOKEN",
            "HA_TOKEN",
            "HASS_TOKEN",
        ),
    )
    ha_mobile_target: Optional[str] = Field(
        "notify.mobile_app",
        validation_alias=AliasChoices(
            "HOME_ASSISTANT_MOBILE_TARGET",
            "HOME_ASSISTANT_NOTIFY_TARGET",
            "HA_NOTIFY_TARGET",
        ),
    )

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

    @validator("mail_backend", pre=True)
    def _normalize_mail_backend(cls, value: str | None) -> str:
        if not value:
            return "IMAP"
        normalized = str(value).strip().upper()
        if normalized in {"IMAP", "IMAPS"}:
            return "IMAP"
        if normalized in {"EXCHANGE", "GRAPH", "OUTLOOK"}:
            return "EXCHANGE"
        raise ValueError("MAIL_BACKEND must be IMAP or EXCHANGE")

    @validator("exchange_authority", pre=True)
    def _normalize_exchange_authority(cls, value: str | None) -> str:
        if not value:
            return "https://login.microsoftonline.com"
        text = str(value).strip()
        if not text:
            return "https://login.microsoftonline.com"
        return text.rstrip("/")

    @validator("exchange_scope", pre=True)
    def _normalize_exchange_scope(cls, value: str | None) -> str:
        if not value:
            return "https://graph.microsoft.com/.default"
        text = " ".join(part for part in str(value).replace(",", " ").split())
        return text or "https://graph.microsoft.com/.default"

    @validator("exchange_login_mode", pre=True)
    def _normalize_exchange_login_mode(cls, value: str | None) -> str:
        if not value:
            return "CLIENT"
        normalized = str(value).strip().upper().replace("-", "_")
        if normalized in {"CLIENT", "APPLICATION", "CLIENT_CREDENTIAL"}:
            return "CLIENT"
        if normalized in {"DELEGATED", "DEVICE", "DEVICE_CODE", "PERSONAL"}:
            return "DELEGATED"
        raise ValueError("EXCHANGE_LOGIN_MODE must be CLIENT or DELEGATED")

    @validator("imap_encryption", pre=True)
    def _normalize_imap_encryption(cls, value: str | bool | None) -> str:
        if value in (None, ""):
            return "SSL"
        if isinstance(value, bool):
            return "SSL" if value else "NONE"

        text = str(value).strip()
        if not text:
            return "SSL"

        normalized = text.replace("-", "").replace("_", "").upper()

        if normalized in {"SSL", "TLS", "TRUE", "1", "ON", "YES"}:
            return "SSL"
        if normalized in {"STARTTLS"}:
            return "STARTTLS"
        if normalized in {"NONE", "NOENCRYPTION", "PLAIN", "UNENCRYPTED", "FALSE", "0", "OFF"}:
            return "NONE"

        raise ValueError("IMAP_ENCRYPTION must be one of: SSL, STARTTLS, NONE")

    @validator("imap_auth_type", pre=True)
    def _normalize_imap_auth_type(cls, value: str | None) -> str:
        if value in (None, ""):
            return "LOGIN"

        normalized = str(value).strip().upper().replace("-", "_")
        if normalized in {"LOGIN", "PLAIN", "AUTHLOGIN", "BASIC"}:
            return "LOGIN"
        if normalized in {"XOAUTH2", "OAUTH2"}:
            return "XOAUTH2"

        raise ValueError("IMAP_AUTH_TYPE must be LOGIN or XOAUTH2")

    @validator("imap_oauth2_token", pre=True)
    def _normalize_imap_oauth2_token(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @property
    def exchange_scopes(self) -> list[str]:
        return [scope for scope in self.exchange_scope.split() if scope]


@lru_cache()
def get_settings() -> Settings:
    return Settings()  # type: ignore[arg-type]


settings = get_settings()

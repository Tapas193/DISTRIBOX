"""Application configuration.

All configuration is environment-driven with safe development defaults.
Secrets are never committed: ``.env`` is gitignored and only
``.env.example`` is tracked.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- application ----
    app_name: str = "D6 DISTRIBOX"
    app_version: str = "0.1.0"
    environment: str = Field(default="development")
    debug: bool = Field(default=True)
    log_format: str = Field(default="console")  # console | json
    api_v1_prefix: str = "/api/v1"
    cors_origins: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
        ]
    )

    # ---- database ----
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5433)
    postgres_db: str = Field(default="distribox")
    postgres_user: str = Field(default=os.environ.get("USER", "postgres"))
    postgres_password: str = Field(default="")
    database_url_override: Optional[str] = Field(default=None)
    db_echo: bool = Field(default=False)
    db_pool_size: int = Field(default=10)
    db_max_overflow: int = Field(default=20)

    # ---- seed / demo ----
    demo_hotel_code: str = Field(default="HHT-DEMO")
    seed_random_seed: int = Field(default=20260904)
    seed_booking_count: int = Field(default=5000)
    seed_guest_count: int = Field(default=1000)

    # ---- uploads ----
    max_csv_bytes: int = Field(default=25 * 1024 * 1024)
    max_csv_rows: int = Field(default=50_000)

    # ---- AI (optional; app degrades gracefully when absent) ----
    ai_provider: str = Field(default="none")
    ai_api_key: Optional[str] = Field(default=None)
    ai_model: str = Field(default="gpt-4o-mini")
    ai_base_url: str = Field(default="https://api.openai.com/v1")
    ai_timeout_seconds: float = Field(default=30.0)

    # ---- auth (architecture present; enforcement is a deployment choice) ----
    auth_enabled: bool = Field(default=False)
    auth_api_key: Optional[str] = Field(default=None)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def database_url(self) -> str:
        """Async-free synchronous SQLAlchemy URL.

        Built from discrete parts so that a password containing ``@`` or
        ``:`` is URL-encoded rather than corrupting the DSN.
        """
        if self.database_url_override:
            return self.database_url_override
        from urllib.parse import quote_plus

        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password)
        auth = f"{user}:{password}" if self.postgres_password else user
        return (
            f"postgresql+psycopg://{auth}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
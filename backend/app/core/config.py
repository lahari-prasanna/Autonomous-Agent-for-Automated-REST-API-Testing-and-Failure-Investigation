"""
Application Configuration and Settings (Phase 5A).
Loads environment variables from .env using pydantic-settings.
"""

from __future__ import annotations

import os
from typing import List, Optional, Union
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central environment-driven configuration for backend services and databases.
    Never exposes raw secrets; supports development, staging, and production environments.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Environment & Logging
    ENVIRONMENT: str = Field(default="development", description="Runtime environment: development, staging, production")
    LOG_LEVEL: str = Field(default="INFO", description="Logging verbosity")

    # Server Configuration
    BACKEND_HOST: str = Field(default="0.0.0.0", description="FastAPI host binding")
    BACKEND_PORT: int = Field(default=8000, description="FastAPI port binding")
    SECRET_KEY: str = Field(
        default="change_this_to_a_secure_random_key_in_production",
        description="Application secret key for tokens and hashing",
    )

    # Database Configuration
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///./api_agent.db",
        description="Async SQLAlchemy database URL (PostgreSQL asyncpg or SQLite aiosqlite)",
    )

    # Security & SSRF Configuration
    SAFE_DEV_MODE: bool = Field(
        default=False,
        description="When True, permits verified loopback (127.0.0.1) for local mock testing only",
    )
    ALLOWED_DEV_HOSTS: Union[List[str], str] = Field(
        default=["localhost", "127.0.0.1"],
        description="Allowlisted loopback hosts when SAFE_DEV_MODE is enabled",
    )

    # Mock API & LLM Provider Configuration
    MOCK_API_PORT: int = Field(default=8081, description="Local mock REST API port")
    LLM_PROVIDER: str = Field(default="gemini", description="Selected LLM provider (gemini or openai)")
    GEMINI_API_KEY: Optional[str] = Field(default=None, description="Google Gemini API key")
    OPENAI_API_KEY: Optional[str] = Field(default=None, description="OpenAI API key")

    @field_validator("ALLOWED_DEV_HOSTS", mode="before")
    @classmethod
    def parse_allowed_dev_hosts(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        if isinstance(v, list):
            return [str(h).strip() for h in v]
        return ["localhost", "127.0.0.1"]

    @property
    def is_sqlite(self) -> bool:
        """Returns True if the configured database is SQLite."""
        return "sqlite" in self.DATABASE_URL.lower()

    @property
    def is_production(self) -> bool:
        """Returns True if running in production mode."""
        return self.ENVIRONMENT.lower() == "production"


# Singleton application settings
settings = Settings()

"""
API Dependencies and Injection Providers (Phase 5C).
Provides short-lived database sessions and singleton service instances.
"""

from __future__ import annotations

from typing import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import get_db
from backend.app.services.project_service import ProjectService
from backend.app.services.run_service import RunService

# Re-export get_db for FastAPI dependency injection
__all__ = [
    "get_db",
    "get_project_service",
    "get_run_service",
]


def get_project_service() -> ProjectService:
    """Dependency providing a ProjectService instance."""
    return ProjectService()


def get_run_service() -> RunService:
    """Dependency providing a RunService instance."""
    return RunService()

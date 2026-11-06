"""
API v1 Router Aggregator (Phase 5C).
Combines Project, Run, and Diagnostic Report routers under the /api/v1 prefix.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.api.v1.projects import router as projects_router
from backend.app.api.v1.reports import router as reports_router
from backend.app.api.v1.runs import router as runs_router

api_v1_router = APIRouter(prefix="/api/v1")

# Mount modular routers
api_v1_router.include_router(projects_router)
api_v1_router.include_router(runs_router)
api_v1_router.include_router(reports_router)

__all__ = ["api_v1_router"]

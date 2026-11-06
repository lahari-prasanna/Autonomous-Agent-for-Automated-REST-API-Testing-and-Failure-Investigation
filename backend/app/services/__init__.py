"""
Services Package (Phase 5B).
Provides application services with lazy attribute loading to prevent circular import cascades.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ProjectService",
    "RunService",
]


def __getattr__(name: str) -> Any:
    if name == "ProjectService":
        from backend.app.services.project_service import ProjectService

        return ProjectService
    if name == "RunService":
        from backend.app.services.run_service import RunService

        return RunService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""
SQLAlchemy 2.0 Database Models Package (Phase 5A).
Exports all declarative models and Base for metadata discovery and Alembic migrations.
"""

from __future__ import annotations

from backend.app.models.base import Base, PortableJSON, TimestampMixin, UUIDMixin
from backend.app.models.endpoint import Endpoint
from backend.app.models.execution import TestExecution
from backend.app.models.investigation import FailureInvestigationModel
from backend.app.models.project import Project
from backend.app.models.report import DiagnosticReportModel
from backend.app.models.run import AgentRun
from backend.app.models.testcase import TestCase

__all__ = [
    "Base",
    "PortableJSON",
    "TimestampMixin",
    "UUIDMixin",
    "Project",
    "Endpoint",
    "AgentRun",
    "TestCase",
    "TestExecution",
    "FailureInvestigationModel",
    "DiagnosticReportModel",
]

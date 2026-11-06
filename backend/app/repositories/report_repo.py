"""
DiagnosticReport Repository (Phase 5B).
Provides database operations for DiagnosticReport snapshots and pre-rendered safe Markdown reports.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.report import DiagnosticReportModel
from backend.app.schemas.report import DiagnosticReport
from backend.app.services.report_generator import DiagnosticReportGenerator

logger = logging.getLogger(__name__)


async def create_report(
    session: AsyncSession,
    run_id: str,
    report: DiagnosticReport,
    markdown_rendered: Optional[str] = None,
) -> DiagnosticReportModel:
    """
    Persists a completed DiagnosticReport and its pre-rendered safe Markdown report.
    """
    safe_markdown = (
        markdown_rendered
        if markdown_rendered is not None
        else DiagnosticReportGenerator.to_markdown(report)
    )

    db_report = DiagnosticReportModel(
        run_id=run_id,
        report_id=report.report_id,
        api_title=report.api_title,
        api_version=report.api_version,
        base_url=report.base_url,
        summary_data=report.summary.model_dump(),
        operations_data=[op.model_dump() for op in report.operations],
        global_remediation_summary=list(report.global_remediation_summary),
        markdown_rendered=safe_markdown,
    )
    session.add(db_report)
    await session.flush()
    await session.refresh(db_report)
    return db_report


async def get_report_by_run(
    session: AsyncSession,
    run_id: str,
) -> Optional[DiagnosticReportModel]:
    """Retrieves a DiagnosticReportModel associated with an AgentRun ID."""
    stmt = select(DiagnosticReportModel).where(DiagnosticReportModel.run_id == run_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_report_by_id(
    session: AsyncSession,
    report_id: str,
) -> Optional[DiagnosticReportModel]:
    """Retrieves a DiagnosticReportModel by canonical report_id string."""
    stmt = select(DiagnosticReportModel).where(DiagnosticReportModel.report_id == report_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

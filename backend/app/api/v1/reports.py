"""
Diagnostic Report API Router (Phase 5C).
Provides endpoints to retrieve structured JSON snapshots and pre-rendered safe Markdown reports.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_db, get_run_service
from backend.app.schemas.api import DiagnosticReportResponse
from backend.app.services.run_service import RunService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["reports"])


@router.get(
    "/{run_id}/report",
    response_model=DiagnosticReportResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve the diagnostic report for a run (JSON or Markdown)",
    responses={
        200: {
            "content": {
                "application/json": {},
                "text/markdown": {},
            },
            "description": "Returns structured JSON or safe GitHub-flavored Markdown.",
        },
        404: {"description": "Run or diagnostic report not found."},
        409: {"description": "Run has not yet finished; report is unavailable."},
    },
)
async def get_run_report(
    run_id: str,
    format: Literal["json", "markdown"] = Query(
        "json",
        description="Desired report serialization format: 'json' (default) or 'markdown'",
    ),
    session: AsyncSession = Depends(get_db),
    run_service: RunService = Depends(get_run_service),
) -> Response:
    """
    Retrieves the completed DiagnosticReport for an AgentRun.
    Supports structured JSON data transfer and neutralized Markdown document export.
    """
    run = await run_service.get_run(session=session, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AgentRun with ID '{run_id}' not found.",
        )

    if run.status in ("pending", "running"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Diagnostic report not yet available; run status is '{run.status}'.",
        )

    db_report = await run_service.get_run_report(session=session, run_id=run_id)
    if not db_report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No diagnostic report found for run '{run_id}'.",
        )

    # Return safe Markdown if requested
    if format == "markdown":
        md_content = db_report.markdown_rendered or "# Diagnostic Report (Content Unavailable)"
        return Response(content=md_content, media_type="text/markdown")

    # Otherwise return JSON model
    report_dto = DiagnosticReportResponse(
        id=db_report.id,
        run_id=db_report.run_id,
        report_id=db_report.report_id,
        api_title=db_report.api_title,
        api_version=db_report.api_version,
        base_url=db_report.base_url,
        summary=db_report.summary_data,
        operations=db_report.operations_data,
        global_remediation_summary=db_report.global_remediation_summary,
        markdown_rendered=db_report.markdown_rendered,
        created_at=db_report.created_at,
    )
    return report_dto  # type: ignore[return-value]

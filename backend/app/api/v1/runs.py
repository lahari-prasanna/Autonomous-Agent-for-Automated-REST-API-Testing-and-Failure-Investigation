"""
Run Execution Management API Router (Phase 5C).
Provides endpoints to trigger, inspect, list, and cancel test execution runs.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_db, get_project_service, get_run_service
from backend.app.schemas.api import RunCreateRequest, RunResponse
from backend.app.services.project_service import ProjectService
from backend.app.services.run_service import RunService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


@router.post(
    "/projects/{project_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new test execution run for a project",
)
async def create_project_run(
    project_id: str,
    payload: RunCreateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    run_service: RunService = Depends(get_run_service),
) -> RunResponse:
    """
    Creates an AgentRun session in 'pending' state and schedules un-transacted
    WorkflowOrchestrator execution in the background.
    """
    try:
        run = await run_service.create_run(
            session=session,
            project_id=project_id,
            concurrency=payload.concurrency,
            target_endpoints=payload.target_endpoints,
        )
        await session.commit()
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        ) from err

    # Dispatch agent run execution via background task outside database transaction
    background_tasks.add_task(
        run_service.execute_run,
        run_id=run.id,
        custom_base_url=payload.custom_base_url,
        safe_dev_mode=payload.safe_dev_mode,
    )

    return RunResponse.model_validate(run)


@router.get(
    "/projects/{project_id}/runs",
    response_model=List[RunResponse],
    status_code=status.HTTP_200_OK,
    summary="List all execution runs for a project",
)
async def list_project_runs(
    project_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_db),
    project_service: ProjectService = Depends(get_project_service),
    run_service: RunService = Depends(get_run_service),
) -> List[RunResponse]:
    """Retrieves all testing runs executed for a specific Project."""
    project = await project_service.get_project(session=session, project_id=project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with ID '{project_id}' not found.",
        )

    runs = await run_service.list_runs_by_project(
        session=session,
        project_id=project_id,
        skip=skip,
        limit=limit,
    )
    return [RunResponse.model_validate(r) for r in runs]


@router.get(
    "/runs/{run_id}",
    response_model=RunResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve status and execution details of a run",
)
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_db),
    run_service: RunService = Depends(get_run_service),
) -> RunResponse:
    """Retrieves an AgentRun session by ID."""
    run = await run_service.get_run(session=session, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AgentRun with ID '{run_id}' not found.",
        )
    return RunResponse.model_validate(run)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunResponse,
    status_code=status.HTTP_200_OK,
    summary="Request cancellation of a pending or in-flight run",
)
async def cancel_run(
    run_id: str,
    session: AsyncSession = Depends(get_db),
    run_service: RunService = Depends(get_run_service),
) -> RunResponse:
    """Signals cancellation to an active AgentRun."""
    run = await run_service.cancel_run(
        session=session,
        run_id=run_id,
        reason="Run cancelled via API request",
    )
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AgentRun with ID '{run_id}' not found.",
        )
    await session.commit()
    return RunResponse.model_validate(run)


@router.get(
    "/runs/{run_id}/events",
    summary="Subscribe to live Server-Sent Events (SSE) for a run",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Real-time stream of run progress events.",
        },
        404: {"description": "Run not found."},
    },
)
async def get_run_events(
    run_id: str,
    request: Request,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    session: AsyncSession = Depends(get_db),
    run_service: RunService = Depends(get_run_service),
) -> StreamingResponse:
    """
    Streams live progress events for an AgentRun using standard Server-Sent Events (SSE).
    Supports multiple concurrent subscribers, event replay via Last-Event-ID, and periodic heartbeats.
    """
    run = await run_service.get_run(session=session, run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AgentRun with ID '{run_id}' not found.",
        )

    async def event_generator():
        try:
            async for event in run_service.event_manager.subscribe(
                run_id=run_id,
                last_event_id=last_event_id,
            ):
                yield event.format_sse()
        except asyncio.CancelledError:
            logger.debug("SSE connection cancelled for run %s", run_id)
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Type": "text/event-stream",
        },
    )

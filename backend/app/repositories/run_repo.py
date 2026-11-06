"""
AgentRun Repository (Phase 5B).
Provides database operations for test execution runs, status lifecycle transitions,
and interrupted-run crash recovery.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.base import utc_now
from backend.app.models.run import AgentRun

logger = logging.getLogger(__name__)


async def create_run(
    session: AsyncSession,
    project_id: str,
    concurrency: int = 1,
    target_endpoints: Optional[List[str]] = None,
    status: str = "pending",
) -> AgentRun:
    """Creates and persists a new AgentRun session."""
    clamped_concurrency = max(1, min(concurrency, 5))
    run = AgentRun(
        project_id=project_id,
        concurrency=clamped_concurrency,
        target_endpoints=target_endpoints,
        status=status,
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)
    return run


async def get_run(session: AsyncSession, run_id: str) -> Optional[AgentRun]:
    """Retrieves an AgentRun by ID, or None if not found."""
    stmt = select(AgentRun).where(AgentRun.id == run_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_runs_by_project(
    session: AsyncSession,
    project_id: str,
    skip: int = 0,
    limit: int = 100,
) -> List[AgentRun]:
    """Lists AgentRuns for a Project ordered by created_at descending."""
    stmt = (
        select(AgentRun)
        .where(AgentRun.project_id == project_id)
        .order_by(AgentRun.created_at.desc())
        .offset(max(0, skip))
        .limit(max(1, limit))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_run(
    session: AsyncSession,
    run_id: str,
    **kwargs: Any,
) -> Optional[AgentRun]:
    """Updates attributes of an existing AgentRun."""
    run = await get_run(session, run_id)
    if run is None:
        return None

    allowed_fields = {
        "status",
        "concurrency",
        "target_endpoints",
        "started_at",
        "completed_at",
        "duration_ms",
        "summary",
        "error_message",
    }
    for key, value in kwargs.items():
        if key in allowed_fields:
            setattr(run, key, value)

    await session.flush()
    await session.refresh(run)
    return run


async def update_run_status(
    session: AsyncSession,
    run_id: str,
    status: str,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    duration_ms: Optional[float] = None,
    summary: Optional[Any] = None,
    error_message: Optional[str] = None,
) -> Optional[AgentRun]:
    """Updates the status and lifecycle timestamps/metrics of an AgentRun."""
    run = await get_run(session, run_id)
    if run is None:
        return None

    run.status = status
    if started_at is not None:
        run.started_at = started_at
    if completed_at is not None:
        run.completed_at = completed_at
    if duration_ms is not None:
        run.duration_ms = duration_ms
    if summary is not None:
        run.summary = summary
    if error_message is not None:
        run.error_message = error_message

    await session.flush()
    await session.refresh(run)
    return run


async def cancel_run(
    session: AsyncSession,
    run_id: str,
    reason: Optional[str] = "Run cancelled by user",
) -> Optional[AgentRun]:
    """Cancels a pending or running AgentRun."""
    run = await get_run(session, run_id)
    if run is None:
        return None

    if run.status in ("pending", "running"):
        run.status = "cancelled"
        run.completed_at = utc_now()
        run.error_message = reason
        await session.flush()
        await session.refresh(run)

    return run


async def recover_interrupted_runs(
    session: AsyncSession,
    error_message: str = "Interrupted run recovered: process terminated while running",
) -> int:
    """
    Finds all AgentRuns left in 'running' state and transitions them to 'failed'.
    Returns the count of recovered runs.
    """
    now = utc_now()
    stmt = (
        update(AgentRun)
        .where(AgentRun.status == "running")
        .values(
            status="failed",
            completed_at=now,
            error_message=error_message,
        )
    )
    result = await session.execute(stmt)
    await session.flush()
    count = result.rowcount
    if count > 0:
        logger.warning("Recovered %d interrupted run(s) left in running state.", count)
    return count

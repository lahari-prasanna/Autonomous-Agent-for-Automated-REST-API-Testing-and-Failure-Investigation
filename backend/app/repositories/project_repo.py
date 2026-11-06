"""
Project and Endpoint Repository (Phase 5B).
Provides database operations for Project entities and extracted OpenAPI Endpoints.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Union

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.endpoint import Endpoint
from backend.app.models.project import Project
from backend.app.schemas.spec import EndpointDefinition

logger = logging.getLogger(__name__)


async def create_project(
    session: AsyncSession,
    name: str,
    description: Optional[str] = None,
    base_url: Optional[str] = None,
    spec_raw: Optional[str] = None,
    spec_title: Optional[str] = None,
    spec_version: Optional[str] = None,
) -> Project:
    """Creates and persists a new Project."""
    project = Project(
        name=name,
        description=description,
        base_url=base_url,
        spec_raw=spec_raw,
        spec_title=spec_title,
        spec_version=spec_version,
    )
    session.add(project)
    await session.flush()
    await session.refresh(project)
    return project


async def get_project(session: AsyncSession, project_id: str) -> Optional[Project]:
    """Retrieves a Project by ID, or None if not found."""
    stmt = select(Project).where(Project.id == project_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_projects(
    session: AsyncSession,
    skip: int = 0,
    limit: int = 100,
) -> List[Project]:
    """Lists projects ordered by creation time descending with pagination."""
    stmt = (
        select(Project)
        .order_by(Project.created_at.desc())
        .offset(max(0, skip))
        .limit(max(1, limit))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_project(
    session: AsyncSession,
    project_id: str,
    **kwargs: Any,
) -> Optional[Project]:
    """Updates attributes of an existing Project."""
    project = await get_project(session, project_id)
    if project is None:
        return None

    allowed_fields = {
        "name",
        "description",
        "base_url",
        "spec_raw",
        "spec_title",
        "spec_version",
    }
    for key, value in kwargs.items():
        if key in allowed_fields:
            setattr(project, key, value)

    await session.flush()
    await session.refresh(project)
    return project


async def delete_project(session: AsyncSession, project_id: str) -> bool:
    """Deletes a Project by ID (cascading endpoints and runs). Returns True if deleted."""
    project = await get_project(session, project_id)
    if project is None:
        return False
    await session.delete(project)
    await session.flush()
    return True


async def create_endpoint(
    session: AsyncSession,
    project_id: str,
    method: str,
    path: str,
    operation_id: Optional[str] = None,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    parameters: Optional[Any] = None,
    request_body: Optional[Any] = None,
    responses: Optional[Any] = None,
) -> Endpoint:
    """Creates and persists an Endpoint entity for a Project."""
    endpoint = Endpoint(
        project_id=project_id,
        method=method.upper(),
        path=path,
        operation_id=operation_id,
        summary=summary,
        description=description,
        parameters=parameters,
        request_body=request_body,
        responses=responses,
    )
    session.add(endpoint)
    await session.flush()
    await session.refresh(endpoint)
    return endpoint


async def bulk_create_endpoints(
    session: AsyncSession,
    project_id: str,
    endpoints: Sequence[Union[EndpointDefinition, Dict[str, Any]]],
) -> List[Endpoint]:
    """
    Bulk creates Endpoint entities for a Project from EndpointDefinitions or dictionaries.
    """
    db_endpoints: List[Endpoint] = []
    for item in endpoints:
        if isinstance(item, EndpointDefinition):
            # Convert Pydantic schemas to JSON-serializable dictionaries
            params_dump = (
                [p.model_dump(by_alias=True) for p in item.parameters]
                if item.parameters
                else None
            )
            rb_dump = (
                item.request_body.model_dump()
                if item.request_body
                else None
            )
            resp_dump = (
                {k: v.model_dump() for k, v in item.responses.items()}
                if item.responses
                else None
            )

            endpoint = Endpoint(
                project_id=project_id,
                method=item.method.upper(),
                path=item.path,
                operation_id=item.operation_id,
                summary=item.summary,
                description=item.description,
                parameters=params_dump,
                request_body=rb_dump,
                responses=resp_dump,
            )
        elif isinstance(item, dict):
            endpoint = Endpoint(
                project_id=project_id,
                method=str(item.get("method", "GET")).upper(),
                path=str(item.get("path", "/")),
                operation_id=item.get("operation_id"),
                summary=item.get("summary"),
                description=item.get("description"),
                parameters=item.get("parameters"),
                request_body=item.get("request_body"),
                responses=item.get("responses"),
            )
        else:
            continue

        session.add(endpoint)
        db_endpoints.append(endpoint)

    await session.flush()
    for ep in db_endpoints:
        await session.refresh(ep)

    return db_endpoints


async def get_endpoint(session: AsyncSession, endpoint_id: str) -> Optional[Endpoint]:
    """Retrieves an Endpoint by ID, or None if not found."""
    stmt = select(Endpoint).where(Endpoint.id == endpoint_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_endpoints_by_project(
    session: AsyncSession,
    project_id: str,
) -> List[Endpoint]:
    """Lists all Endpoints for a Project ordered by path and method."""
    stmt = (
        select(Endpoint)
        .where(Endpoint.project_id == project_id)
        .order_by(Endpoint.path.asc(), Endpoint.method.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_endpoint_by_operation(
    session: AsyncSession,
    project_id: str,
    method: str,
    path: str,
) -> Optional[Endpoint]:
    """Finds a specific Endpoint in a Project matching uppercase HTTP method and path."""
    stmt = select(Endpoint).where(
        Endpoint.project_id == project_id,
        Endpoint.method == method.upper(),
        Endpoint.path == path,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

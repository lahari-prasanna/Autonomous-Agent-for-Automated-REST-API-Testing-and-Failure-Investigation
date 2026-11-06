"""
Project Management API Router (Phase 5C).
Provides endpoints to create, list, inspect, and delete Projects and their Endpoints.
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.deps import get_db, get_project_service
from backend.app.schemas.api import (
    EndpointResponse,
    ProjectCreateRequest,
    ProjectDetailResponse,
    ProjectResponse,
)
from backend.app.services.project_service import ProjectService
from backend.app.services.spec_parser import (
    SpecParserError,
    SpecParsingError,
    SpecSecurityError,
    SpecValidationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post(
    "",
    response_model=ProjectDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project from an OpenAPI specification",
)
async def create_project(
    payload: ProjectCreateRequest,
    session: AsyncSession = Depends(get_db),
    service: ProjectService = Depends(get_project_service),
) -> ProjectDetailResponse:
    """
    Validates the uploaded OpenAPI 3.0.x / 3.1.x specification (YAML or JSON),
    extracts normalized endpoint operations, and persists the Project and its Endpoints.
    """
    try:
        project, endpoints = await service.create_project_from_spec(
            session=session,
            name=payload.name,
            spec_content=payload.spec_content,
            description=payload.description,
            base_url_override=payload.base_url_override,
        )
    except SpecParsingError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid specification syntax: {err}",
        ) from err
    except SpecValidationError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"OpenAPI validation error: {err}",
        ) from err
    except SpecSecurityError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Specification security violation: {err}",
        ) from err
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(err),
        ) from err

    ep_responses = [EndpointResponse.model_validate(ep) for ep in endpoints]
    return ProjectDetailResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        base_url=project.base_url,
        spec_title=project.spec_title,
        spec_version=project.spec_version,
        endpoints_count=len(endpoints),
        created_at=project.created_at,
        updated_at=project.updated_at,
        endpoints=ep_responses,
    )


@router.get(
    "",
    response_model=List[ProjectResponse],
    status_code=status.HTTP_200_OK,
    summary="List all projects with pagination",
)
async def list_projects(
    skip: int = Query(0, ge=0, description="Offset pagination index"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum items per page"),
    session: AsyncSession = Depends(get_db),
    service: ProjectService = Depends(get_project_service),
) -> List[ProjectResponse]:
    """Retrieves all projects ordered by creation timestamp descending."""
    projects = await service.list_projects(session=session, skip=skip, limit=limit)
    return [
        ProjectResponse(
            id=p.id,
            name=p.name,
            description=p.description,
            base_url=p.base_url,
            spec_title=p.spec_title,
            spec_version=p.spec_version,
            endpoints_count=len(p.endpoints) if hasattr(p, "endpoints") and p.endpoints else 0,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in projects
    ]


@router.get(
    "/{project_id}",
    response_model=ProjectDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve project details with extracted endpoints",
)
async def get_project(
    project_id: str,
    session: AsyncSession = Depends(get_db),
    service: ProjectService = Depends(get_project_service),
) -> ProjectDetailResponse:
    """Retrieves a Project by ID along with its extracted Endpoints."""
    project = await service.get_project(session=session, project_id=project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with ID '{project_id}' not found.",
        )

    endpoints = await service.get_project_endpoints(session=session, project_id=project_id)
    ep_responses = [EndpointResponse.model_validate(ep) for ep in endpoints]

    return ProjectDetailResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        base_url=project.base_url,
        spec_title=project.spec_title,
        spec_version=project.spec_version,
        endpoints_count=len(endpoints),
        created_at=project.created_at,
        updated_at=project.updated_at,
        endpoints=ep_responses,
    )


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project and cascade associated records",
)
async def delete_project(
    project_id: str,
    session: AsyncSession = Depends(get_db),
    service: ProjectService = Depends(get_project_service),
) -> None:
    """Deletes a Project and all associated Endpoints, Runs, and Reports."""
    deleted = await service.delete_project(session=session, project_id=project_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with ID '{project_id}' not found.",
        )


@router.get(
    "/{project_id}/endpoints",
    response_model=List[EndpointResponse],
    status_code=status.HTTP_200_OK,
    summary="List extracted endpoints for a project",
)
async def list_project_endpoints(
    project_id: str,
    session: AsyncSession = Depends(get_db),
    service: ProjectService = Depends(get_project_service),
) -> List[EndpointResponse]:
    """Retrieves all Endpoints belonging to a Project."""
    project = await service.get_project(session=session, project_id=project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with ID '{project_id}' not found.",
        )

    endpoints = await service.get_project_endpoints(session=session, project_id=project_id)
    return [EndpointResponse.model_validate(ep) for ep in endpoints]

"""
Project Application Service (Phase 5B).
Orchestrates OpenAPI specification validation, operation extraction,
and persistence of Projects and Endpoints.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.endpoint import Endpoint
from backend.app.models.project import Project
from backend.app.repositories import project_repo
from backend.app.schemas.sanitizer import ReportDataSanitizer
from backend.app.schemas.spec import ParsedAPISpec
from backend.app.services.spec_parser import (
    OpenAPIParser,
    SpecParserError,
    SpecParsingError,
    SpecSecurityError,
    SpecValidationError,
)

logger = logging.getLogger(__name__)


class ProjectService:
    """
    Application service managing Projects and their normalized Endpoints.
    Delegates OpenAPI parsing to OpenAPIParser and persistence to project_repo.
    """

    def __init__(self, parser: Optional[OpenAPIParser] = None) -> None:
        self.parser = parser or OpenAPIParser()

    async def create_project_from_spec(
        self,
        session: AsyncSession,
        name: str,
        spec_content: Union[str, Dict[str, Any]],
        description: Optional[str] = None,
        base_url_override: Optional[str] = None,
    ) -> Tuple[Project, List[Endpoint]]:
        """
        Validates and parses an OpenAPI specification (YAML/JSON text or dictionary),
        extracts normalized endpoint operations, and persists both Project and Endpoints.

        Raises:
            SpecParsingError: If raw text cannot be parsed as JSON or YAML.
            SpecValidationError: If specification is missing required OpenAPI structure.
            SpecSecurityError: If specification contains prohibited remote/file references.
            ValueError: If input parameters are invalid.
        """
        if not name or not name.strip():
            raise ValueError("Project name must not be empty.")

        # 1. Parse and validate specification
        if isinstance(spec_content, dict):
            raw_text = json.dumps(spec_content, indent=2)
            parsed_spec: ParsedAPISpec = self.parser.parse_dict(spec_content)
        elif isinstance(spec_content, str):
            if not spec_content.strip():
                raise ValueError("Specification content must not be empty.")
            raw_text = spec_content
            parsed_spec = self.parser.parse_string(spec_content)
        else:
            raise ValueError("spec_content must be either a string or a dictionary.")

        # 2. Determine effective base URL
        effective_base_url: Optional[str] = None
        if base_url_override:
            effective_base_url = (
                ReportDataSanitizer.sanitize_url(base_url_override) or base_url_override.strip()
            )
        elif parsed_spec.servers:
            first_srv = parsed_spec.servers[0]
            effective_base_url = (
                ReportDataSanitizer.sanitize_url(first_srv) or first_srv.strip()
            )

        # 3. Persist Project entity
        sanitized_name = ReportDataSanitizer.sanitize_text(name.strip())
        sanitized_desc = (
            ReportDataSanitizer.sanitize_text(description.strip())
            if description
            else None
        )

        project = await project_repo.create_project(
            session=session,
            name=sanitized_name,
            description=sanitized_desc,
            base_url=effective_base_url,
            spec_raw=raw_text,
            spec_title=parsed_spec.title,
            spec_version=parsed_spec.version,
        )

        # 4. Bulk persist extracted endpoint operations
        endpoints = await project_repo.bulk_create_endpoints(
            session=session,
            project_id=project.id,
            endpoints=parsed_spec.endpoints,
        )

        logger.info(
            "Created project '%s' (id=%s) with %d endpoint(s).",
            project.name,
            project.id,
            len(endpoints),
        )
        return project, endpoints

    async def get_project(
        self,
        session: AsyncSession,
        project_id: str,
    ) -> Optional[Project]:
        """Retrieves a Project by ID."""
        return await project_repo.get_project(session, project_id)

    async def list_projects(
        self,
        session: AsyncSession,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Project]:
        """Lists projects ordered by creation time descending with pagination."""
        return await project_repo.list_projects(session, skip=skip, limit=limit)

    async def get_project_endpoints(
        self,
        session: AsyncSession,
        project_id: str,
    ) -> List[Endpoint]:
        """Lists all Endpoints belonging to a Project."""
        return await project_repo.list_endpoints_by_project(session, project_id)

    async def update_project(
        self,
        session: AsyncSession,
        project_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Optional[Project]:
        """Updates metadata of an existing Project."""
        kwargs: Dict[str, Any] = {}
        if name is not None:
            kwargs["name"] = ReportDataSanitizer.sanitize_text(name.strip())
        if description is not None:
            kwargs["description"] = ReportDataSanitizer.sanitize_text(description.strip())
        if base_url is not None:
            kwargs["base_url"] = (
                ReportDataSanitizer.sanitize_url(base_url) or base_url.strip()
            )

        return await project_repo.update_project(session, project_id, **kwargs)

    async def delete_project(
        self,
        session: AsyncSession,
        project_id: str,
    ) -> bool:
        """Deletes a Project and all associated cascaded records."""
        return await project_repo.delete_project(session, project_id)

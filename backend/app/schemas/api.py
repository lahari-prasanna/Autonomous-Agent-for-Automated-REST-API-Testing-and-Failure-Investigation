"""
API Request and Response Pydantic Schemas (Phase 5C).
Provides structured, validated data transfer objects for Projects, Endpoints, Runs, and Reports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class ProjectCreateRequest(BaseModel):
    """Payload for creating a Project from an OpenAPI specification."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Human-readable project name",
    )
    spec_content: str = Field(
        ...,
        min_length=1,
        description="Raw OpenAPI specification content in JSON or YAML format",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Optional project description or scope notes",
    )
    base_url_override: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="Optional target server base URL overriding specification servers",
    )


class RunCreateRequest(BaseModel):
    """Payload for initiating a test execution run."""
    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Bounded concurrency workers (1 to 5)",
    )
    target_endpoints: Optional[List[str]] = Field(
        default=None,
        description="Optional list of endpoint UUIDs or path filters to restrict test execution",
    )
    custom_base_url: Optional[str] = Field(
        default=None,
        max_length=1024,
        description="Optional execution base URL overriding project configuration",
    )
    safe_dev_mode: bool = Field(
        default=False,
        description="If True, permits verified loopback (127.0.0.1) for local mock testing",
    )


class EndpointResponse(BaseModel):
    """Serialized representation of a normalized API endpoint."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    method: str
    path: str
    operation_id: Optional[str] = None
    summary: Optional[str] = None
    description: Optional[str] = None
    parameters: Optional[Any] = None
    request_body: Optional[Any] = None
    responses: Optional[Any] = None
    created_at: datetime


class ProjectResponse(BaseModel):
    """Summary representation of a Project."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str] = None
    base_url: Optional[str] = None
    spec_title: Optional[str] = None
    spec_version: Optional[str] = None
    endpoints_count: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class ProjectDetailResponse(ProjectResponse):
    """Detailed Project representation including extracted endpoint operations."""
    endpoints: List[EndpointResponse] = Field(default_factory=list)


class RunResponse(BaseModel):
    """Status and metrics representation of an AgentRun session."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    status: str
    concurrency: int
    target_endpoints: Optional[List[str]] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    summary: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: datetime


class DiagnosticReportResponse(BaseModel):
    """Serialized DiagnosticReport output with summary statistics and markdown report."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    report_id: str
    api_title: str
    api_version: str
    base_url: Optional[str] = None
    summary: Dict[str, Any]
    operations: List[Dict[str, Any]]
    global_remediation_summary: Optional[List[str]] = None
    markdown_rendered: Optional[str] = None
    created_at: datetime


class ErrorResponse(BaseModel):
    """Standardized error response payload."""
    detail: str
    error_type: Optional[str] = None

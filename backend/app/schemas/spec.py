"""
Normalized Pydantic models for OpenAPI 3.0.x and 3.1.x specifications.
Preserves rich JSON schema constraints, multiple content types, and response keys.
"""

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class ParameterDefinition(BaseModel):
    """Normalized parameter representation (path, query, header, cookie)."""
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., description="Parameter name")
    in_: Literal["path", "query", "header", "cookie"] = Field(
        ..., alias="in", description="Parameter location"
    )
    required: bool = Field(default=False, description="Whether parameter is required")
    description: Optional[str] = Field(default=None, description="Parameter description")
    deprecated: bool = Field(default=False, description="Whether parameter is deprecated")
    schema_: Dict[str, Any] = Field(
        default_factory=dict,
        alias="schema",
        description="Preserved JSON Schema with all validation constraints intact",
    )
    example: Optional[Any] = Field(default=None, description="Sample parameter value")
    examples: Optional[Dict[str, Any]] = Field(default=None, description="Multiple examples")
    style: Optional[str] = Field(default=None, description="Serialization style")
    explode: Optional[bool] = Field(default=None, description="Serialization explode flag")

    @property
    def schema_dict(self) -> Dict[str, Any]:
        """Convenience property to access schema without conflict."""
        return self.schema_


class RequestBodyDefinition(BaseModel):
    """Normalized request body preserving all media types and schema constraints."""
    description: Optional[str] = Field(default=None, description="Request body description")
    required: bool = Field(default=False, description="Whether request body is required")
    content: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Map of media-type (e.g. application/json) to media-type objects",
    )

    def get_schema(self, media_type: str = "application/json") -> Optional[Dict[str, Any]]:
        """Returns the schema dictionary for a specific media type if present."""
        mt = self.content.get(media_type)
        if mt and isinstance(mt, dict):
            return mt.get("schema")
        return None


class ResponseDefinition(BaseModel):
    """Normalized response definition preserving status code, headers, and media types."""
    status_code: str = Field(
        ..., description="HTTP status code (e.g. '200', '201', '404', 'default', '2XX')"
    )
    description: str = Field(default="", description="Response description")
    headers: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict, description="Response headers"
    )
    content: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        description="Map of media-type (e.g. application/json) to media-type objects",
    )

    def get_schema(self, media_type: str = "application/json") -> Optional[Dict[str, Any]]:
        """Returns the schema dictionary for a specific media type if present."""
        mt = self.content.get(media_type)
        if mt and isinstance(mt, dict):
            return mt.get("schema")
        return None


class SecuritySchemeDefinition(BaseModel):
    """Normalized security scheme representation."""
    type: Literal["apiKey", "http", "oauth2", "openIdConnect"] = Field(
        ..., description="Type of security scheme"
    )
    description: Optional[str] = Field(default=None, description="Scheme description")
    name: Optional[str] = Field(default=None, description="Header or query param name for apiKey")
    in_: Optional[Literal["header", "query", "cookie"]] = Field(
        default=None, alias="in", description="Location for apiKey"
    )
    scheme: Optional[str] = Field(
        default=None, description="HTTP auth scheme (e.g. 'bearer', 'basic')"
    )
    bearer_format: Optional[str] = Field(
        default=None, description="Format hint for bearer token (e.g. 'JWT')"
    )
    flows: Optional[Dict[str, Any]] = Field(
        default=None, description="OAuth2 flow configurations"
    )
    open_id_connect_url: Optional[str] = Field(
        default=None, description="OpenID Connect URL"
    )


class EndpointDefinition(BaseModel):
    """Normalized API endpoint operation with merged parameters and security."""
    path: str = Field(..., description="URL path template (e.g. '/api/products/{id}')")
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE"] = Field(
        ..., description="HTTP method"
    )
    summary: Optional[str] = Field(default=None, description="Short summary of operation")
    description: Optional[str] = Field(default=None, description="Detailed operation description")
    operation_id: Optional[str] = Field(default=None, description="Unique operation identifier")
    tags: List[str] = Field(default_factory=list, description="Tags for categorization")
    deprecated: bool = Field(default=False, description="Whether endpoint is deprecated")
    parameters: List[ParameterDefinition] = Field(
        default_factory=list,
        description="Merged path-level and operation-level parameters",
    )
    request_body: Optional[RequestBodyDefinition] = Field(
        default=None, description="Operation request body definition"
    )
    responses: Dict[str, ResponseDefinition] = Field(
        default_factory=dict,
        description="Map of status codes ('200', '400', 'default') to response definitions",
    )
    security: List[Dict[str, List[str]]] = Field(
        default_factory=list,
        description="Merged security requirements for this endpoint",
    )
    servers: List[str] = Field(
        default_factory=list,
        description="Effective server base URLs for this operation",
    )


class ParsedAPISpec(BaseModel):
    """Complete normalized OpenAPI specification representation."""
    title: str = Field(..., description="API title")
    version: str = Field(..., description="API version")
    openapi_version: str = Field(..., description="Detected OpenAPI specification version")
    description: Optional[str] = Field(default=None, description="API description")
    servers: List[str] = Field(
        default_factory=list, description="Declared root server base URLs"
    )
    endpoints: List[EndpointDefinition] = Field(
        default_factory=list, description="List of all normalized endpoint operations"
    )
    security_schemes: Dict[str, SecuritySchemeDefinition] = Field(
        default_factory=dict, description="Security scheme definitions"
    )
    global_security: List[Dict[str, List[str]]] = Field(
        default_factory=list, description="Root-level security requirements"
    )
    raw_spec: Dict[str, Any] = Field(
        default_factory=dict, description="Fully dereferenced raw specification dictionary"
    )


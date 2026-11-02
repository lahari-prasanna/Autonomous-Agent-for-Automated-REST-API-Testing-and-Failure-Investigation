"""
Pydantic schemas for deterministic HTTP execution.
Defines strongly-typed request envelopes, execution results,
and security-conscious header sanitization.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.security import sanitize_url_for_logging

SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "token",
}


def sanitize_headers_for_logging(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Returns a copy of headers with authorization, cookies, and tokens redacted.
    Never exposes raw secrets in logs or user interfaces.
    """
    if not headers:
        return {}
    sanitized = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADER_NAMES:
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized


class ExecutionRequest(BaseModel):
    """
    Structured HTTP request specification for the execution engine.
    Enforces clamped timeouts (max 10s) and restricted redirect hops (max 3).
    """
    model_config = ConfigDict(populate_by_name=True)

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = Field(
        ..., description="HTTP method"
    )
    url: str = Field(..., description="Target URL")
    headers: Dict[str, str] = Field(default_factory=dict, description="Request headers")
    params: Dict[str, Any] = Field(default_factory=dict, description="URL query parameters")
    json_body: Optional[Any] = Field(default=None, description="JSON payload")
    raw_body: Optional[bytes] = Field(default=None, description="Raw binary payload")
    follow_redirects: bool = Field(default=False, description="Whether to follow redirects manually")
    max_redirects: int = Field(default=3, le=3, description="Maximum permitted redirect hops (clamped to 3)")
    timeout: float = Field(default=10.0, le=10.0, description="Total timeout in seconds (clamped to max 10.0s)")

    @property
    def sanitized_url(self) -> str:
        """Returns URL with credentials redacted."""
        return sanitize_url_for_logging(self.url)

    @property
    def sanitized_headers(self) -> Dict[str, str]:
        """Returns headers with sensitive auth/cookie tokens redacted."""
        return sanitize_headers_for_logging(self.headers)


class ExecutionResult(BaseModel):
    """
    Structured outcome of an executed HTTP test request.
    Captures HTTP envelope, latency measurement, size, and failure classification.
    """
    model_config = ConfigDict(populate_by_name=True)

    request_id: Optional[str] = Field(default=None, description="Optional caller correlation ID")
    method: str = Field(..., description="HTTP method executed")
    url: str = Field(..., description="Final requested URL (sanitized)")
    status_code: int = Field(default=0, description="HTTP response code (0 if network/SSRF failure)")
    headers: Dict[str, str] = Field(default_factory=dict, description="Raw response headers")
    body: Optional[Any] = Field(default=None, description="Parsed JSON object or decoded text preview")
    raw_body_bytes_len: int = Field(default=0, description="Actual received payload byte length")
    latency_ms: float = Field(default=0.0, description="Round-trip latency in milliseconds")
    is_success: bool = Field(
        default=False, description="True if HTTP exchange completed (regardless of 4xx/5xx status)"
    )
    is_network_error: bool = Field(default=False, description="True if socket/connection error occurred")
    is_timeout: bool = Field(default=False, description="True if request exceeded timeout threshold")
    is_ssrf_blocked: bool = Field(default=False, description="True if blocked by SSRF Security Guard")
    error_message: Optional[str] = Field(default=None, description="Error details if execution failed")
    redirect_count: int = Field(default=0, description="Number of manual redirect hops traversed")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description="UTC execution timestamp"
    )

    @property
    def sanitized_headers(self) -> Dict[str, str]:
        """Returns response headers with sensitive cookies redacted."""
        return sanitize_headers_for_logging(self.headers)


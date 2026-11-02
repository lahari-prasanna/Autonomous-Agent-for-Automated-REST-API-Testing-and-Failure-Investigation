"""
Shared typed state and data models for the LangGraph API testing agent (Phase 3A).
Integrates existing OpenAPI schemas, execution envelopes, assertion reports,
and lifecycle diagnostics with zero live HTTP resources in the graph state.
"""

from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from typing_extensions import TypedDict
from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec
from backend.app.services.assertion_evaluator import AssertionReport

# Explicit workflow status enumeration
WorkflowStatus = Literal[
    "idle",
    "spec_loaded",
    "operation_selected",
    "request_ready",
    "executed",
    "evaluated",
    "error",
]

WORKFLOW_STATUSES: Tuple[WorkflowStatus, ...] = (
    "idle",
    "spec_loaded",
    "operation_selected",
    "request_ready",
    "executed",
    "evaluated",
    "error",
)


class AgentError(BaseModel):
    """
    Structured representation of an error encountered during agent workflow execution.
    Preserves error category, step of failure, and rich diagnostic metadata.
    """
    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(..., description="Human-readable summary of the error")
    error_type: str = Field(
        default="generic_error",
        description="Category, code, or exception class name for the error",
    )
    step: Optional[WorkflowStatus] = Field(
        default=None,
        description="Workflow lifecycle step during which the failure occurred",
    )
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary structured context, parameters, or traceback details",
    )

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        step: Optional[WorkflowStatus] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> "AgentError":
        """Convenience constructor to create an AgentError from an active exception."""
        return cls(
            message=str(exc) or exc.__class__.__name__,
            error_type=exc.__class__.__name__,
            step=step,
            details=details or {},
        )


class AgentState(TypedDict, total=False):
    """
    Unified typed state channel for the LangGraph API testing agent.

    Carries the complete lifecycle context across graph nodes:
    - OpenAPI specification and dereferenced operation definitions
    - Selected operation targeting (method, path, operation ID)
    - Deterministic HTTP request configuration
    - Completed execution result envelope
    - Deterministic assertion evaluation report
    - Explicit workflow status and structured error context
    - Traceability metadata and event logs

    Zero live HTTP resources (HTTPX clients, sockets, open streams) are retained.
    All state fields are standard serializable models, primitives, or dicts.
    """
    # 1. Specification Context
    spec: Optional[ParsedAPISpec]
    raw_spec: Optional[Dict[str, Any]]

    # 2. Selected Operation Target
    selected_operation: Optional[EndpointDefinition]
    target_path: Optional[str]
    target_method: Optional[str]
    target_operation_id: Optional[str]

    # 3. Request Configuration
    request_config: Optional[ExecutionRequest]

    # 4. HTTP Execution Outcome
    execution_result: Optional[ExecutionResult]

    # 5. Assertion Evaluation Report
    assertion_report: Optional[AssertionReport]

    # 6. Workflow Lifecycle Status
    status: WorkflowStatus

    # 7. Structured Error Information
    error: Optional[AgentError]

    # 8. Execution Metadata & Traceability
    metadata: Dict[str, Any]
    logs: List[str]


def create_initial_state(
    spec: Optional[ParsedAPISpec] = None,
    raw_spec: Optional[Dict[str, Any]] = None,
    selected_operation: Optional[EndpointDefinition] = None,
    target_path: Optional[str] = None,
    target_method: Optional[str] = None,
    target_operation_id: Optional[str] = None,
    request_config: Optional[ExecutionRequest] = None,
    execution_result: Optional[ExecutionResult] = None,
    assertion_report: Optional[AssertionReport] = None,
    status: Optional[WorkflowStatus] = None,
    error: Optional[AgentError] = None,
    metadata: Optional[Dict[str, Any]] = None,
    logs: Optional[List[str]] = None,
    existing_state: Optional[Union[AgentState, Dict[str, Any]]] = None,
    **kwargs: Any,
) -> AgentState:
    """
    Creates a new AgentState or safely merges initial fields into an existing state.

    Guarantees:
    - Default status is 'idle' if not specified or not already present in existing_state.
    - Existing state values are preserved and never unintentionally overwritten.
    - Mutable containers (metadata, logs) are freshly instantiated to prevent reference leaks.
    - Zero live HTTP resources (clients, sockets, open streams) are permitted or retained.
    """
    base: Dict[str, Any] = dict(existing_state) if existing_state else {}

    # Ensure mutable containers copied from existing_state are distinct instances
    if "metadata" in base:
        base["metadata"] = dict(base["metadata"])
    if "logs" in base:
        base["logs"] = list(base["logs"])

    # 1. Workflow Status (default: 'idle')
    if status is not None:
        base["status"] = status
    elif "status" not in base:
        base["status"] = "idle"

    # 2. Mutable Containers (isolated instances)
    if "metadata" not in base:
        base["metadata"] = dict(metadata) if metadata else {}
    elif metadata is not None:
        base["metadata"].update(metadata)

    if "logs" not in base:
        base["logs"] = list(logs) if logs else []
    elif logs is not None:
        base["logs"].extend(logs)

    # 3. Specification & Operation Context
    if spec is not None or "spec" not in base:
        base["spec"] = spec if spec is not None else base.get("spec")
    if raw_spec is not None or "raw_spec" not in base:
        base["raw_spec"] = raw_spec if raw_spec is not None else base.get("raw_spec")

    if selected_operation is not None or "selected_operation" not in base:
        base["selected_operation"] = (
            selected_operation if selected_operation is not None else base.get("selected_operation")
        )
    if target_path is not None or "target_path" not in base:
        base["target_path"] = target_path if target_path is not None else base.get("target_path")
    if target_method is not None or "target_method" not in base:
        base["target_method"] = target_method if target_method is not None else base.get("target_method")
    if target_operation_id is not None or "target_operation_id" not in base:
        base["target_operation_id"] = (
            target_operation_id if target_operation_id is not None else base.get("target_operation_id")
        )

    # 4. Request, Execution & Evaluation Envelopes
    if request_config is not None or "request_config" not in base:
        base["request_config"] = (
            request_config if request_config is not None else base.get("request_config")
        )
    if execution_result is not None or "execution_result" not in base:
        base["execution_result"] = (
            execution_result if execution_result is not None else base.get("execution_result")
        )
    if assertion_report is not None or "assertion_report" not in base:
        base["assertion_report"] = (
            assertion_report if assertion_report is not None else base.get("assertion_report")
        )

    # 5. Error Information
    if error is not None or "error" not in base:
        base["error"] = error if error is not None else base.get("error")

    # 6. Any additional caller-supplied fields
    for k, v in kwargs.items():
        base[k] = v

    return base  # AgentState typed dictionary

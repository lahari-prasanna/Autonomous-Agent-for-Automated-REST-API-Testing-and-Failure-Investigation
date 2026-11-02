"""
Individual deterministic LangGraph node functions (Phase 3B).
Coordinates OpenAPI parsing, operation selection, request envelope preparation,
SSRF-safe HTTP execution, and deterministic assertion evaluation through AgentState.
"""

import os
import re
from typing import Any, Dict, List, Optional, Union

from backend.app.agent.state import AgentError, AgentState
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec
from backend.app.services.assertion_evaluator import (
    AssertionReport,
    DeterministicAssertionEvaluator,
)
from backend.app.services.http_executor import HTTPExecutor
from backend.app.services.spec_parser import OpenAPIParser, SpecParserError


def load_spec_node(
    state: AgentState,
    parser: Optional[OpenAPIParser] = None,
) -> Dict[str, Any]:
    """
    LangGraph Node: Ingests and normalizes an OpenAPI 3.0.x / 3.1.x specification.
    
    Accepts specification data from:
    - state['spec']: Pre-parsed ParsedAPISpec (passes through).
    - state['raw_spec']: In-memory dict, JSON string, YAML string, or local file path.
    - state['spec_path'] / metadata['spec_path']: Local file path.
    - state['spec_content']: Raw JSON/YAML string.
    
    Returns partial state update with 'spec', 'raw_spec', and status='spec_loaded',
    or status='error' with structured AgentError.
    """
    # 1. Passthrough if already parsed
    existing_spec = state.get("spec")
    if isinstance(existing_spec, ParsedAPISpec):
        return {
            "spec": existing_spec,
            "raw_spec": existing_spec.raw_spec,
            "status": "spec_loaded",
            "logs": [f"Specification '{existing_spec.title}' (v{existing_spec.version}) already loaded."],
        }

    # 2. Extract inputs
    fallback_base_url = (
        state.get("fallback_base_url")
        or state.get("metadata", {}).get("fallback_base_url")
    )
    active_parser = parser or OpenAPIParser(fallback_base_url=fallback_base_url)

    raw_spec = state.get("raw_spec")
    spec_path = (
        state.get("spec_path")
        or state.get("metadata", {}).get("spec_path")
    )
    spec_content = state.get("spec_content")

    try:
        parsed_spec: ParsedAPISpec

        if isinstance(raw_spec, dict):
            parsed_spec = active_parser.parse_dict(raw_spec)
        elif spec_path and os.path.isfile(str(spec_path)):
            parsed_spec = active_parser.parse_file(spec_path, fallback_base_url=fallback_base_url)
        elif isinstance(raw_spec, str) and os.path.isfile(raw_spec):
            parsed_spec = active_parser.parse_file(raw_spec, fallback_base_url=fallback_base_url)
        elif isinstance(raw_spec, str) and raw_spec.strip():
            parsed_spec = active_parser.parse_string(raw_spec)
        elif isinstance(spec_content, str) and spec_content.strip():
            parsed_spec = active_parser.parse_string(spec_content)
        else:
            return {
                "status": "error",
                "error": AgentError(
                    message="No OpenAPI specification found in state. Expected 'raw_spec', 'spec_path', or 'spec'.",
                    error_type="SpecNotFoundError",
                    step="spec_loaded",
                    details={"state_keys": list(state.keys())},
                ),
                "logs": ["Failed to load specification: no input data provided."],
            }

        return {
            "spec": parsed_spec,
            "raw_spec": parsed_spec.raw_spec,
            "status": "spec_loaded",
            "logs": [
                f"Successfully parsed OpenAPI specification: '{parsed_spec.title}' "
                f"(v{parsed_spec.version}, {len(parsed_spec.endpoints)} operations)"
            ],
        }

    except SpecParserError as spe:
        return {
            "status": "error",
            "error": AgentError(
                message=f"Specification parsing failed: {str(spe)}",
                error_type=spe.__class__.__name__,
                step="spec_loaded",
                details={"error": str(spe)},
            ),
            "logs": [f"OpenAPI parser rejected specification: {str(spe)}"],
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": AgentError.from_exception(
                exc,
                step="spec_loaded",
                details={"error": str(exc)},
            ),
            "logs": [f"Unexpected error while loading specification: {str(exc)}"],
        }


def select_operation_node(state: AgentState) -> Dict[str, Any]:
    """
    LangGraph Node: Resolves and selects a target API operation from the parsed specification.
    
    Lookup criteria (in order of priority):
    1. Pre-selected EndpointDefinition in state['selected_operation'].
    2. Operation ID in state['target_operation_id'] or metadata['operation_id'].
    3. Method and Path combination in state['target_method'] + state['target_path'].
    
    Returns partial state update with 'selected_operation', 'target_path',
    'target_method', 'target_operation_id', and status='operation_selected',
    or status='error' with structured AgentError.
    """
    spec = state.get("spec")
    if not isinstance(spec, ParsedAPISpec) or not spec.endpoints:
        return {
            "status": "error",
            "error": AgentError(
                message="Cannot select operation: valid ParsedAPISpec not found in state.",
                error_type="MissingSpecificationError",
                step="operation_selected",
                details={"reason": "Run load_spec_node before selecting an operation."},
            ),
            "logs": ["Operation selection failed: specification not loaded."],
        }

    # 1. Existing EndpointDefinition validation
    existing_op = state.get("selected_operation")
    if isinstance(existing_op, EndpointDefinition):
        return {
            "selected_operation": existing_op,
            "target_path": existing_op.path,
            "target_method": existing_op.method,
            "target_operation_id": existing_op.operation_id,
            "status": "operation_selected",
            "logs": [f"Using pre-selected operation: {existing_op.method} {existing_op.path}"],
        }

    # 2. Extract lookup parameters
    target_op_id = (
        state.get("target_operation_id")
        or state.get("metadata", {}).get("operation_id")
    )
    target_method = (
        state.get("target_method")
        or state.get("metadata", {}).get("method")
    )
    target_path = (
        state.get("target_path")
        or state.get("metadata", {}).get("path")
    )

    matched_op: Optional[EndpointDefinition] = None

    # 3. Match by Operation ID
    if target_op_id:
        op_id_clean = target_op_id.strip()
        for ep in spec.endpoints:
            if ep.operation_id and ep.operation_id.strip() == op_id_clean:
                matched_op = ep
                break

    # 4. Match by Method + Path if not matched by ID
    if not matched_op and target_method and target_path:
        m_clean = target_method.strip().upper()
        p_clean = target_path.strip()
        p_path_only = p_clean.split("?")[0]

        for ep in spec.endpoints:
            if ep.method.upper() == m_clean:
                # Exact path template match
                if ep.path == p_clean or ep.path == p_path_only:
                    matched_op = ep
                    break
                # Suffix / endpoint matching (e.g. target_path has full URL or base prefix)
                if p_clean.endswith(ep.path) or p_path_only.endswith(ep.path):
                    matched_op = ep
                    break

    # 5. Handle missing selection criteria
    if not target_op_id and not (target_method and target_path):
        return {
            "status": "error",
            "error": AgentError(
                message="No operation identifier provided. Expected 'target_operation_id' or 'target_method' + 'target_path'.",
                error_type="MissingOperationIdentifierError",
                step="operation_selected",
                details={"state_keys": list(state.keys())},
            ),
            "logs": ["Operation selection failed: no lookup criteria specified."],
        }

    # 6. Handle not found
    if not matched_op:
        available = [f"{ep.method} {ep.path} (id: {ep.operation_id})" for ep in spec.endpoints[:10]]
        return {
            "status": "error",
            "error": AgentError(
                message=f"Operation not found in specification for operation_id='{target_op_id}', method='{target_method}', path='{target_path}'.",
                error_type="OperationNotFoundError",
                step="operation_selected",
                details={
                    "target_operation_id": target_op_id,
                    "target_method": target_method,
                    "target_path": target_path,
                    "available_sample": available,
                },
            ),
            "logs": [f"Operation lookup failed for id='{target_op_id}', method='{target_method}', path='{target_path}'."],
        }

    return {
        "selected_operation": matched_op,
        "target_path": matched_op.path,
        "target_method": matched_op.method,
        "target_operation_id": matched_op.operation_id,
        "status": "operation_selected",
        "logs": [f"Selected operation: {matched_op.method} {matched_op.path} (operationId: '{matched_op.operation_id}')"],
    }


def prepare_request_node(state: AgentState) -> Dict[str, Any]:
    """
    LangGraph Node: Prepares the deterministic ExecutionRequest envelope.
    
    Synthesizes:
    - HTTP method and URL template from selected_operation.
    - Base URL from state['base_url'], metadata['base_url'], or specification servers.
    - Path parameter substitution into templates (e.g. /products/{id}).
    - Query parameters, request headers, auth tokens, and JSON bodies.
    - Hard architectural constraints: max 10.0s timeout, max 3 redirect hops.
    
    Returns partial state update with 'request_config' and status='request_ready',
    or status='error' with structured AgentError.
    """
    # Passthrough if an ExecutionRequest is already fully configured in state
    existing_req = state.get("request_config")
    if isinstance(existing_req, ExecutionRequest):
        return {
            "request_config": existing_req,
            "status": "request_ready",
            "logs": [f"Using pre-configured ExecutionRequest: {existing_req.method} {existing_req.sanitized_url}"],
        }

    # Ensure an operation has been selected
    op = state.get("selected_operation")
    if not isinstance(op, EndpointDefinition):
        return {
            "status": "error",
            "error": AgentError(
                message="Cannot prepare request: no operation selected in state.",
                error_type="MissingOperationError",
                step="request_ready",
                details={"reason": "Run select_operation_node before prepare_request_node."},
            ),
            "logs": ["Request preparation failed: selected_operation is missing."],
        }

    try:
        # 1. Resolve Base URL
        base_url = (
            state.get("base_url")
            or state.get("metadata", {}).get("base_url")
        )
        if not base_url:
            if op.servers:
                base_url = op.servers[0]
            elif state.get("spec") and state["spec"].servers:
                base_url = state["spec"].servers[0]

        # 2. Resolve Path & Path Parameter Substitution
        raw_path = op.path
        path_params: Dict[str, Any] = {}
        if "path_params" in state and isinstance(state["path_params"], dict):
            path_params.update(state["path_params"])
        if "metadata" in state and isinstance(state["metadata"].get("path_params"), dict):
            path_params.update(state["metadata"]["path_params"])

        # Substitute {param} placeholders in path
        resolved_path = raw_path
        used_path_param_keys = set()
        for placeholder in re.findall(r"\{([a-zA-Z0-9_]+)\}", raw_path):
            if placeholder in path_params:
                resolved_path = resolved_path.replace(f"{{{placeholder}}}", str(path_params[placeholder]))
                used_path_param_keys.add(placeholder)

        # 3. Assemble Complete Target URL
        if base_url:
            target_url = f"{base_url.rstrip('/')}/{resolved_path.lstrip('/')}"
        elif resolved_path.startswith("http://") or resolved_path.startswith("https://"):
            target_url = resolved_path
        else:
            return {
                "status": "error",
                "error": AgentError(
                    message=f"Target URL lacks a base host scheme (resolved path: '{resolved_path}'). Provide 'base_url' in state or metadata.",
                    error_type="MissingBaseUrlError",
                    step="request_ready",
                    details={"path": resolved_path},
                ),
                "logs": [f"Request preparation failed: missing base URL for '{resolved_path}'."],
            }

        # 4. Resolve Query Parameters
        query_params: Dict[str, Any] = {}
        if "params" in state and isinstance(state["params"], dict):
            query_params.update(state["params"])
        if "query_params" in state and isinstance(state["query_params"], dict):
            query_params.update(state["query_params"])
        # Add unused path params as query params if any
        for k, v in path_params.items():
            if k not in used_path_param_keys and k not in query_params:
                query_params[k] = v

        # 5. Resolve Headers & Authorization
        headers: Dict[str, str] = {}
        if "metadata" in state and isinstance(state["metadata"].get("headers"), dict):
            headers.update({str(k): str(v) for k, v in state["metadata"]["headers"].items()})
        if "headers" in state and isinstance(state["headers"], dict):
            headers.update({str(k): str(v) for k, v in state["headers"].items()})
        if "request_headers" in state and isinstance(state["request_headers"], dict):
            headers.update({str(k): str(v) for k, v in state["request_headers"].items()})

        auth_token = (
            state.get("auth_token")
            or state.get("metadata", {}).get("auth_token")
        )
        if auth_token and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {auth_token}"

        # 6. Resolve JSON / Raw Body
        json_body = (
            state.get("json_body")
            if "json_body" in state
            else state.get("request_body")
        )
        raw_body = state.get("raw_body")

        # 7. Clamped Execution Parameters
        raw_timeout = state.get("timeout") or state.get("metadata", {}).get("timeout", 10.0)
        timeout = min(float(raw_timeout), 10.0)

        follow_redirects = bool(
            state.get("follow_redirects", False)
            or state.get("metadata", {}).get("follow_redirects", False)
        )
        raw_redirects = state.get("max_redirects") or state.get("metadata", {}).get("max_redirects", 3)
        max_redirects = min(int(raw_redirects), 3)

        execution_req = ExecutionRequest(
            method=op.method.upper(),
            url=target_url,
            headers=headers,
            params=query_params,
            json_body=json_body,
            raw_body=raw_body,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
            timeout=timeout,
        )

        return {
            "request_config": execution_req,
            "status": "request_ready",
            "logs": [f"Prepared {execution_req.method} request to '{execution_req.sanitized_url}'"],
        }

    except Exception as exc:
        return {
            "status": "error",
            "error": AgentError.from_exception(
                exc,
                step="request_ready",
                details={"error": str(exc)},
            ),
            "logs": [f"Request preparation encountered an exception: {str(exc)}"],
        }


async def execute_request_node(
    state: AgentState,
    executor: Optional[HTTPExecutor] = None,
) -> Dict[str, Any]:
    """
    LangGraph Async Node: Executes the prepared HTTP request using the deterministic executor.
    
    Guarantees:
    - Strictly routes all network calls through SSRFSafeAsyncTransport.
    - Enforces 10-second wall-clock deadline bounding redirects and streaming.
    - Respects 5-connection concurrency limit.
    - Defends against 10 MB decompression bombs while streaming.
    - Never stores live clients, sockets, or open streams in AgentState.
    
    Returns partial state update with 'execution_result' and status='executed',
    or status='error' with structured AgentError if execution could not be attempted.
    """
    req = state.get("request_config")
    if not isinstance(req, ExecutionRequest):
        return {
            "status": "error",
            "error": AgentError(
                message="Cannot execute request: no valid ExecutionRequest in state.",
                error_type="MissingRequestConfigError",
                step="executed",
                details={"reason": "Run prepare_request_node before execute_request_node."},
            ),
            "logs": ["Execution aborted: missing request_config."],
        }

    safe_dev_mode = (
        state.get("safe_dev_mode")
        if "safe_dev_mode" in state
        else state.get("metadata", {}).get(
            "safe_dev_mode",
            os.environ.get("SAFE_DEV_MODE", "").lower() in ("1", "true", "yes"),
        )
    )

    active_executor = executor or HTTPExecutor(safe_dev_mode=safe_dev_mode)

    try:
        result: ExecutionResult = await active_executor.execute(req)

        updates: Dict[str, Any] = {
            "execution_result": result,
            "status": "executed",
        }

        if result.is_timeout:
            updates["logs"] = [f"Request execution timed out: {result.error_message}"]
        elif result.is_ssrf_blocked:
            updates["logs"] = [f"Request execution blocked by SSRF Security Guard: {result.error_message}"]
        elif result.is_network_error:
            updates["logs"] = [f"Request execution failed with network error: {result.error_message}"]
        else:
            updates["logs"] = [
                f"Executed {result.method} {result.url} -> HTTP {result.status_code} "
                f"({result.latency_ms}ms, {result.raw_body_bytes_len} bytes)"
            ]

        return updates

    except Exception as exc:
        return {
            "status": "error",
            "error": AgentError.from_exception(
                exc,
                step="executed",
                details={"error": str(exc)},
            ),
            "logs": [f"Executor failed with unhandled exception: {str(exc)}"],
        }


def evaluate_response_node(
    state: AgentState,
    evaluator: Optional[DeterministicAssertionEvaluator] = None,
) -> Dict[str, Any]:
    """
    LangGraph Node: Validates the execution outcome against the OpenAPI operation schema.
    
    Guarantees:
    - Zero LLM token consumption; deterministic schema and status matching.
    - Evaluates exact status codes, ranges (2XX/4XX/5XX), and default fallbacks.
    - Evaluates Content-Type matching and JSON Schema constraints.
    - Gracefully handles transport failure results (SSRF blocked, timeouts, network errors).
    
    Returns partial state update with 'assertion_report' and status='evaluated',
    or status='error' with structured AgentError if evaluation cannot be performed.
    """
    exec_result = state.get("execution_result")
    if not isinstance(exec_result, ExecutionResult):
        return {
            "status": "error",
            "error": AgentError(
                message="Cannot evaluate response: no ExecutionResult found in state.",
                error_type="MissingExecutionResultError",
                step="evaluated",
                details={"reason": "Run execute_request_node before evaluate_response_node."},
            ),
            "logs": ["Assertion evaluation aborted: missing execution_result."],
        }

    selected_op = state.get("selected_operation")
    spec = state.get("spec")

    if not isinstance(selected_op, EndpointDefinition) and not isinstance(spec, ParsedAPISpec):
        return {
            "status": "error",
            "error": AgentError(
                message="Cannot evaluate response: neither selected_operation nor spec available in state.",
                error_type="MissingOperationDefinitionError",
                step="evaluated",
                details={"reason": "Endpoint schema required for deterministic assertion checks."},
            ),
            "logs": ["Assertion evaluation aborted: no endpoint definition available."],
        }

    raw_spec = state.get("raw_spec") or (spec.raw_spec if spec else None)
    active_evaluator = evaluator or DeterministicAssertionEvaluator(raw_spec=raw_spec)

    try:
        report: AssertionReport
        if isinstance(selected_op, EndpointDefinition):
            report = active_evaluator.evaluate(selected_op, exec_result)
        else:
            report = active_evaluator.evaluate_against_spec(spec, exec_result)

        pass_count = len(report.passed_assertions)
        fail_count = len(report.failed_assertions)

        return {
            "assertion_report": report,
            "status": "evaluated",
            "logs": [
                f"Evaluated {report.method} {report.endpoint_path} -> HTTP {report.status_code} "
                f"(Overall Passed: {report.is_success}; Passed: {pass_count}, Failed: {fail_count})"
            ],
        }

    except Exception as exc:
        return {
            "status": "error",
            "error": AgentError.from_exception(
                exc,
                step="evaluated",
                details={"error": str(exc)},
            ),
            "logs": [f"Assertion evaluator encountered an unexpected exception: {str(exc)}"],
        }

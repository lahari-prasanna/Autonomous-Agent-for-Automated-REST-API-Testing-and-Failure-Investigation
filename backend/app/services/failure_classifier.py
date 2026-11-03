"""
Deterministic Failure Classification Engine Service (Phase 4A).
Interprets AgentState lifecycle, execution outcomes, and assertion reports
to produce structured, serializable failure classifications with strict
allowlist-based evidence sanitization, conservative retry guidance,
and fresh-execution state isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union
import copy
import math
import urllib.parse

if TYPE_CHECKING:
    from backend.app.agent.state import AgentState
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.failure import (
    FailureCategory,
    FailureClassification,
    FailureSeverity,
    RetryGuidance,
    RetryPolicy,
    SanitizedFailureEvidence,
)
from backend.app.schemas.spec import EndpointDefinition
from backend.app.services.assertion_evaluator import AssertionReport, AssertionResult

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class FailureClassifier:
    """
    Deterministic failure classification engine.
    Analyzes workflow state and outputs structured classification with
    allowlisted evidence and conservative retry guidance. Zero LLM dependencies.
    """

    @classmethod
    def classify(cls, state: Union[AgentState, Dict[str, Any]]) -> FailureClassification:
        """
        Main classification entrypoint.
        Evaluates state against strict hierarchical precedence rules.
        """
        # 1. Resolve HTTP method and idempotency heuristic early
        method = cls._extract_http_method(state)
        is_idempotent = method in IDEMPOTENT_METHODS

        # 2. Rule 1: Active Lifecycle Error Precedence
        # Genuine lifecycle errors take absolute precedence over any downstream or stale envelopes.
        lifecycle_classification = cls._check_lifecycle_error(state, method, is_idempotent)
        if lifecycle_classification is not None:
            return lifecycle_classification

        exec_result: Optional[ExecutionResult] = state.get("execution_result")
        op: Optional[EndpointDefinition] = state.get("selected_operation")

        # 3. Rule 2: Missing Execution Result (when no lifecycle error was present)
        if exec_result is None or not isinstance(exec_result, ExecutionResult):
            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.UNKNOWN_FAILURE,
                secondary_categories=[],
                severity=FailureSeverity.LOW,
                retry_guidance=RetryGuidance(
                    policy=RetryPolicy.DO_NOT_RETRY,
                    is_idempotent_method=is_idempotent,
                    reason="State contains neither execution outcome nor structured error context.",
                ),
                summary="Workflow state contains neither execution outcome nor structured error context.",
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    op=op,
                    state=state,
                    lifecycle_step=state.get("status"),
                ),
            )

        # 4. Rule 3: Transport & Security Interception (status_code == 0 or is_success is False before HTTP response)
        transport_classification = cls._check_transport_failure(exec_result, method, is_idempotent, op, state)
        if transport_classification is not None:
            return transport_classification

        # 5. Rule 4: Completed Transport Exchange with Missing Contract Evaluation
        # A completed HTTP transport exchange (100-599) without an assertion report must NEVER
        # automatically pass. Contract compliance cannot be determined.
        assertion_report: Optional[AssertionReport] = state.get("assertion_report")
        status_code = exec_result.status_code
        status_token = state.get("status")

        if assertion_report is None or not isinstance(assertion_report, AssertionReport) or status_token != "evaluated":
            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.EVALUATION_INCOMPLETE,
                secondary_categories=[],
                severity=FailureSeverity.HIGH,
                retry_guidance=RetryGuidance(
                    policy=RetryPolicy.DO_NOT_RETRY,
                    is_idempotent_method=is_idempotent,
                    reason="API contract evaluation stage was not completed; re-running HTTP requests will not verify contract compliance until evaluation is restored.",
                ),
                summary=(
                    f"HTTP {status_code} transport exchange completed, but API contract evaluation "
                    f"was not performed (assertion report is missing or workflow did not reach 'evaluated' stage). "
                    f"Contract compliance cannot be determined."
                ),
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    exec_result=exec_result,
                    op=op,
                    state=state,
                    lifecycle_step=status_token or "executed",
                ),
            )

        # 6. Rule 5: Valid Evaluated Contract Success
        # Trusted ONLY when workflow reached 'evaluated' and there is no active error.
        # Covers both happy-path 2xx and expected contract-compliant negative testing (declared 4xx / 5xx with valid schema).
        if assertion_report.is_success is True:
            path_tmpl = cls._extract_path_template(op, assertion_report, exec_result)
            return FailureClassification(
                has_failure=False,
                primary_category=None,
                secondary_categories=[],
                severity=FailureSeverity.LOW,
                retry_guidance=RetryGuidance(
                    policy=RetryPolicy.DO_NOT_RETRY,
                    is_idempotent_method=is_idempotent,
                    reason="Workflow completed successfully and satisfied all OpenAPI contract assertions; no retry needed.",
                ),
                summary=(
                    f"Operation {method} {path_tmpl} completed successfully with HTTP {assertion_report.status_code} "
                    f"and satisfied all contract assertions."
                ),
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    exec_result=exec_result,
                    assertion_report=assertion_report,
                    op=op,
                    state=state,
                    lifecycle_step="evaluated",
                ),
            )

        # 7. Rule 6: Contract Failure Analysis (assertion_report.is_success is False)
        return cls._classify_contract_failure(
            exec_result=exec_result,
            assertion_report=assertion_report,
            method=method,
            is_idempotent=is_idempotent,
            op=op,
            state=state,
        )

    # ---------------------------------------------------------------------------
    # Internal Classification Checkers
    # ---------------------------------------------------------------------------

    @classmethod
    def _check_lifecycle_error(
        cls, state: Union[AgentState, Dict[str, Any]], method: str, is_idempotent: bool
    ) -> Optional[FailureClassification]:
        """
        Inspects state for active lifecycle failures.
        Pre-execution failures take strict precedence over any downstream envelopes.
        """
        error = state.get("error")
        status = state.get("status")

        if status != "error" and error is None:
            return None

        # Extract structured error attributes safely
        step: Optional[str] = None
        error_type: Optional[str] = None
        if hasattr(error, "step") and hasattr(error, "error_type"):
            step = getattr(error, "step")
            error_type = getattr(error, "error_type")
        elif isinstance(error, dict):
            step = error.get("step")
            error_type = error.get("error_type")

        # Map to appropriate failure category based on error.step and error_type
        op: Optional[EndpointDefinition] = state.get("selected_operation")

        if step in ("spec_loaded", "operation_selected") or error_type in (
            "SpecNotFoundError",
            "SpecValidationError",
            "SpecParserError",
            "MissingSpecificationError",
            "MissingOperationIdentifierError",
            "OperationNotFoundError",
        ):
            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.SPEC_OR_OPERATION_FAILURE,
                secondary_categories=[],
                severity=FailureSeverity.HIGH,
                retry_guidance=RetryGuidance(
                    policy=RetryPolicy.DO_NOT_RETRY,
                    is_idempotent_method=is_idempotent,
                    reason="Specification parsing or operation lookup failed; configuration or route must be corrected.",
                ),
                summary=f"OpenAPI specification or operation lookup failed at step '{step or 'unknown'}': {error_type or 'SpecError'}.",
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    op=op,
                    state=state,
                    error_type=error_type,
                    lifecycle_step=step or status,
                ),
            )

        if step in ("request_ready", "executed") or error_type in (
            "MissingOperationError",
            "MissingBaseUrlError",
            "RequestPreparationError",
            "MissingRequestConfigError",
        ):
            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.REQUEST_PREPARATION_FAILURE,
                secondary_categories=[],
                severity=FailureSeverity.HIGH,
                retry_guidance=RetryGuidance(
                    policy=RetryPolicy.DO_NOT_RETRY,
                    is_idempotent_method=is_idempotent,
                    reason="Request preparation failed; parameters, path variables, or base URL must be provided.",
                ),
                summary=f"Request preparation failed at step '{step or 'unknown'}': {error_type or 'PreparationError'}.",
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    op=op,
                    state=state,
                    error_type=error_type,
                    lifecycle_step=step or status,
                ),
            )

        # Fallback for unrecognized lifecycle errors
        return FailureClassification(
            has_failure=True,
            primary_category=FailureCategory.UNKNOWN_FAILURE,
            secondary_categories=[],
            severity=FailureSeverity.HIGH,
            retry_guidance=RetryGuidance(
                policy=RetryPolicy.DO_NOT_RETRY,
                is_idempotent_method=is_idempotent,
                reason="Unclassified pipeline lifecycle error encountered.",
            ),
            summary=f"Pipeline error '{error_type or 'GenericError'}' occurred at step '{step or status or 'unknown'}'.",
            evidence=cls._build_sanitized_evidence(
                method=method,
                op=op,
                state=state,
                error_type=error_type,
                lifecycle_step=step or status,
            ),
        )

    @classmethod
    def _check_transport_failure(
        cls,
        exec_result: ExecutionResult,
        method: str,
        is_idempotent: bool,
        op: Optional[EndpointDefinition],
        state: Union[AgentState, Dict[str, Any]],
    ) -> Optional[FailureClassification]:
        """
        Classifies pre-response transport failures (SSRF blocks, timeouts, socket/network errors).
        """
        path_tmpl = cls._extract_path_template(op, None, exec_result)

        # 1. SSRF Security Block
        if exec_result.is_ssrf_blocked is True:
            clean_url = cls._sanitize_url(exec_result.url, path_tmpl)
            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.SSRF_SECURITY_BLOCK,
                secondary_categories=[],
                severity=FailureSeverity.CRITICAL,
                retry_guidance=RetryGuidance(
                    policy=RetryPolicy.DO_NOT_RETRY,
                    is_idempotent_method=is_idempotent,
                    reason="Target address was intercepted and blocked by SSRF security guard; retries are strictly forbidden.",
                ),
                summary=f"Security Block: Target address {clean_url or 'destination'} was blocked by SSRFGuard prior to socket connection.",
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    exec_result=exec_result,
                    op=op,
                    state=state,
                    error_type="SSRFSecurityError",
                    lifecycle_step="executed",
                ),
            )

        # 2. Request Timeout
        if exec_result.is_timeout is True:
            policy = RetryPolicy.SAFE_TO_RETRY if is_idempotent else RetryPolicy.UNSAFE_NON_IDEMPOTENT
            reason = (
                f"Request timed out during HTTP execution. HTTP method {method} is protocol-idempotent; "
                f"eligible for bounded retry, subject to application concurrency limits."
                if is_idempotent
                else f"Request timed out during HTTP execution. HTTP method {method} is non-idempotent; "
                f"automated retry risks duplicate state mutation on the server."
            )
            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.REQUEST_TIMEOUT,
                secondary_categories=[],
                severity=FailureSeverity.MEDIUM,
                retry_guidance=RetryGuidance(
                    policy=policy,
                    is_idempotent_method=is_idempotent,
                    suggested_backoff_seconds=2.0 if is_idempotent else None,
                    reason=reason,
                ),
                summary=f"Request timed out during HTTP execution for {method} {path_tmpl}.",
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    exec_result=exec_result,
                    op=op,
                    state=state,
                    error_type="TimeoutException",
                    lifecycle_step="executed",
                ),
            )

        # 3. Network Connection Failure
        if exec_result.is_network_error is True:
            policy = RetryPolicy.SAFE_TO_RETRY if is_idempotent else RetryPolicy.UNSAFE_NON_IDEMPOTENT
            reason = (
                f"Network connection failed (DNS error, socket refusal, or connection reset). "
                f"Method {method} is protocol-idempotent; eligible for bounded retry."
                if is_idempotent
                else f"Network connection failed. Method {method} is non-idempotent; automated retry is unsafe."
            )
            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.NETWORK_FAILURE,
                secondary_categories=[],
                severity=FailureSeverity.HIGH,
                retry_guidance=RetryGuidance(
                    policy=policy,
                    is_idempotent_method=is_idempotent,
                    suggested_backoff_seconds=1.0 if is_idempotent else None,
                    reason=reason,
                ),
                summary=f"Network connection failed (socket refusal, DNS failure, or reset) for {method} {path_tmpl}.",
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    exec_result=exec_result,
                    op=op,
                    state=state,
                    error_type="NetworkError",
                    lifecycle_step="executed",
                ),
            )

        # 4. Other Transport Failure (is_success is False and status_code == 0)
        if exec_result.is_success is False and exec_result.status_code == 0:
            policy = RetryPolicy.SAFE_TO_RETRY if is_idempotent else RetryPolicy.UNSAFE_NON_IDEMPOTENT
            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.TRANSPORT_FAILURE,
                secondary_categories=[],
                severity=FailureSeverity.HIGH,
                retry_guidance=RetryGuidance(
                    policy=policy,
                    is_idempotent_method=is_idempotent,
                    reason=(
                        f"HTTP transport exchange failed before response was received. "
                        f"Method {method} idempotency heuristic: {'safe to retry' if is_idempotent else 'unsafe for non-idempotent retry'}."
                    ),
                ),
                summary=f"HTTP transport exchange failed before completion for {method} {path_tmpl}.",
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    exec_result=exec_result,
                    op=op,
                    state=state,
                    error_type="TransportError",
                    lifecycle_step="executed",
                ),
            )

        return None

    @classmethod
    def _classify_contract_failure(
        cls,
        exec_result: ExecutionResult,
        assertion_report: AssertionReport,
        method: str,
        is_idempotent: bool,
        op: Optional[EndpointDefinition],
        state: Union[AgentState, Dict[str, Any]],
    ) -> FailureClassification:
        """
        Classifies completed HTTP responses where contract assertions failed (assertion_report.is_success is False).
        """
        status_code = assertion_report.status_code
        matched_key = assertion_report.matched_status_key
        path_tmpl = cls._extract_path_template(op, assertion_report, exec_result)

        # A. Case 1: Undeclared 5xx Server Error (500 <= code <= 599 and matched_key is None)
        # Note: SERVER_ERROR is assigned ONLY when matched_key is None.
        if 500 <= status_code <= 599 and matched_key is None:
            # Distinguish 500/501 server crashes from 502/503/504 gateway availability issues
            is_gateway_outage = status_code in (502, 503, 504)
            if is_gateway_outage and is_idempotent:
                policy = RetryPolicy.SAFE_TO_RETRY
                reason = f"HTTP {status_code} indicates transient gateway/service unavailability on protocol-idempotent {method}; eligible for retry with backoff."
                backoff = cls._extract_retry_after(exec_result) or 5.0
            elif is_gateway_outage and not is_idempotent:
                policy = RetryPolicy.UNSAFE_NON_IDEMPOTENT
                reason = f"HTTP {status_code} indicates transient gateway drop, but {method} is non-idempotent; automated retry is unsafe without idempotency key."
                backoff = None
            else:
                policy = RetryPolicy.DO_NOT_RETRY
                reason = f"Undeclared HTTP {status_code} indicates a server defect or unhandled exception; repeating request will trigger identical failure."
                backoff = None

            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.SERVER_ERROR,
                secondary_categories=[FailureCategory.UNEXPECTED_HTTP_STATUS],
                severity=FailureSeverity.CRITICAL,
                retry_guidance=RetryGuidance(
                    policy=policy,
                    is_idempotent_method=is_idempotent,
                    suggested_backoff_seconds=backoff,
                    reason=reason,
                ),
                summary=(
                    f"Undeclared HTTP {status_code} Server Error: {method} {path_tmpl} triggered an unhandled server "
                    f"defect that was not declared in the OpenAPI specification."
                ),
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    exec_result=exec_result,
                    assertion_report=assertion_report,
                    op=op,
                    state=state,
                    lifecycle_step="evaluated",
                ),
            )

        # B. Case 2: Undeclared HTTP Status Code (matched_key is None)
        if matched_key is None:
            secondary: List[FailureCategory] = []
            if status_code == 401:
                secondary.append(FailureCategory.AUTHENTICATION_FAILURE)
                policy = RetryPolicy.REQUIRES_MODIFICATION
                reason = "HTTP 401 Unauthorized was undeclared; valid authentication credentials must be supplied."
                backoff = None
            elif status_code == 403:
                secondary.append(FailureCategory.AUTHORIZATION_FAILURE)
                policy = RetryPolicy.REQUIRES_MODIFICATION
                reason = "HTTP 403 Forbidden was undeclared; caller lacks required permissions."
                backoff = None
            elif status_code == 404:
                secondary.append(FailureCategory.RESOURCE_NOT_FOUND)
                policy = RetryPolicy.REQUIRES_MODIFICATION
                reason = "HTTP 404 Not Found was undeclared; resource identifier or route must be corrected."
                backoff = None
            elif status_code == 429:
                secondary.append(FailureCategory.RATE_LIMITING)
                policy = RetryPolicy.SAFE_TO_RETRY if is_idempotent else RetryPolicy.UNSAFE_NON_IDEMPOTENT
                reason = (
                    f"HTTP 429 Rate Limiting; method {method} is protocol-idempotent, safe to retry with backoff."
                    if is_idempotent
                    else f"HTTP 429 Rate Limiting; method {method} is non-idempotent, automated retry is unsafe."
                )
                backoff = cls._extract_retry_after(exec_result) or 5.0
            elif status_code in (400, 422):
                secondary.append(FailureCategory.REQUEST_VALIDATION_FAILURE)
                policy = RetryPolicy.REQUIRES_MODIFICATION
                reason = f"HTTP {status_code} Request Validation Failure was undeclared; payload or parameters must be adjusted."
                backoff = None
            else:
                policy = RetryPolicy.REQUIRES_MODIFICATION
                reason = f"HTTP status code {status_code} was unexpected and undeclared in the OpenAPI specification."
                backoff = None

            return FailureClassification(
                has_failure=True,
                primary_category=FailureCategory.UNEXPECTED_HTTP_STATUS,
                secondary_categories=secondary,
                severity=FailureSeverity.HIGH if status_code >= 500 else FailureSeverity.MEDIUM,
                retry_guidance=RetryGuidance(
                    policy=policy,
                    is_idempotent_method=is_idempotent,
                    suggested_backoff_seconds=backoff,
                    reason=reason,
                ),
                summary=(
                    f"Unexpected HTTP {status_code}: {method} {path_tmpl} returned status code {status_code} "
                    f"which was not declared in the OpenAPI specification."
                ),
                evidence=cls._build_sanitized_evidence(
                    method=method,
                    exec_result=exec_result,
                    assertion_report=assertion_report,
                    op=op,
                    state=state,
                    lifecycle_step="evaluated",
                ),
            )

        # C. Case 3: Declared Status Code with Broken Response Contract (matched_key is not None)
        # Includes: declared 2xx, declared 4xx, or declared 5xx that failed schema or content-type checks.
        # Notice: SERVER_ERROR is NEVER assigned here because the 5xx status was declared in the spec!
        failed_names = [a.name for a in assertion_report.failed_assertions]
        has_ct_failure = any("content_type" in name for name in failed_names)

        if has_ct_failure:
            primary_cat = FailureCategory.RESPONSE_CONTENT_TYPE_MISMATCH
            secondary_cats = [FailureCategory.RESPONSE_SCHEMA_MISMATCH] if any("schema" in n for n in failed_names) else []
            summary_text = (
                f"Response Content-Type mismatch on {method} {path_tmpl}: observed Content-Type "
                f"did not match any declared media type in OpenAPI definition for status {matched_key}."
            )
        else:
            primary_cat = FailureCategory.RESPONSE_SCHEMA_MISMATCH
            secondary_cats = []  # Strictly excludes SERVER_ERROR on declared responses
            summary_text = (
                f"Response Schema mismatch on {method} {path_tmpl}: HTTP {status_code} response payload "
                f"violated OpenAPI JSON Schema constraints for declared status '{matched_key}'."
            )

        return FailureClassification(
            has_failure=True,
            primary_category=primary_cat,
            secondary_categories=secondary_cats,
            severity=FailureSeverity.MEDIUM,
            retry_guidance=RetryGuidance(
                policy=RetryPolicy.REQUIRES_MODIFICATION,
                is_idempotent_method=is_idempotent,
                reason="Response payload or headers violated documented OpenAPI schema contract; API implementation or specification must be corrected.",
            ),
            summary=summary_text,
            evidence=cls._build_sanitized_evidence(
                method=method,
                exec_result=exec_result,
                assertion_report=assertion_report,
                op=op,
                state=state,
                lifecycle_step="evaluated",
            ),
        )

    # ---------------------------------------------------------------------------
    # Evidence Allowlist Builder & Security Sanitization
    # ---------------------------------------------------------------------------

    @classmethod
    def _build_sanitized_evidence(
        cls,
        method: str,
        exec_result: Optional[ExecutionResult] = None,
        assertion_report: Optional[AssertionReport] = None,
        op: Optional[EndpointDefinition] = None,
        state: Optional[Union[AgentState, Dict[str, Any]]] = None,
        error_type: Optional[str] = None,
        lifecycle_step: Optional[str] = None,
    ) -> SanitizedFailureEvidence:
        """
        Builds diagnostic evidence strictly using an allowlist.
        Guarantees zero exposure of raw request/response bodies, auth headers,
        cookies, API keys, raw exception tracebacks, or URL query parameter values.
        """
        state_dict = state or {}

        # 1. URL & Route Sanitization
        raw_url = exec_result.url if exec_result else None
        if not raw_url and state_dict.get("request_config"):
            req = state_dict["request_config"]
            if isinstance(req, ExecutionRequest):
                raw_url = req.url
        path_template = cls._extract_path_template(op, assertion_report, exec_result)
        sanitized_url = cls._sanitize_url(raw_url, path_template)

        # 2. Status & Latency Envelopes
        status_code = assertion_report.status_code if assertion_report else (exec_result.status_code if exec_result else None)
        latency_ms = exec_result.latency_ms if exec_result else None

        # 3. Contract Assertions & Keywords
        matched_status_key = assertion_report.matched_status_key if assertion_report else None
        expected_status_codes = cls._extract_expected_statuses(op, assertion_report)
        failed_assertion_types: List[str] = []
        json_paths: List[str] = []
        schema_keywords: List[str] = []
        contract_summaries: List[str] = []

        if assertion_report:
            for a in assertion_report.failed_assertions:
                if a.name not in failed_assertion_types:
                    failed_assertion_types.append(a.name)
                if a.json_path and a.json_path not in json_paths:
                    json_paths.append(a.json_path)

                # Extract safe schema keyword from validator name
                kw = cls._extract_schema_keyword(a.name)
                if kw and kw not in schema_keywords:
                    schema_keywords.append(kw)

                # Synthesize safe, allowlisted contract violation summary (no raw body exposure)
                summary_msg = cls._synthesize_safe_assertion_summary(a)
                if summary_msg and summary_msg not in contract_summaries:
                    contract_summaries.append(summary_msg)

        # 4. Content-Type Extraction
        observed_content_type = cls._extract_observed_content_type(exec_result)
        expected_content_types = cls._extract_expected_content_types(op, matched_status_key)

        return SanitizedFailureEvidence(
            http_method=method,
            sanitized_url=sanitized_url,
            path_template=path_template,
            status_code=status_code,
            latency_ms=latency_ms,
            matched_status_key=matched_status_key,
            expected_status_codes=expected_status_codes,
            failed_assertion_types=failed_assertion_types,
            json_paths=json_paths,
            schema_keywords=schema_keywords,
            contract_violation_summaries=contract_summaries,
            observed_content_type=observed_content_type,
            expected_content_types=expected_content_types,
            error_type=error_type,
            lifecycle_step=lifecycle_step,
        )

    # ---------------------------------------------------------------------------
    # Sanitization and Extraction Helper Functions
    # ---------------------------------------------------------------------------

    @staticmethod
    def _sanitize_url(raw_url: Optional[str], path_template: Optional[str] = None) -> Optional[str]:
        """
        Sanitizes target URL:
        - Strips userinfo credentials (http://user:pass@host -> http://host)
        - Strips sensitive path values if a path template is provided (e.g. /api/users/{id})
        - Strips query parameter values, keeping parameter names only (e.g. ?limit&offset)
        - Strips fragment identifiers
        """
        if not raw_url:
            return None
        try:
            if "://" in raw_url:
                scheme, rest = raw_url.split("://", 1)
                first_slash = rest.find("/")
                first_q = rest.find("?")
                split_idx = -1
                if first_slash != -1 and first_q != -1:
                    split_idx = min(first_slash, first_q)
                elif first_slash != -1:
                    split_idx = first_slash
                elif first_q != -1:
                    split_idx = first_q

                if split_idx != -1:
                    authority = rest[:split_idx]
                    path_query = rest[split_idx:]
                else:
                    authority = rest
                    path_query = ""

                if "@" in authority:
                    authority = authority.rsplit("@", 1)[1]

                pq = urllib.parse.urlsplit(path_query)
                sanitized_query = ""
                if pq.query:
                    q_tuples = urllib.parse.parse_qsl(pq.query, keep_blank_values=True)
                    sanitized_query = "&".join(urllib.parse.quote(k) for k, _ in q_tuples)

                resolved_path = (
                    path_template
                    if (path_template and path_template.startswith("/") and not path_template.startswith("/unknown_route"))
                    else pq.path
                )
                query_part = f"?{sanitized_query}" if sanitized_query else ""
                return f"{scheme}://{authority}{resolved_path}{query_part}"
            return raw_url
        except Exception:
            return None

    @staticmethod
    def _extract_http_method(state: Union[AgentState, Dict[str, Any]]) -> str:
        """Resolves HTTP method from available state envelopes or defaults to GET."""
        req = state.get("request_config")
        if isinstance(req, ExecutionRequest) and req.method:
            return req.method.upper()
        op = state.get("selected_operation")
        if isinstance(op, EndpointDefinition) and op.method:
            return op.method.upper()
        exec_res = state.get("execution_result")
        if isinstance(exec_res, ExecutionResult) and exec_res.method:
            return exec_res.method.upper()
        raw_m = state.get("target_method")
        if isinstance(raw_m, str) and raw_m.strip():
            return raw_m.strip().upper()
        return "GET"

    @staticmethod
    def _extract_path_template(
        op: Optional[EndpointDefinition],
        assertion_report: Optional[AssertionReport] = None,
        exec_result: Optional[ExecutionResult] = None,
    ) -> str:
        """Extracts normalized OpenAPI path template (e.g. /api/products/{id})."""
        if op and op.path:
            return op.path
        if assertion_report and assertion_report.endpoint_path:
            return assertion_report.endpoint_path
        if exec_result and exec_result.url:
            try:
                return urllib.parse.urlsplit(exec_result.url).path or "/"
            except Exception:
                pass
        return "/unknown_route"

    @staticmethod
    def _extract_expected_statuses(
        op: Optional[EndpointDefinition], assertion_report: Optional[AssertionReport] = None
    ) -> List[str]:
        """Collects declared response status codes from endpoint definition."""
        if op and op.responses:
            return sorted(list(op.responses.keys()))
        if assertion_report:
            for a in assertion_report.assertions:
                if a.name == "status_code_match" and isinstance(a.expected, (list, tuple)):
                    return sorted([str(x) for x in a.expected])
        return []

    @staticmethod
    def _extract_schema_keyword(assertion_name: str) -> Optional[str]:
        """Extracts JSON Schema keyword from assertion check identifier."""
        if assertion_name.startswith("schema_"):
            return assertion_name[len("schema_") :]
        if assertion_name == "status_code_match":
            return "status_code"
        if assertion_name == "content_type_match":
            return "content_type"
        return None

    @staticmethod
    def _synthesize_safe_assertion_summary(assertion: AssertionResult) -> str:
        """
        Synthesizes a rule-based description of the contract violation without
        dumping raw response payloads or sensitive customer values.
        """
        name = assertion.name
        path = assertion.json_path or "/"

        if name == "status_code_match":
            return f"HTTP status code was unexpected and undeclared in the OpenAPI specification."
        if name == "content_type_match":
            return f"Response Content-Type header did not match OpenAPI contract media types."
        if name == "schema_required":
            return f"Missing required property at path '{path}'."
        if name == "schema_type":
            return f"Data type mismatch at path '{path}'."
        if name.startswith("schema_"):
            kw = name[len("schema_") :]
            return f"JSON Schema constraint '{kw}' violated at path '{path}'."

        return f"Contract assertion '{name}' failed at path '{path}'."

    @staticmethod
    def _extract_observed_content_type(exec_result: Optional[ExecutionResult]) -> Optional[str]:
        """Extracts response Content-Type MIME type parameter-free."""
        if not exec_result or not exec_result.headers:
            return None
        for k, v in exec_result.headers.items():
            if k.lower() == "content-type":
                return v.split(";")[0].strip().lower()
        return None

    @staticmethod
    def _extract_expected_content_types(
        op: Optional[EndpointDefinition], matched_status_key: Optional[str]
    ) -> List[str]:
        """Extracts declared media types for the matched status definition."""
        if not op or not op.responses or not matched_status_key:
            return []
        resp_def = op.responses.get(matched_status_key)
        if resp_def and resp_def.content:
            return sorted(list(resp_def.content.keys()))
        return []

    @staticmethod
    def _extract_retry_after(exec_result: Optional[ExecutionResult]) -> Optional[float]:
        """Extracts numeric delay from Retry-After response header if present."""
        if not exec_result or not exec_result.headers:
            return None
        for k, v in exec_result.headers.items():
            if k.lower() == "retry-after":
                try:
                    return float(v.strip())
                except ValueError:
                    return None
        return None


TRANSIENT_STATE_KEYS = frozenset({"status", "error", "execution_result", "assertion_report"})
MAX_HISTORY_ENTRIES = 5
MAX_STR_LEN = 64
MAX_PATH_LEN = 256


def _safe_get(obj: Any, key: str, default: Any = None) -> Any:
    """
    Safely retrieves a field or attribute from dicts, Pydantic models, or objects,
    returning default if the object is None, a primitive, or lacks the field.
    Never calls .get() on non-dict objects or returns callable methods.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, key):
        try:
            val = getattr(obj, key)
            if callable(val):
                return default
            return default if val is None else val
        except Exception:
            return default
    return default


def _clean_string(val: Any, max_len: int = MAX_STR_LEN) -> Optional[str]:
    """
    Validates that val is a string, strips whitespace, and truncates to max_len.
    Never stringifies arbitrary objects (e.g. dicts, models, exceptions, numbers).
    Returns None if val is not a string or is empty after stripping.
    """
    if isinstance(val, str):
        cleaned = val.strip()
        if cleaned:
            return cleaned[:max_len]
    return None


def _clean_int(val: Any, min_val: int = 0, max_val: Optional[int] = None) -> Optional[int]:
    """
    Validates that val is an integer (and explicitly not a boolean),
    optionally within [min_val, max_val].
    """
    if isinstance(val, int) and not isinstance(val, bool):
        if val >= min_val and (max_val is None or val <= max_val):
            return val
    return None


def _clean_float(val: Any, min_val: float = 0.0) -> Optional[float]:
    """
    Validates that val is numeric (int or float, not bool) and >= min_val.
    Returns float rounded to 2 decimal places.
    """
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        f = float(val)
        if f >= min_val and not (math.isnan(f) or math.isinf(f)):
            return round(f, 2)
    return None


def _clean_path_template(val: Any, max_len: int = MAX_PATH_LEN) -> Optional[str]:
    """
    Validates that val is a genuine OpenAPI path template.
    Accepts only strings beginning with '/'.
    Rejects URLs containing '://', query strings ('?'), and fragments ('#').
    Applies the max_len (default MAX_PATH_LEN=256) character limit.
    Returns None for invalid values.
    """
    cleaned = _clean_string(val, max_len=max_len)
    if not cleaned:
        return None
    # Must start with '/', and must not contain URI scheme, query parameters, or fragments
    if not cleaned.startswith("/") or "://" in cleaned or "?" in cleaned or "#" in cleaned:
        return None
    return cleaned


def _match_spec_endpoint_path(
    candidate_path: Optional[str],
    candidate_method: Optional[str],
    spec: Any,
) -> Optional[str]:
    """
    Verifies candidate_path against actual spec.endpoints in the parsed specification.
    Also validates candidate_method when available.
    Returns the normalized matched endpoint path template, or None if no match is found,
    if spec is missing, or if method mismatches.
    """
    if not candidate_path or spec is None:
        return None

    endpoints = _safe_get(spec, "endpoints")
    if not isinstance(endpoints, (list, tuple)) or not endpoints:
        return None

    clean_cand_path = str(candidate_path).strip()
    norm_cand_method = str(candidate_method).strip().upper() if candidate_method else None

    for ep in endpoints:
        ep_path = _safe_get(ep, "path")
        ep_method = _safe_get(ep, "method")
        norm_ep_method = str(ep_method).strip().upper() if ep_method else None

        # Route template must match exactly
        if ep_path and str(ep_path).strip() == clean_cand_path:
            # If candidate_method is available, it must match the endpoint method
            if norm_cand_method is None or norm_ep_method is None or norm_cand_method == norm_ep_method:
                return _clean_path_template(ep_path, MAX_PATH_LEN)

    return None


def _extract_compact_history_entry(
    status: Optional[Any] = None,
    execution_result: Optional[Any] = None,
    assertion_report: Optional[Any] = None,
    error: Optional[Any] = None,
    selected_operation: Optional[Any] = None,
    target_path: Optional[Any] = None,
    target_method: Optional[Any] = None,
    spec: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """
    Constructs a compact, allowlisted, JSON-serializable historical record.

    Explicitly Excludes:
    - Request and response bodies.
    - Raw request and response headers.
    - Authorization headers, cookies, API keys, passwords, and tokens.
    - Raw URL query values and credentials.
    - Concrete URLs or raw path parameters containing potential secrets.
    - Assertion 'actual' payloads.
    - Arbitrary error messages, stack traces, and unstructured details dictionaries.

    Includes Only Safe Allowlisted Primitives:
    - status: str (e.g. 'evaluated', 'error') clamped to MAX_STR_LEN (64)
    - method: str (e.g. 'GET', 'POST') clamped to 16
    - path_template: str (e.g. '/api/products/{id}') clamped to MAX_PATH_LEN (256)
    - status_code: int (100..599)
    - latency_ms: float (>= 0, 2 decimal places)
    - passed_assertions: int (>= 0)
    - failed_assertions: int (>= 0)
    - error_type: str clamped to MAX_STR_LEN (64)
    - error_step: str clamped to MAX_STR_LEN (64)
    """
    if execution_result is None and assertion_report is None and error is None and status in (None, "idle"):
        return None

    # 1. Previous workflow status (validated string, clamped to MAX_STR_LEN)
    entry_status = _clean_string(status, MAX_STR_LEN)

    # 2. HTTP method (validated string, uppercase, clamped to 16 chars)
    raw_m = _safe_get(execution_result, "method")
    if not raw_m:
        raw_m = _safe_get(selected_operation, "method")
    if not raw_m and target_method:
        raw_m = target_method
    method = _clean_string(raw_m, 16)
    if method:
        method = method.upper()

    # 3. Normalized OpenAPI route template
    # Contract Requirement: Every archived path_template must be verified against
    # actual spec.endpoints in the parsed specification.
    # - Do not trust EndpointDefinition solely because of Python type.
    # - Arbitrary dictionaries and caller-supplied objects must never establish path provenance independently.
    # - assertion_report.endpoint_path may be used ONLY when:
    #   * parsed specification exists
    #   * explicit endpoint_lookup assertion exists and passed
    #   * no conflicting failed lookup exists
    #   * report path matches an actual endpoint template in spec.endpoints
    #   * method matches when available
    # - Missing or failed endpoint lookup must result in path_template=None when using assertion report.
    # - Never fall back to target_path, execution URLs, or concrete request paths.
    # - Always apply _clean_path_template() after specification matching.
    path_template: Optional[str] = None
    resolved_path: Optional[str] = None

    # Step A: Verify selected_operation against parsed specification
    if isinstance(selected_operation, EndpointDefinition) and spec is not None:
        resolved_path = _match_spec_endpoint_path(
            candidate_path=selected_operation.path,
            candidate_method=selected_operation.method or method,
            spec=spec,
        )

    # Step B: If not resolved from selected_operation, evaluate assertion_report.endpoint_path
    if not resolved_path and assertion_report is not None and spec is not None:
        has_passed_lookup = False
        has_failed_lookup = False
        assertions_list = _safe_get(assertion_report, "assertions")
        if isinstance(assertions_list, (list, tuple)):
            for a in assertions_list:
                if _safe_get(a, "name") == "endpoint_lookup":
                    if _safe_get(a, "passed") is True:
                        has_passed_lookup = True
                    else:
                        has_failed_lookup = True

        # Explicit endpoint_lookup assertion must exist and have passed with zero failed lookups
        if has_passed_lookup and not has_failed_lookup:
            cand_path = _safe_get(assertion_report, "endpoint_path")
            cand_method = _safe_get(assertion_report, "method") or method
            resolved_path = _match_spec_endpoint_path(
                candidate_path=cand_path,
                candidate_method=cand_method,
                spec=spec,
            )

    # Always apply _clean_path_template() after specification matching
    if resolved_path:
        path_template = _clean_path_template(resolved_path, MAX_PATH_LEN)
    else:
        path_template = None

    # 4. HTTP status code & Latency
    raw_code = _safe_get(execution_result, "status_code")
    if raw_code is None:
        raw_code = _safe_get(assertion_report, "status_code")
    status_code = _clean_int(raw_code, min_val=100, max_val=599)

    raw_lat = _safe_get(execution_result, "latency_ms")
    latency_ms = _clean_float(raw_lat, min_val=0.0)

    # 5. Assertion counts (strictly count integers; zero 'actual' payloads)
    passed_assertions = 0
    failed_assertions = 0
    assertions_list = _safe_get(assertion_report, "assertions")
    if isinstance(assertions_list, (list, tuple)):
        for a in assertions_list:
            is_pass = _safe_get(a, "passed")
            if is_pass is True:
                passed_assertions += 1
            elif is_pass is False:
                failed_assertions += 1

    # 6. Safe error type and workflow step (zero tracebacks or details)
    raw_type = _safe_get(error, "error_type")
    error_type = _clean_string(raw_type, MAX_STR_LEN)

    raw_step = _safe_get(error, "step")
    error_step = _clean_string(raw_step, MAX_STR_LEN)

    return {
        "status": entry_status,
        "method": method,
        "path_template": path_template,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "passed_assertions": passed_assertions,
        "failed_assertions": failed_assertions,
        "error_type": error_type,
        "error_step": error_step,
    }


def _sanitize_history_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """
    Sanitizes an existing history entry into the standard compact allowlisted format.
    Strips raw Pydantic models, response bodies, headers, and unsafe attributes.
    Safely handles malformed entries (primitives, None, unexpected structures) without crashing.
    Enforces MAX_STR_LEN (64) and MAX_PATH_LEN (256) bounds.
    """
    if entry is None or isinstance(entry, (str, int, float, bool, list, tuple, set, bytes)):
        return None

    # Handle legacy raw entries that contained raw Pydantic models or execution envelopes
    has_legacy = any(_safe_get(entry, k) is not None for k in ("execution_result", "assertion_report", "error"))
    if has_legacy:
        return _extract_compact_history_entry(
            status=_safe_get(entry, "status"),
            execution_result=_safe_get(entry, "execution_result"),
            assertion_report=_safe_get(entry, "assertion_report"),
            error=_safe_get(entry, "error"),
            selected_operation=_safe_get(entry, "selected_operation"),
            spec=_safe_get(entry, "spec"),
        )

    # Allowlist safe keys only from an existing compact entry
    status = _clean_string(_safe_get(entry, "status"), MAX_STR_LEN)
    raw_m = _clean_string(_safe_get(entry, "method"), 16)
    method = raw_m.upper() if raw_m else None
    path_template = _clean_path_template(_safe_get(entry, "path_template"), MAX_PATH_LEN)
    status_code = _clean_int(_safe_get(entry, "status_code"), min_val=100, max_val=599)
    latency_ms = _clean_float(_safe_get(entry, "latency_ms"), min_val=0.0)
    passed_assertions = _clean_int(_safe_get(entry, "passed_assertions"), min_val=0) or 0
    failed_assertions = _clean_int(_safe_get(entry, "failed_assertions"), min_val=0) or 0
    error_type = _clean_string(_safe_get(entry, "error_type"), MAX_STR_LEN)
    error_step = _clean_string(_safe_get(entry, "error_step"), MAX_STR_LEN)

    # Discard entries containing zero substantive information
    if (
        status is None
        and method is None
        and path_template is None
        and status_code is None
        and latency_ms is None
        and passed_assertions == 0
        and failed_assertions == 0
        and error_type is None
        and error_step is None
    ):
        return None

    return {
        "status": status,
        "method": method,
        "path_template": path_template,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "passed_assertions": passed_assertions,
        "failed_assertions": failed_assertions,
        "error_type": error_type,
        "error_step": error_step,
    }


def create_fresh_run_state(
    prior_state: Optional[Union[AgentState, Dict[str, Any]]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """
    Creates fresh transient execution state for a new workflow invocation (Phase 4A contract).

    Guarantees:
    1. Active Transient State is strictly reset to clean initial values:
       - 'status' is reset strictly to 'idle'.
       - 'error' is reset strictly to None.
       - 'execution_result' is reset strictly to None.
       - 'assertion_report' is reset strictly to None.
       Caller-supplied attempts to override transient execution fields are rejected with ValueError.

    2. Reusable Configuration is preserved across runs:
       - 'spec': Retained.
       - 'raw_spec': Retained.
       - 'selected_operation': Retained.
       - 'target_path': Retained.
       - 'target_method': Retained.
       - 'target_operation_id': Retained.
       - 'request_config': Retained (unless safely overridden).
       - Safe configuration within metadata (base_url, safe_dev_mode, etc.) is retained.

    3. Historical Execution Data is safe, compact, serializable, and bounded:
       - History entries are strictly allowlisted dictionaries containing primitives (status,
         method, path_template, status_code, latency_ms, passed/failed counts, error_type/step).
       - Raw request/response bodies, headers, auth tokens, cookies, passwords, URL queries,
         and assertion 'actual' payloads are unconditionally excluded.
       - Existing history is sanitized to purge any legacy raw objects or unsafe attributes.
       - Total history is strictly capped at the 5 most recent entries, preserving ordering.

    4. Isolation & Overrides:
       - Mutable containers ('metadata', 'logs') are cleanly isolated.
       - Safe configuration overrides are applied to configuration fields only.
    """
    # 1. Reject attempts to override transient execution state
    for k in overrides:
        if k in TRANSIENT_STATE_KEYS:
            raise ValueError(
                f"Cannot override transient execution field '{k}'. "
                f"Transient execution fields ({', '.join(sorted(TRANSIENT_STATE_KEYS))}) "
                f"are managed exclusively by the workflow execution engine and must initialize to clean defaults."
            )

    base: Dict[str, Any] = {}
    prior_dict = dict(prior_state) if prior_state else {}

    # 2. Copy metadata and logs cleanly
    raw_meta = dict(prior_dict.get("metadata", {}))
    new_meta: Dict[str, Any] = copy.deepcopy(raw_meta)
    new_logs: List[str] = list(prior_dict.get("logs", []))

    # 3. Sanitize existing history before carrying it into the new state
    raw_history = raw_meta.get("history", [])
    sanitized_history: List[Dict[str, Any]] = []
    if isinstance(raw_history, list):
        for item in raw_history:
            cleaned_item = _sanitize_history_entry(item)
            if cleaned_item is not None:
                sanitized_history.append(cleaned_item)

    # 4. Archive previous run outcomes into a compact allowlisted entry if present
    prior_exec = prior_dict.get("execution_result")
    prior_report = prior_dict.get("assertion_report")
    prior_error = prior_dict.get("error")
    prior_status = prior_dict.get("status")

    if prior_exec is not None or prior_report is not None or prior_error is not None:
        new_entry = _extract_compact_history_entry(
            status=prior_status,
            execution_result=prior_exec,
            assertion_report=prior_report,
            error=prior_error,
            selected_operation=prior_dict.get("selected_operation"),
            target_path=prior_dict.get("target_path"),
            target_method=prior_dict.get("target_method"),
            spec=prior_dict.get("spec"),
        )
        if new_entry is not None:
            sanitized_history.append(new_entry)

    # 5. Cap history at the 5 most recent entries, preserving chronological order
    if len(sanitized_history) > MAX_HISTORY_ENTRIES:
        sanitized_history = sanitized_history[-MAX_HISTORY_ENTRIES:]

    new_meta["history"] = sanitized_history
    base["metadata"] = new_meta
    base["logs"] = new_logs

    # 6. Retain reusable configuration fields
    REUSABLE_CONFIG_KEYS = (
        "spec",
        "raw_spec",
        "selected_operation",
        "target_path",
        "target_method",
        "target_operation_id",
        "request_config",
    )
    for k in REUSABLE_CONFIG_KEYS:
        if k in prior_dict:
            base[k] = prior_dict[k]

    # 7. Apply safe caller-supplied configuration overrides
    for k, v in overrides.items():
        if k not in TRANSIENT_STATE_KEYS:
            base[k] = v

    # 8. Unconditionally enforce fresh transient state
    base["status"] = "idle"
    base["error"] = None
    base["execution_result"] = None
    base["assertion_report"] = None

    return base


async def run_agent_workflow(
    prior_state: Optional[Union[AgentState, Dict[str, Any]]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """
    Public asynchronous execution wrapper enforcing fresh transient state (Phase 4A contract).

    Workflow Invocation Contract:
    1. Invokes create_fresh_run_state() to safely cleanse transient execution fields
       ('status'='idle', 'error'=None, 'execution_result'=None, 'assertion_report'=None)
       and archive previous execution outcomes in metadata['history'] using a compact,
       sanitized, allowlisted representation capped at 5 entries.
    2. Rejects attempts to override transient execution fields with ValueError.
    3. Executes the compiled LangGraph workflow via agent_graph.ainvoke().
    4. Returns the completed final workflow state.

    Important Contract Note:
    Direct invocations of `agent_graph.ainvoke(state)` do NOT automatically sanitize or reset
    transient state. Future production callers (such as the Phase 4C retest loop and Phase 5 REST
    API endpoints) MUST invoke the workflow through this `run_agent_workflow()` wrapper rather
    than calling `agent_graph.ainvoke()` directly with reused state dictionaries.
    """
    from backend.app.agent.graph import agent_graph

    fresh_state = create_fresh_run_state(prior_state=prior_state, **overrides)
    final_state = await agent_graph.ainvoke(fresh_state)
    return final_state


# Attach helpers to FailureClassifier for convenient access
FailureClassifier.create_fresh_run_state = staticmethod(create_fresh_run_state)
FailureClassifier.run_agent_workflow = staticmethod(run_agent_workflow)
FailureClassifier.MAX_STR_LEN = MAX_STR_LEN
FailureClassifier.MAX_PATH_LEN = MAX_PATH_LEN
FailureClassifier.MAX_HISTORY_ENTRIES = MAX_HISTORY_ENTRIES
FailureClassifier._safe_get = staticmethod(_safe_get)
FailureClassifier._clean_string = staticmethod(_clean_string)
FailureClassifier._clean_path_template = staticmethod(_clean_path_template)
FailureClassifier._match_spec_endpoint_path = staticmethod(_match_spec_endpoint_path)
FailureClassifier._extract_compact_history_entry = staticmethod(_extract_compact_history_entry)
FailureClassifier._sanitize_history_entry = staticmethod(_sanitize_history_entry)

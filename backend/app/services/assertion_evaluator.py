"""
Deterministic Assertion Evaluator Service (Phase 2C).
Evaluates HTTP execution results against parsed OpenAPI 3.0.x and 3.1.x operations.
Zero LLM dependencies, deterministic status matching, content-type checks,
and full JSON Schema constraint validation.
"""

from typing import Any, Dict, List, Optional, Tuple, Union
import warnings
from pydantic import BaseModel, ConfigDict, Field
import jsonschema
from jsonschema.validators import extend

from backend.app.schemas.execution import ExecutionResult
from backend.app.schemas.spec import (
    EndpointDefinition,
    ParsedAPISpec,
    ResponseDefinition,
)


class AssertionResult(BaseModel):
    """Result of a single deterministic assertion check."""
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(
        ...,
        description="Assertion identifier (e.g. 'status_code_match', 'content_type_match', 'schema_required')",
    )
    passed: bool = Field(..., description="Whether the assertion succeeded")
    expected: Optional[Any] = Field(default=None, description="Expected value, pattern, or schema descriptor")
    actual: Optional[Any] = Field(default=None, description="Actual observed value in response")
    message: str = Field(..., description="Human-readable explanation of result or failure cause")
    json_path: Optional[str] = Field(
        default=None, description="RFC 6901 JSON Pointer path of error if applicable (e.g. '/items/0/price')"
    )


class AssertionReport(BaseModel):
    """Aggregated evaluation report for an HTTP execution against an OpenAPI operation."""
    model_config = ConfigDict(populate_by_name=True)

    endpoint_path: str = Field(..., description="API endpoint path template (e.g. '/api/products/{id}')")
    method: str = Field(..., description="HTTP method executed (e.g. 'GET', 'POST')")
    status_code: int = Field(..., description="Observed HTTP status code")
    matched_status_key: Optional[str] = Field(
        default=None, description="Matched response definition key (e.g. '200', '2XX', 'default')"
    )
    is_success: bool = Field(..., description="True if ALL individual assertions passed")
    assertions: List[AssertionResult] = Field(
        default_factory=list, description="List of individual assertion results"
    )

    @property
    def passed_assertions(self) -> List[AssertionResult]:
        return [a for a in self.assertions if a.passed]

    @property
    def failed_assertions(self) -> List[AssertionResult]:
        return [a for a in self.assertions if not a.passed]


# ---------------------------------------------------------------------------
# Custom OAS 3.0 / 3.1 Compatible JSON Schema Validator
# Extends Draft202012Validator to support OAS 3.0 `nullable: true` semantics
# while natively supporting OAS 3.1 type unions (e.g. type: ['string', 'null']).
# ---------------------------------------------------------------------------

def _check_type_with_nullable(validator, types, instance, schema):
    if instance is None and schema.get("nullable") is True:
        return
    yield from jsonschema.Draft202012Validator.VALIDATORS["type"](validator, types, instance, schema)


def _check_enum_with_nullable(validator, enums, instance, schema):
    if instance is None and schema.get("nullable") is True:
        return
    yield from jsonschema.Draft202012Validator.VALIDATORS["enum"](validator, enums, instance, schema)


def _check_one_of_with_nullable(validator, oneOf, instance, schema):
    if instance is None and schema.get("nullable") is True:
        return
    yield from jsonschema.Draft202012Validator.VALIDATORS["oneOf"](validator, oneOf, instance, schema)


def _check_any_of_with_nullable(validator, anyOf, instance, schema):
    if instance is None and schema.get("nullable") is True:
        return
    yield from jsonschema.Draft202012Validator.VALIDATORS["anyOf"](validator, anyOf, instance, schema)


def _check_all_of_with_nullable(validator, allOf, instance, schema):
    if instance is None and schema.get("nullable") is True:
        return
    yield from jsonschema.Draft202012Validator.VALIDATORS["allOf"](validator, allOf, instance, schema)


OASValidator = extend(
    jsonschema.Draft202012Validator,
    validators={
        "type": _check_type_with_nullable,
        "enum": _check_enum_with_nullable,
        "oneOf": _check_one_of_with_nullable,
        "anyOf": _check_any_of_with_nullable,
        "allOf": _check_all_of_with_nullable,
    },
)


class DeterministicAssertionEvaluator:
    """
    Deterministic response assertion evaluator.
    Validates ExecutionResults against OpenAPI 3.0 / 3.1 EndpointDefinitions without LLMs.
    """

    def __init__(
        self,
        raw_spec: Optional[Dict[str, Any]] = None,
        openapi_version: str = "3.1.0",
    ):
        self.raw_spec = raw_spec or {}
        self.openapi_version = openapi_version
        self._resolver: Optional[jsonschema.RefResolver] = None

        if self.raw_spec:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=DeprecationWarning)
                try:
                    self._resolver = jsonschema.RefResolver.from_schema(self.raw_spec)
                except Exception:
                    self._resolver = None

    def evaluate(
        self,
        endpoint: EndpointDefinition,
        execution_result: ExecutionResult,
    ) -> AssertionReport:
        """
        Evaluates an HTTP execution result against an OpenAPI endpoint definition.
        
        Args:
            endpoint: Normalized EndpointDefinition from Phase 1B parser.
            execution_result: ExecutionResult envelope from Phase 2B executor.
            
        Returns:
            AssertionReport containing pass/fail status and structured assertions.
        """
        assertions: List[AssertionResult] = []

        # 1. Handle transport-level execution failures (SSRF, timeouts, network errors)
        if execution_result.is_ssrf_blocked:
            assertions.append(
                AssertionResult(
                    name="execution_transport",
                    passed=False,
                    expected="Safe HTTP connection",
                    actual="SSRF Blocked",
                    message=f"Request blocked by SSRF Security Guard: {execution_result.error_message}",
                )
            )
            return AssertionReport(
                endpoint_path=endpoint.path,
                method=endpoint.method,
                status_code=execution_result.status_code,
                matched_status_key=None,
                is_success=False,
                assertions=assertions,
            )

        if execution_result.is_timeout:
            assertions.append(
                AssertionResult(
                    name="execution_transport",
                    passed=False,
                    expected="Response within deadline",
                    actual="Timed out",
                    message=f"Request execution timed out: {execution_result.error_message}",
                )
            )
            return AssertionReport(
                endpoint_path=endpoint.path,
                method=endpoint.method,
                status_code=execution_result.status_code,
                matched_status_key=None,
                is_success=False,
                assertions=assertions,
            )

        if execution_result.is_network_error:
            assertions.append(
                AssertionResult(
                    name="execution_transport",
                    passed=False,
                    expected="Completed HTTP exchange",
                    actual="Network Error",
                    message=f"Network error during execution: {execution_result.error_message}",
                )
            )
            return AssertionReport(
                endpoint_path=endpoint.path,
                method=endpoint.method,
                status_code=execution_result.status_code,
                matched_status_key=None,
                is_success=False,
                assertions=assertions,
            )

        # 2. Status Code Matching (Exact -> Range 2XX/4XX/5XX -> Default)
        matched_key, response_def, status_assertion = self._match_status_code(
            execution_result.status_code, endpoint.responses
        )
        assertions.append(status_assertion)

        if not response_def:
            # Status code unexpected and undeclared; cannot proceed to content/body validation
            return AssertionReport(
                endpoint_path=endpoint.path,
                method=endpoint.method,
                status_code=execution_result.status_code,
                matched_status_key=None,
                is_success=False,
                assertions=assertions,
            )

        # 3. Content-Type Validation
        matched_media_type, ct_assertion = self._match_content_type(
            execution_result, response_def
        )
        assertions.append(ct_assertion)

        # 4. Response Body & JSON Schema Validation
        schema = None
        if matched_media_type and response_def.content:
            schema = response_def.get_schema(matched_media_type)
            if schema is None:
                # If specific get_schema didn't find it, check content dictionary directly
                content_obj = response_def.content.get(matched_media_type)
                if isinstance(content_obj, dict):
                    schema = content_obj.get("schema")

        body_assertions = self._validate_body_schema(
            body=execution_result.body,
            raw_body_bytes_len=execution_result.raw_body_bytes_len,
            schema=schema,
            matched_media_type=matched_media_type,
            response_def=response_def,
        )
        assertions.extend(body_assertions)

        is_all_passed = all(a.passed for a in assertions)

        return AssertionReport(
            endpoint_path=endpoint.path,
            method=endpoint.method,
            status_code=execution_result.status_code,
            matched_status_key=matched_key,
            is_success=is_all_passed,
            assertions=assertions,
        )

    def evaluate_against_spec(
        self,
        spec: ParsedAPISpec,
        execution_result: ExecutionResult,
        endpoint_path: Optional[str] = None,
        method: Optional[str] = None,
    ) -> AssertionReport:
        """
        Convenience evaluator: looks up the EndpointDefinition from a ParsedAPISpec
        and evaluates the execution result against it.
        """
        target_path = endpoint_path or execution_result.url
        target_method = (method or execution_result.method).upper()

        matching_endpoint: Optional[EndpointDefinition] = None
        for ep in spec.endpoints:
            if ep.method.upper() == target_method:
                # Exact path match or pattern match
                if ep.path == target_path or target_path.endswith(ep.path):
                    matching_endpoint = ep
                    break

        if not matching_endpoint:
            # Fallback: check path ignoring query string
            path_only = target_path.split("?")[0]
            for ep in spec.endpoints:
                if ep.method.upper() == target_method:
                    if ep.path == path_only or path_only.endswith(ep.path):
                        matching_endpoint = ep
                        break

        if not matching_endpoint:
            return AssertionReport(
                endpoint_path=target_path,
                method=target_method,
                status_code=execution_result.status_code,
                matched_status_key=None,
                is_success=False,
                assertions=[
                    AssertionResult(
                        name="endpoint_lookup",
                        passed=False,
                        expected=f"{target_method} {target_path}",
                        actual="Not found in specification",
                        message=f"No matching endpoint found for {target_method} {target_path} in parsed API specification.",
                    )
                ],
            )

        return self.evaluate(matching_endpoint, execution_result)

    def _match_status_code(
        self,
        status_code: int,
        responses: Dict[str, ResponseDefinition],
    ) -> Tuple[Optional[str], Optional[ResponseDefinition], AssertionResult]:
        """
        Matches status code against declared responses in order:
        1. Exact status code match (e.g. '200', '404')
        2. Status code class range match (e.g. '2XX', '4XX', '5XX', case-insensitive)
        3. 'default' response definition
        """
        status_str = str(status_code)
        range_upper = f"{status_code // 100}XX"
        range_lower = f"{status_code // 100}xx"

        # 1. Exact match
        if status_str in responses:
            return (
                status_str,
                responses[status_str],
                AssertionResult(
                    name="status_code_match",
                    passed=True,
                    expected=status_str,
                    actual=status_code,
                    message=f"HTTP status code {status_code} matched exact declared response '{status_str}'.",
                ),
            )

        # 2. Range match (e.g. 2XX, 4XX)
        for r_key in (range_upper, range_lower):
            if r_key in responses:
                return (
                    r_key,
                    responses[r_key],
                    AssertionResult(
                        name="status_code_match",
                        passed=True,
                        expected=r_key,
                        actual=status_code,
                        message=f"HTTP status code {status_code} matched declared range '{r_key}'.",
                    ),
                )

        # 3. Default fallback
        if "default" in responses:
            return (
                "default",
                responses["default"],
                AssertionResult(
                    name="status_code_match",
                    passed=True,
                    expected="default",
                    actual=status_code,
                    message=f"HTTP status code {status_code} matched 'default' response definition.",
                ),
            )

        # No match found
        declared = list(responses.keys())
        return (
            None,
            None,
            AssertionResult(
                name="status_code_match",
                passed=False,
                expected=declared,
                actual=status_code,
                message=f"HTTP status code {status_code} was unexpected and did not match any declared response: {declared}.",
            ),
        )

    def _match_content_type(
        self,
        execution_result: ExecutionResult,
        response_def: ResponseDefinition,
    ) -> Tuple[Optional[str], AssertionResult]:
        """
        Validates response Content-Type against declared content media types.
        Supports exact matches, wildcards (*/*, application/*), and structured syntax (+json).
        """
        # Extract MIME type without parameters (e.g. 'application/json' from 'application/json; charset=utf-8')
        actual_mime: Optional[str] = None
        for k, v in execution_result.headers.items():
            if k.lower() == "content-type":
                actual_mime = v.split(";")[0].strip().lower()
                break

        # Case A: Operation defines no content (e.g. 204 No Content or headers-only)
        if not response_def.content:
            if not actual_mime or execution_result.raw_body_bytes_len == 0:
                return (
                    None,
                    AssertionResult(
                        name="content_type_match",
                        passed=True,
                        expected=None,
                        actual=actual_mime,
                        message="No response content-type declared or required for this status code.",
                    ),
                )
            else:
                return (
                    None,
                    AssertionResult(
                        name="content_type_match",
                        passed=False,
                        expected=[],
                        actual=actual_mime,
                        message=f"Response returned Content-Type '{actual_mime}' but no content types are declared.",
                    ),
                )

        # Case B: Operation defines one or more media types
        declared_types = list(response_def.content.keys())

        if not actual_mime:
            # If body is completely empty and status code is 204/304, allow empty
            if execution_result.raw_body_bytes_len == 0 and execution_result.status_code in (204, 304):
                return (
                    None,
                    AssertionResult(
                        name="content_type_match",
                        passed=True,
                        expected=declared_types,
                        actual=None,
                        message="Empty body on 204/304 permitted without Content-Type.",
                    ),
                )
            return (
                None,
                AssertionResult(
                    name="content_type_match",
                    passed=False,
                    expected=declared_types,
                    actual=None,
                    message=f"Response is missing Content-Type header. Expected one of: {declared_types}.",
                ),
            )

        # Check for matching declared media type
        matched_key: Optional[str] = None
        for dt in declared_types:
            dt_lower = dt.lower()
            if actual_mime == dt_lower:
                matched_key = dt
                break
            if dt_lower == "*/*":
                matched_key = dt
                break
            if dt_lower.endswith("/*"):
                main_type = dt_lower.split("/")[0]
                if actual_mime.startswith(f"{main_type}/"):
                    matched_key = dt
                    break
            # Structured syntax match e.g. application/problem+json matches application/json
            if actual_mime.endswith("+json") and dt_lower == "application/json":
                matched_key = dt
                break

        if matched_key:
            return (
                matched_key,
                AssertionResult(
                    name="content_type_match",
                    passed=True,
                    expected=declared_types,
                    actual=actual_mime,
                    message=f"Response Content-Type '{actual_mime}' matches declared media type '{matched_key}'.",
                ),
            )
        else:
            return (
                None,
                AssertionResult(
                    name="content_type_match",
                    passed=False,
                    expected=declared_types,
                    actual=actual_mime,
                    message=f"Response Content-Type '{actual_mime}' does not match any declared media type in {declared_types}.",
                ),
            )

    def _validate_body_schema(
        self,
        body: Any,
        raw_body_bytes_len: int,
        schema: Optional[Dict[str, Any]],
        matched_media_type: Optional[str],
        response_def: ResponseDefinition,
    ) -> List[AssertionResult]:
        """
        Validates response body against the extracted JSON Schema.
        Supports all standard constraints and composition keywords.
        """
        # If response definition has no declared content, verify body is empty
        if not response_def.content:
            if raw_body_bytes_len > 0 and body is not None:
                return [
                    AssertionResult(
                        name="body_schema_validation",
                        passed=False,
                        expected=None,
                        actual=f"{raw_body_bytes_len} bytes",
                        message="Response returned a non-empty body, but no response content was declared in specification.",
                        json_path="/",
                    )
                ]
            return [
                AssertionResult(
                    name="body_schema_validation",
                    passed=True,
                    expected=None,
                    actual=None,
                    message="Empty response body matches empty response definition.",
                )
            ]

        # Content was declared, but media-type object defines no schema constraint
        if schema is None or schema == {}:
            return [
                AssertionResult(
                    name="body_schema_validation",
                    passed=True,
                    expected=None,
                    actual=type(body).__name__ if body is not None else None,
                    message="Response media type declared without schema constraints.",
                )
            ]

        # Content was declared with a schema, check if body is missing/empty
        if body is None:
            # Check if schema allows null / empty
            is_nullable = (
                schema.get("type") == "null"
                or schema.get("nullable") is True
                or (isinstance(schema.get("type"), list) and "null" in schema.get("type"))
            )
            if is_nullable:
                return [
                    AssertionResult(
                        name="body_schema_validation",
                        passed=True,
                        expected="null/nullable",
                        actual=None,
                        message="Empty/null body matches nullable schema.",
                        json_path="/",
                    )
                ]
            return [
                AssertionResult(
                    name="body_schema_validation",
                    passed=False,
                    expected=schema.get("type", "non-empty"),
                    actual=None,
                    message="Response body was empty, but a non-empty payload is required by schema.",
                    json_path="/",
                )
            ]

        # Execute JSON Schema validation
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            try:
                validator = OASValidator(schema, resolver=self._resolver)
                errors = sorted(validator.iter_errors(body), key=lambda e: (list(e.path), e.message))
            except Exception as e:
                return [
                    AssertionResult(
                        name="body_schema_validation",
                        passed=False,
                        expected="Resolvable schema reference",
                        actual=None,
                        message=f"Schema reference resolution failed: {e}",
                        json_path="/",
                    )
                ]

        if not errors:
            return [
                AssertionResult(
                    name="body_schema_validation",
                    passed=True,
                    expected=schema.get("type", "valid schema"),
                    actual="Valid payload",
                    message="Response body conforms to JSON Schema.",
                )
            ]

        results: List[AssertionResult] = []
        for err in errors:
            path = "/" + "/".join(str(p) for p in err.path) if err.path else "/"
            val_name = f"schema_{err.validator}" if err.validator else "body_schema_validation"
            results.append(
                AssertionResult(
                    name=val_name,
                    passed=False,
                    expected=err.validator_value,
                    actual=err.instance,
                    message=err.message,
                    json_path=path,
                )
            )

        return results

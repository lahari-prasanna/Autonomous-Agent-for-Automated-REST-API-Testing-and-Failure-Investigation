"""
Unit and integration test suite for Deterministic Assertion Evaluator (Phase 2C).
Tests status code matching (exact, ranges, default), content-type validation,
JSON Schema constraint validation (types, required, constraints, composition),
OAS 3.0 nullable vs OAS 3.1 null unions, empty responses, unresolved references,
and integration with Phase 1A Mock API / Phase 1B OpenAPI Parser.
"""

from typing import Any, Dict
import pytest

from backend.app.schemas.execution import ExecutionResult
from backend.app.schemas.spec import (
    EndpointDefinition,
    ParsedAPISpec,
    ResponseDefinition,
)
from backend.app.services.assertion_evaluator import (
    AssertionReport,
    AssertionResult,
    DeterministicAssertionEvaluator,
)
from backend.app.services.spec_parser import OpenAPIParser


# ---------------------------------------------------------------------------
# Helper Fixtures & Builders
# ---------------------------------------------------------------------------

def make_endpoint(
    path: str = "/api/test",
    method: str = "GET",
    responses: Dict[str, ResponseDefinition] = None,
) -> EndpointDefinition:
    """Creates a test EndpointDefinition with specified responses."""
    return EndpointDefinition(
        path=path,
        method=method,
        responses=responses or {},
    )


def make_execution_result(
    status_code: int = 200,
    headers: Dict[str, str] = None,
    body: Any = None,
    method: str = "GET",
    url: str = "http://127.0.0.1:8081/api/test",
    is_success: bool = True,
    is_timeout: bool = False,
    is_network_error: bool = False,
    is_ssrf_blocked: bool = False,
    error_message: str = None,
) -> ExecutionResult:
    """Creates a mock ExecutionResult for assertion testing."""
    h = headers if headers is not None else {"content-type": "application/json"}
    raw_len = len(str(body).encode("utf-8")) if body is not None else 0
    return ExecutionResult(
        method=method,
        url=url,
        status_code=status_code,
        headers=h,
        body=body,
        raw_body_bytes_len=raw_len,
        latency_ms=15.5,
        is_success=is_success,
        is_timeout=is_timeout,
        is_network_error=is_network_error,
        is_ssrf_blocked=is_ssrf_blocked,
        error_message=error_message,
    )


# ---------------------------------------------------------------------------
# 1. Status Code Matching Tests (Exact, Ranges 2XX/4XX/5XX, Default, Unexpected)
# ---------------------------------------------------------------------------

def test_status_code_exact_match():
    """Verify exact status code match succeeds."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(status_code="200", description="OK"),
            "404": ResponseDefinition(status_code="404", description="Not Found"),
        }
    )
    result = make_execution_result(status_code=200)

    report = evaluator.evaluate(endpoint, result)

    assert report.is_success is True
    assert report.matched_status_key == "200"
    status_assertion = report.assertions[0]
    assert status_assertion.name == "status_code_match"
    assert status_assertion.passed is True
    assert status_assertion.actual == 200


def test_status_code_range_match_2xx():
    """Verify status range 2XX matches 201 Created and 204 No Content."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "2XX": ResponseDefinition(status_code="2XX", description="All 2xx Success"),
        }
    )

    # 201 Created
    report_201 = evaluator.evaluate(endpoint, make_execution_result(status_code=201))
    assert report_201.is_success is True
    assert report_201.matched_status_key == "2XX"
    assert report_201.assertions[0].passed is True

    # 204 No Content
    report_204 = evaluator.evaluate(endpoint, make_execution_result(status_code=204, headers={}, body=None))
    assert report_204.is_success is True
    assert report_204.matched_status_key == "2XX"


def test_status_code_range_match_4xx_and_5xx():
    """Verify range matching for 4XX client error and 5XX server error."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(status_code="200", description="OK"),
            "4XX": ResponseDefinition(status_code="4XX", description="Client Error"),
            "5XX": ResponseDefinition(status_code="5XX", description="Server Error"),
        }
    )

    # 400 Bad Request
    rep_400 = evaluator.evaluate(endpoint, make_execution_result(status_code=400))
    assert rep_400.matched_status_key == "4XX"
    assert rep_400.assertions[0].passed is True

    # 503 Service Unavailable
    rep_503 = evaluator.evaluate(endpoint, make_execution_result(status_code=503))
    assert rep_503.matched_status_key == "5XX"
    assert rep_503.assertions[0].passed is True


def test_status_code_default_fallback():
    """Verify 'default' response definition matches when neither exact nor range matches."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(status_code="200", description="OK"),
            "default": ResponseDefinition(status_code="default", description="Unexpected Error"),
        }
    )
    # HTTP 418 I'm a teapot
    result = make_execution_result(status_code=418)

    report = evaluator.evaluate(endpoint, result)

    assert report.is_success is True
    assert report.matched_status_key == "default"
    assert report.assertions[0].passed is True
    assert "default" in report.assertions[0].message


def test_status_code_unexpected_failure():
    """Verify undeclared status code fails evaluation and halts further body validation."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(status_code="200", description="OK"),
            "404": ResponseDefinition(status_code="404", description="Not Found"),
        }
    )
    # Server returns unexpected 500
    result = make_execution_result(status_code=500, body={"error": "fatal"})

    report = evaluator.evaluate(endpoint, result)

    assert report.is_success is False
    assert report.matched_status_key is None
    assert len(report.assertions) == 1
    status_ast = report.assertions[0]
    assert status_ast.name == "status_code_match"
    assert status_ast.passed is False
    assert status_ast.actual == 500
    assert "unexpected" in status_ast.message.lower()


# ---------------------------------------------------------------------------
# 2. Content-Type Validation Tests
# ---------------------------------------------------------------------------

def test_content_type_exact_and_parameters():
    """Verify Content-Type matches exact declared media type and ignores parameters like charset."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={"application/json": {"schema": {"type": "object"}}},
            )
        }
    )
    # Server sends Content-Type with charset parameter
    res = make_execution_result(
        status_code=200,
        headers={"content-type": "application/json; charset=utf-8"},
        body={"ok": True},
    )

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is True
    ct_ast = [a for a in report.assertions if a.name == "content_type_match"][0]
    assert ct_ast.passed is True
    assert ct_ast.actual == "application/json"


def test_content_type_wildcard_matching():
    """Verify wildcard declared types (*/* and application/*) match incoming MIME types."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={"application/*": {"schema": {}}},
            )
        }
    )

    # application/xml matches application/*
    res1 = make_execution_result(
        status_code=200,
        headers={"content-type": "application/xml"},
        body="<xml></xml>",
    )
    report1 = evaluator.evaluate(endpoint, res1)
    assert report1.is_success is True

    # image/png fails application/*
    res2 = make_execution_result(
        status_code=200,
        headers={"content-type": "image/png"},
        body=b"png-data",
    )
    report2 = evaluator.evaluate(endpoint, res2)
    assert report2.is_success is False
    ct_ast = [a for a in report2.assertions if a.name == "content_type_match"][0]
    assert ct_ast.passed is False


def test_content_type_structured_syntax_plus_json():
    """Verify structured syntax suffix (e.g. application/problem+json) matches application/json."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "400": ResponseDefinition(
                status_code="400",
                description="Bad Request",
                content={"application/json": {"schema": {"type": "object"}}},
            )
        }
    )
    res = make_execution_result(
        status_code=400,
        headers={"content-type": "application/problem+json"},
        body={"type": "about:blank", "title": "Invalid Param"},
    )

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is True
    ct_ast = [a for a in report.assertions if a.name == "content_type_match"][0]
    assert ct_ast.passed is True


def test_content_type_mismatch():
    """Verify Content-Type mismatch fails evaluation."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={"application/json": {"schema": {"type": "object"}}},
            )
        }
    )
    res = make_execution_result(
        status_code=200,
        headers={"content-type": "text/html"},
        body="<html>Error</html>",
    )

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is False
    ct_ast = [a for a in report.assertions if a.name == "content_type_match"][0]
    assert ct_ast.passed is False
    assert "text/html" in str(ct_ast.actual)


def test_content_type_missing_header():
    """Verify missing Content-Type header fails when content schema is declared."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={"application/json": {"schema": {"type": "object"}}},
            )
        }
    )
    res = make_execution_result(
        status_code=200,
        headers={},  # Missing content-type header
        body={"data": 123},
    )

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is False
    ct_ast = [a for a in report.assertions if a.name == "content_type_match"][0]
    assert ct_ast.passed is False
    assert "missing content-type header" in ct_ast.message.lower()


# ---------------------------------------------------------------------------
# 3. JSON Schema Constraint Validation Tests (Required, Types, Numeric, String, Array, Enum)
# ---------------------------------------------------------------------------

def test_schema_required_properties_and_json_pointer():
    """Verify missing required property triggers failure with root JSON Pointer '/'."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["id", "name", "price"],
                            "properties": {
                                "id": {"type": "integer"},
                                "name": {"type": "string"},
                                "price": {"type": "number"},
                            },
                        }
                    }
                },
            )
        }
    )
    # Body missing 'price'
    res = make_execution_result(
        status_code=200,
        body={"id": 101, "name": "Mechanical Keyboard"},
    )

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is False
    failed = report.failed_assertions
    assert len(failed) == 1
    assert failed[0].name == "schema_required"
    assert failed[0].passed is False
    assert "price" in failed[0].message
    assert failed[0].json_path == "/"


def test_schema_nested_property_type_mismatch_and_pointer():
    """Verify nested property type mismatch returns accurate JSON Pointer path."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "order": {
                                    "type": "object",
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "price": {"type": "number"}
                                                },
                                            },
                                        }
                                    },
                                }
                            },
                        }
                    }
                },
            )
        }
    )
    # Nested item price is a string instead of number
    res = make_execution_result(
        status_code=200,
        body={"order": {"items": [{"price": "not-a-number"}]}},
    )

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is False
    failed = report.failed_assertions
    assert len(failed) == 1
    assert failed[0].name == "schema_type"
    assert failed[0].json_path == "/order/items/0/price"
    assert "not-a-number" in failed[0].message or "number" in failed[0].message


def test_schema_numeric_constraints_min_max():
    """Verify minimum and maximum numeric boundary constraints."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "price": {"type": "number", "minimum": 1.0, "maximum": 100.0}
                            },
                        }
                    }
                },
            )
        }
    )

    # Valid within bounds
    res_valid = make_execution_result(status_code=200, body={"price": 50.0})
    assert evaluator.evaluate(endpoint, res_valid).is_success is True

    # Less than minimum
    res_under = make_execution_result(status_code=200, body={"price": 0.5})
    rep_under = evaluator.evaluate(endpoint, res_under)
    assert rep_under.is_success is False
    assert rep_under.failed_assertions[0].name == "schema_minimum"
    assert rep_under.failed_assertions[0].json_path == "/price"

    # Greater than maximum
    res_over = make_execution_result(status_code=200, body={"price": 105.0})
    rep_over = evaluator.evaluate(endpoint, res_over)
    assert rep_over.is_success is False
    assert rep_over.failed_assertions[0].name == "schema_maximum"
    assert rep_over.failed_assertions[0].json_path == "/price"


def test_schema_string_constraints_min_max_length():
    """Verify minLength and maxLength string constraints."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "sku": {"type": "string", "minLength": 3, "maxLength": 6}
                            },
                        }
                    }
                },
            )
        }
    )

    # Too short
    rep_short = evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"sku": "AB"}))
    assert rep_short.is_success is False
    assert rep_short.failed_assertions[0].name == "schema_minLength"
    assert rep_short.failed_assertions[0].json_path == "/sku"

    # Too long
    rep_long = evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"sku": "TOOLONG123"}))
    assert rep_long.is_success is False
    assert rep_long.failed_assertions[0].name == "schema_maxLength"
    assert rep_long.failed_assertions[0].json_path == "/sku"


def test_schema_array_constraints_min_max_items():
    """Verify minItems and maxItems array constraints."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "tags": {"type": "array", "minItems": 1, "maxItems": 2}
                            },
                        }
                    }
                },
            )
        }
    )

    # Empty array violates minItems
    rep_empty = evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"tags": []}))
    assert rep_empty.is_success is False
    assert rep_empty.failed_assertions[0].name == "schema_minItems"
    assert rep_empty.failed_assertions[0].json_path == "/tags"

    # 3 items violates maxItems: 2
    rep_over = evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"tags": ["a", "b", "c"]}))
    assert rep_over.is_success is False
    assert rep_over.failed_assertions[0].name == "schema_maxItems"
    assert rep_over.failed_assertions[0].json_path == "/tags"


def test_schema_enum_constraint():
    """Verify enum constraint enforcement and error reporting."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string", "enum": ["ADMIN", "USER", "GUEST"]}
                            },
                        }
                    }
                },
            )
        }
    )

    # Valid enum value
    res_valid = make_execution_result(status_code=200, body={"role": "ADMIN"})
    assert evaluator.evaluate(endpoint, res_valid).is_success is True

    # Invalid enum value
    res_bad = make_execution_result(status_code=200, body={"role": "SUPERUSER"})
    report = evaluator.evaluate(endpoint, res_bad)
    assert report.is_success is False
    assert report.failed_assertions[0].name == "schema_enum"
    assert report.failed_assertions[0].json_path == "/role"
    assert "'SUPERUSER' is not one of ['ADMIN', 'USER', 'GUEST']" in report.failed_assertions[0].message


# ---------------------------------------------------------------------------
# 4. Schema Composition Tests (oneOf, anyOf, allOf)
# ---------------------------------------------------------------------------

def test_schema_one_of_composition():
    """Verify oneOf composition succeeds when matching exactly one schema, fails otherwise."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "integer"},
                            ]
                        }
                    }
                },
            )
        }
    )

    # Matching string -> PASS
    assert evaluator.evaluate(endpoint, make_execution_result(status_code=200, body="hello")).is_success is True

    # Matching integer -> PASS
    assert evaluator.evaluate(endpoint, make_execution_result(status_code=200, body=42)).is_success is True

    # Matching boolean -> FAIL
    rep_bool = evaluator.evaluate(endpoint, make_execution_result(status_code=200, body=True))
    assert rep_bool.is_success is False
    assert rep_bool.failed_assertions[0].name == "schema_oneOf"


def test_schema_any_of_composition():
    """Verify anyOf composition succeeds when matching at least one schema."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "anyOf": [
                                {"required": ["email"]},
                                {"required": ["phone"]},
                            ],
                            "properties": {
                                "email": {"type": "string"},
                                "phone": {"type": "string"},
                            },
                        }
                    }
                },
            )
        }
    )

    # Email only -> PASS
    assert evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"email": "a@b.com"})).is_success is True

    # Phone only -> PASS
    assert evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"phone": "123456"})).is_success is True

    # Neither -> FAIL
    rep_none = evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"name": "Alice"}))
    assert rep_none.is_success is False
    assert rep_none.failed_assertions[0].name == "schema_anyOf"


def test_schema_all_of_composition():
    """Verify allOf composition requires satisfying all merged constraints."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "allOf": [
                                {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
                                {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
                            ]
                        }
                    }
                },
            )
        }
    )

    # Both id and name -> PASS
    assert evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"id": 1, "name": "Test"})).is_success is True

    # Missing name -> FAIL
    rep_missing = evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"id": 1}))
    assert rep_missing.is_success is False
    assert rep_missing.failed_assertions[0].name == "schema_required"


# ---------------------------------------------------------------------------
# 5. Nullable Semantics Tests (OAS 3.0 nullable: true vs OAS 3.1 null unions)
# ---------------------------------------------------------------------------

def test_schema_oas30_nullable_true_semantics():
    """Verify OAS 3.0 `nullable: true` accepts null values on primitive and object fields."""
    evaluator = DeterministicAssertionEvaluator(openapi_version="3.0.3")
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["id", "description", "category"],
                            "properties": {
                                "id": {"type": "integer"},
                                "description": {"type": "string", "nullable": True},
                                "category": {
                                    "type": "object",
                                    "nullable": True,
                                    "properties": {"name": {"type": "string"}},
                                },
                            },
                        }
                    }
                },
            )
        }
    )

    # Null values provided on nullable fields -> PASS
    res = make_execution_result(
        status_code=200,
        body={"id": 1, "description": None, "category": None},
    )
    report = evaluator.evaluate(endpoint, res)
    assert report.is_success is True


def test_schema_oas30_non_nullable_rejection():
    """Verify non-nullable fields reject null values in OAS 3.0."""
    evaluator = DeterministicAssertionEvaluator(openapi_version="3.0.3")
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"}  # nullable is False/absent
                            },
                        }
                    }
                },
            )
        }
    )

    res = make_execution_result(status_code=200, body={"name": None})
    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is False
    assert report.failed_assertions[0].name == "schema_type"
    assert report.failed_assertions[0].json_path == "/name"
    assert "None is not of type 'string'" in report.failed_assertions[0].message


def test_schema_oas31_type_null_union_semantics():
    """Verify OAS 3.1 JSON Schema type unions (e.g. type: ['string', 'null']) are supported."""
    evaluator = DeterministicAssertionEvaluator(openapi_version="3.1.0")
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "nickname": {"type": ["string", "null"]}
                            },
                        }
                    }
                },
            )
        }
    )

    # String value -> PASS
    assert evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"nickname": "Bobby"})).is_success is True

    # Null value -> PASS
    assert evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"nickname": None})).is_success is True

    # Integer value -> FAIL
    rep_int = evaluator.evaluate(endpoint, make_execution_result(status_code=200, body={"nickname": 123}))
    assert rep_int.is_success is False
    assert rep_int.failed_assertions[0].name == "schema_type"


# ---------------------------------------------------------------------------
# 6. Empty Responses, Missing Schemas & Unresolved References
# ---------------------------------------------------------------------------

def test_empty_body_valid_204_no_content():
    """Verify 204 No Content response with empty body passes when no content is declared."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "204": ResponseDefinition(status_code="204", description="No Content")
        }
    )
    res = make_execution_result(status_code=204, headers={}, body=None)

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is True
    assert report.matched_status_key == "204"
    assert all(a.passed for a in report.assertions)


def test_empty_body_invalid_when_schema_required():
    """Verify empty response body fails when a non-empty payload schema is declared."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={"application/json": {"schema": {"type": "object", "required": ["id"]}}},
            )
        }
    )
    res = make_execution_result(status_code=200, headers={"content-type": "application/json"}, body=None)

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is False
    failed = report.failed_assertions
    assert any(a.name == "body_schema_validation" and "empty" in a.message.lower() for a in failed)


def test_unexpected_body_when_no_content_declared():
    """Verify non-empty response body fails when no response content was declared in spec."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "204": ResponseDefinition(status_code="204", description="No Content")
        }
    )
    # Server returned content on 204
    res = make_execution_result(status_code=204, headers={"content-type": "application/json"}, body={"unwanted": "data"})

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is False
    assert any("no response content was declared" in a.message.lower() for a in report.failed_assertions)


def test_missing_schema_allows_any_body():
    """Verify response media-type declared with empty/missing schema allows any payload."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={"application/json": {}},  # No schema key
            )
        }
    )
    res = make_execution_result(status_code=200, body={"any": "dynamic", "payload": 123})

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is True


def test_unresolved_schema_reference_failure():
    """Verify unresolvable schema reference returns structured failure without crashing."""
    evaluator = DeterministicAssertionEvaluator(raw_spec={"components": {"schemas": {}}})
    endpoint = make_endpoint(
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="OK",
                content={
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/NonExistent"}
                    }
                },
            )
        }
    )
    res = make_execution_result(status_code=200, body={"data": 1})

    report = evaluator.evaluate(endpoint, res)

    assert report.is_success is False
    failed = report.failed_assertions
    assert any("reference resolution failed" in a.message.lower() for a in failed)


def test_evaluation_against_transport_failures():
    """Verify SSRF, timeout, and network errors are captured as transport assertion failures."""
    evaluator = DeterministicAssertionEvaluator()
    endpoint = make_endpoint(
        responses={"200": ResponseDefinition(status_code="200", description="OK")}
    )

    # SSRF blocked
    ssrf_res = make_execution_result(
        status_code=0, is_success=False, is_ssrf_blocked=True, error_message="Metadata blocked"
    )
    ssrf_rep = evaluator.evaluate(endpoint, ssrf_res)
    assert ssrf_rep.is_success is False
    assert ssrf_rep.assertions[0].name == "execution_transport"
    assert "ssrf" in ssrf_rep.assertions[0].message.lower()

    # Timeout
    timeout_res = make_execution_result(
        status_code=0, is_success=False, is_timeout=True, error_message="Wall-clock deadline exceeded"
    )
    timeout_rep = evaluator.evaluate(endpoint, timeout_res)
    assert timeout_rep.is_success is False
    assert timeout_rep.assertions[0].name == "execution_transport"
    assert "timed out" in timeout_rep.assertions[0].message.lower()


# ---------------------------------------------------------------------------
# 7. Integration Tests with Phase 1A Mock API & Phase 1B Parsed Spec
# ---------------------------------------------------------------------------

def test_integration_with_phase_1a_mock_api_spec():
    """
    Integration test: Validates real Phase 1A execution outputs against
    the parsed mock_api/openapi.json specification.
    """
    spec = OpenAPIParser.parse_file("mock_api/openapi.json")
    evaluator = DeterministicAssertionEvaluator(raw_spec=spec.raw_spec, openapi_version=spec.openapi_version)

    # 1. GET /api/products -> 200 OK array of products
    get_products_res = make_execution_result(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=200,
        headers={"content-type": "application/json"},
        body=[
            {"id": 1, "name": "Laptop", "price": 999.99, "category": "electronics", "in_stock": True},
            {"id": 2, "name": "Desk", "price": 199.50, "category": "furniture", "in_stock": False},
        ],
    )
    rep1 = evaluator.evaluate_against_spec(spec, get_products_res, endpoint_path="/api/products")
    assert rep1.is_success is True
    assert rep1.matched_status_key == "200"

    # 2. POST /api/products -> 201 Created valid product
    post_product_res = make_execution_result(
        method="POST",
        url="http://127.0.0.1:8081/api/products",
        status_code=201,
        headers={"content-type": "application/json"},
        body={"id": 3, "name": "Mouse", "price": 25.00, "category": "accessories", "in_stock": True},
    )
    rep2 = evaluator.evaluate_against_spec(spec, post_product_res, endpoint_path="/api/products")
    assert rep2.is_success is True
    assert rep2.matched_status_key == "201"

    # 3. POST /api/products (Intentional Defect: price=-500 -> returns unhandled HTTP 500)
    # The spec only defines 201 and 422 for POST /api/products.
    # Evaluator MUST catch that HTTP 500 is unexpected!
    post_defect_res = make_execution_result(
        method="POST",
        url="http://127.0.0.1:8081/api/products",
        status_code=500,
        headers={"content-type": "application/json"},
        body={"detail": "Internal Server Error: Unhandled validation failure for price: -500"},
    )
    rep3 = evaluator.evaluate_against_spec(spec, post_defect_res, endpoint_path="/api/products")
    assert rep3.is_success is False
    assert rep3.matched_status_key is None
    status_fail = [a for a in rep3.assertions if a.name == "status_code_match"][0]
    assert status_fail.passed is False
    assert status_fail.actual == 500

    # 4. DELETE /api/products/{id} -> 204 No Content
    delete_res = make_execution_result(
        method="DELETE",
        url="http://127.0.0.1:8081/api/products/1",
        status_code=204,
        headers={},
        body=None,
    )
    rep4 = evaluator.evaluate_against_spec(spec, delete_res, endpoint_path="/api/products/{product_id}")
    assert rep4.is_success is True
    assert rep4.matched_status_key == "204"


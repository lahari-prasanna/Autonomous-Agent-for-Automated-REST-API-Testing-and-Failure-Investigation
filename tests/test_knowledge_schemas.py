"""
Unit and boundary test suite for RAG Knowledge Document and Chunk Schemas (Phase 4B.1).
Validates structural invariants, source types, HTTP method normalization,
empty/whitespace rejection, maximum lengths, metadata allowlist security,
source_ref credential sanitization, and JSON serialization.
"""

from datetime import datetime, timezone
import json
import pytest
from pydantic import ValidationError

from backend.app.schemas.knowledge import (
    ALLOWED_METADATA_KEYS,
    MAX_CHUNK_CONTENT_LEN,
    MAX_ID_LEN,
    MAX_METADATA_KEY_LEN,
    MAX_METADATA_VAL_LEN,
    MAX_PATH_LEN,
    MAX_REF_LEN,
    MAX_TITLE_LEN,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from backend.app.schemas.failure import FailureCategory


# ===========================================================================
# 1. Valid Documents and Chunks (Minimal and Full)
# ===========================================================================

def test_valid_knowledge_document_minimal():
    """Verifies creation of minimal valid KnowledgeDocument with sensible defaults."""
    doc = KnowledgeDocument(
        document_id="doc-openapi-products-001",
        source_type=KnowledgeSourceType.OPENAPI_SPEC,
        title="Products API OpenAPI Spec",
    )

    assert doc.document_id == "doc-openapi-products-001"
    assert doc.source_type == KnowledgeSourceType.OPENAPI_SPEC
    assert doc.source_type == "openapi_spec"
    assert doc.title == "Products API OpenAPI Spec"
    assert doc.source_ref is None
    assert doc.http_method is None
    assert doc.path_template is None
    assert doc.operation_id is None
    assert isinstance(doc.created_at, datetime)
    assert doc.created_at.tzinfo is not None
    assert doc.metadata == {}


def test_valid_knowledge_document_full():
    """Verifies creation of fully populated KnowledgeDocument using approved metadata keys."""
    fixed_time = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
    doc = KnowledgeDocument(
        document_id="doc-api-guide-002",
        source_type=KnowledgeSourceType.API_DOCUMENTATION,
        title="Products API Developer Guide",
        source_ref="docs/products_v1.md#overview",
        http_method="post",  # Should be normalized to POST
        path_template="/api/products",
        operation_id="create_product",
        created_at=fixed_time,
        metadata={
            "version": "1.0.0",
            "section": "products-api",
            "category": "developer-guide",
            "is_deprecated": False,
            "verified": True,
            "doc_format": "markdown",
        },
    )

    assert doc.document_id == "doc-api-guide-002"
    assert doc.source_type == KnowledgeSourceType.API_DOCUMENTATION
    assert doc.title == "Products API Developer Guide"
    assert doc.source_ref == "docs/products_v1.md#overview"
    assert doc.http_method == "POST"
    assert doc.path_template == "/api/products"
    assert doc.operation_id == "create_product"
    assert doc.created_at == fixed_time
    assert doc.metadata["version"] == "1.0.0"
    assert doc.metadata["section"] == "products-api"
    assert doc.metadata["category"] == "developer-guide"
    assert doc.metadata["is_deprecated"] is False
    assert doc.metadata["verified"] is True
    assert doc.metadata["doc_format"] == "markdown"


def test_valid_knowledge_chunk_minimal():
    """Verifies creation of minimal valid KnowledgeChunk with default index and empty metadata."""
    chunk = KnowledgeChunk(
        chunk_id="chunk-prod-001",
        document_id="doc-openapi-products-001",
        content="GET /api/products returns a list of products with pagination parameters.",
        source_type=KnowledgeSourceType.OPENAPI_SPEC,
    )

    assert chunk.chunk_id == "chunk-prod-001"
    assert chunk.document_id == "doc-openapi-products-001"
    assert chunk.content == "GET /api/products returns a list of products with pagination parameters."
    assert chunk.text == chunk.content  # Property alias
    assert chunk.chunk_index == 0
    assert chunk.source_type == KnowledgeSourceType.OPENAPI_SPEC
    assert chunk.http_method is None
    assert chunk.path_template is None
    assert chunk.operation_id is None
    assert chunk.failure_category is None
    assert chunk.source_ref is None
    assert chunk.metadata == {}


def test_valid_knowledge_chunk_full_with_text_alias():
    """Verifies creation of fully populated KnowledgeChunk constructed with 'text' alias and approved metadata."""
    chunk = KnowledgeChunk(
        chunk_id="chunk-hist-500-01",
        document_id="doc-hist-run-42",
        text="Observed HTTP 500 when price < 0 due to unhandled NegativePriceException.",
        chunk_index=2,
        source_type=KnowledgeSourceType.FAILURE_HISTORY,
        http_method="post",  # Normalized to POST
        path_template="/api/products",
        operation_id="create_product",
        failure_category=FailureCategory.SERVER_ERROR,
        source_ref="run://test-run-42#step-execute",
        metadata={
            "status_code": 500,
            "verified": True,
            "scenario": "negative_price_defect",
            "category": "server_defect",
        },
    )

    assert chunk.chunk_id == "chunk-hist-500-01"
    assert chunk.document_id == "doc-hist-run-42"
    assert "NegativePriceException" in chunk.content
    assert chunk.text == chunk.content
    assert chunk.chunk_index == 2
    assert chunk.source_type == KnowledgeSourceType.FAILURE_HISTORY
    assert chunk.http_method == "POST"
    assert chunk.path_template == "/api/products"
    assert chunk.operation_id == "create_product"
    assert chunk.failure_category == "server_error"
    assert chunk.source_ref == "run://test-run-42#step-execute"
    assert chunk.metadata["status_code"] == 500
    assert chunk.metadata["scenario"] == "negative_price_defect"


# ===========================================================================
# 2. Supported Source Types
# ===========================================================================

def test_every_supported_source_type():
    """Verifies that all 3 required source types work as enum members and string literals."""
    source_types = [
        KnowledgeSourceType.OPENAPI_SPEC,
        KnowledgeSourceType.API_DOCUMENTATION,
        KnowledgeSourceType.FAILURE_HISTORY,
        "openapi_spec",
        "api_documentation",
        "failure_history",
    ]

    for st in source_types:
        doc = KnowledgeDocument(
            document_id=f"doc-{st}",
            source_type=st,
            title=f"Title for {st}",
        )
        chunk = KnowledgeChunk(
            chunk_id=f"chunk-{st}",
            document_id=f"doc-{st}",
            content=f"Content for {st}",
            source_type=st,
        )
        assert doc.source_type in (
            KnowledgeSourceType.OPENAPI_SPEC,
            KnowledgeSourceType.API_DOCUMENTATION,
            KnowledgeSourceType.FAILURE_HISTORY,
        )
        assert chunk.source_type in (
            KnowledgeSourceType.OPENAPI_SPEC,
            KnowledgeSourceType.API_DOCUMENTATION,
            KnowledgeSourceType.FAILURE_HISTORY,
        )


def test_invalid_source_type_rejected():
    """Verifies that unsupported or fabricated source types are rejected with ValidationError."""
    with pytest.raises(ValidationError):
        KnowledgeDocument(
            document_id="doc-invalid",
            source_type="arbitrary_web_crawl",  # Unsupported
            title="Invalid",
        )

    with pytest.raises(ValidationError):
        KnowledgeChunk(
            chunk_id="chunk-invalid",
            document_id="doc-1",
            content="Some text",
            source_type="unsupported_source",  # Unsupported
        )


# ===========================================================================
# 3. Missing Required Fields
# ===========================================================================

def test_missing_required_document_fields():
    """Verifies that omitting any required field on KnowledgeDocument raises ValidationError."""
    with pytest.raises(ValidationError, match="document_id"):
        KnowledgeDocument(source_type=KnowledgeSourceType.OPENAPI_SPEC, title="Title")

    with pytest.raises(ValidationError, match="source_type"):
        KnowledgeDocument(document_id="doc-1", title="Title")

    with pytest.raises(ValidationError, match="title"):
        KnowledgeDocument(document_id="doc-1", source_type=KnowledgeSourceType.OPENAPI_SPEC)


def test_missing_required_chunk_fields():
    """Verifies that omitting any required field on KnowledgeChunk raises ValidationError."""
    with pytest.raises(ValidationError, match="chunk_id"):
        KnowledgeChunk(
            document_id="doc-1",
            content="Sample text",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
        )

    with pytest.raises(ValidationError, match="document_id"):
        KnowledgeChunk(
            chunk_id="chunk-1",
            content="Sample text",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
        )

    with pytest.raises(ValidationError, match="content"):
        KnowledgeChunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
        )

    with pytest.raises(ValidationError, match="source_type"):
        KnowledgeChunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            content="Sample text",
        )


# ===========================================================================
# 4. Empty and Whitespace-Only Content and Identifiers
# ===========================================================================

def test_empty_and_whitespace_only_chunk_content():
    """Contract Requirement: Reject empty or whitespace-only chunk content."""
    invalid_contents = ["", "   ", "\t", "\n\n   \t", " \r\n "]

    for empty_val in invalid_contents:
        with pytest.raises(ValidationError, match="Chunk content cannot be empty or whitespace-only"):
            KnowledgeChunk(
                chunk_id="chunk-1",
                document_id="doc-1",
                content=empty_val,
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
            )

        with pytest.raises(ValidationError, match="Chunk content cannot be empty or whitespace-only"):
            KnowledgeChunk(
                chunk_id="chunk-1",
                document_id="doc-1",
                text=empty_val,
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
            )


def test_empty_and_whitespace_only_identifiers():
    """Verifies that blank or whitespace-only identifiers and titles are rejected."""
    blank_values = ["", "   ", "\t\n"]

    for b in blank_values:
        with pytest.raises(ValidationError, match="document_id cannot be empty"):
            KnowledgeDocument(document_id=b, source_type=KnowledgeSourceType.OPENAPI_SPEC, title="Title")

        with pytest.raises(ValidationError, match="Title cannot be empty"):
            KnowledgeDocument(document_id="doc-1", source_type=KnowledgeSourceType.OPENAPI_SPEC, title=b)

        with pytest.raises(ValidationError, match="chunk_id cannot be empty"):
            KnowledgeChunk(
                chunk_id=b,
                document_id="doc-1",
                content="Valid content",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
            )

        with pytest.raises(ValidationError, match="document_id cannot be empty"):
            KnowledgeChunk(
                chunk_id="chunk-1",
                document_id=b,
                content="Valid content",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
            )


# ===========================================================================
# 5. HTTP Method Normalization and Validation
# ===========================================================================

def test_http_method_normalization_all_verbs():
    """Verifies that lowercase and whitespace-padded HTTP methods are normalized to uppercase."""
    valid_verbs = ["get", "post", "put", "delete", "patch", "options", "head", "trace"]
    for verb in valid_verbs:
        doc = KnowledgeDocument(
            document_id=f"doc-{verb}",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title=f"Doc for {verb}",
            http_method=f"  {verb}  ",
        )
        assert doc.http_method == verb.upper()

        chunk = KnowledgeChunk(
            chunk_id=f"chunk-{verb}",
            document_id=f"doc-{verb}",
            content=f"Content for {verb}",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            http_method=f"  {verb}  ",
        )
        assert chunk.http_method == verb.upper()


def test_invalid_http_methods_rejected():
    """Contract Requirement: Reject invalid or non-standard HTTP methods."""
    invalid_methods = ["INVALID", "FOOBAR", "CONNECT", "GET_POST", "123", "P0ST"]
    for inv in invalid_methods:
        with pytest.raises(ValidationError, match="Invalid HTTP method"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                http_method=inv,
            )

        with pytest.raises(ValidationError, match="Invalid HTTP method"):
            KnowledgeChunk(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Content",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                http_method=inv,
            )


# ===========================================================================
# 6. OpenAPI Path Template Validation
# ===========================================================================

def test_path_template_validation_rules():
    """Verifies OpenAPI route template syntax constraints (leading '/', no schemes, queries, or fragments)."""
    valid_paths = ["/api/products", "/api/products/{id}", "/health", "/v1/users/{user_id}/orders"]
    for p in valid_paths:
        doc = KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            path_template=p,
        )
        assert doc.path_template == p

    # Invalid: missing leading slash
    with pytest.raises(ValidationError, match="Path template must begin with '/'"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            path_template="api/products",
        )

    # Invalid: full URL with scheme
    with pytest.raises(ValidationError, match="Path template cannot contain URI schemes"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            path_template="https://api.example.com/products",
        )

    # Invalid: query string
    with pytest.raises(ValidationError, match="Path template cannot contain query parameters"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            path_template="/api/products?limit=10",
        )

    # Invalid: URI fragment
    with pytest.raises(ValidationError, match="Path template cannot contain URI fragments"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            path_template="/api/products#details",
        )


# ===========================================================================
# 7. Maximum-Length Validation
# ===========================================================================

def test_maximum_length_constraints():
    """Contract Requirement: Apply sensible maximum lengths across all fields."""
    with pytest.raises(ValidationError, match="document_id exceeds maximum length"):
        KnowledgeDocument(
            document_id="d" * (MAX_ID_LEN + 1),
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Valid Title",
        )

    with pytest.raises(ValidationError, match="chunk_id exceeds maximum length"):
        KnowledgeChunk(
            chunk_id="c" * (MAX_ID_LEN + 1),
            document_id="doc-1",
            content="Valid content",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
        )

    with pytest.raises(ValidationError, match="Title exceeds maximum length"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="T" * (MAX_TITLE_LEN + 1),
        )

    with pytest.raises(ValidationError, match="source_ref exceeds maximum length"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            source_ref="r" * (MAX_REF_LEN + 1),
        )

    with pytest.raises(ValidationError, match="Path template exceeds maximum length"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            path_template="/" + "p" * MAX_PATH_LEN,
        )

    with pytest.raises(ValidationError, match="operation_id exceeds maximum length"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            operation_id="o" * (MAX_ID_LEN + 1),
        )

    with pytest.raises(ValidationError, match="Chunk content exceeds maximum length"):
        KnowledgeChunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            content="x" * (MAX_CHUNK_CONTENT_LEN + 1),
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
        )


# ===========================================================================
# 8. Correction 1: Metadata Allowlist & Key-Specific Validation
# ===========================================================================

def test_metadata_rejects_unknown_and_denylist_keys():
    """Security Requirement (Correction 1): Reject any key not in ALLOWED_METADATA_KEYS."""
    unapproved_keys = [
        "auth",
        "passwd",
        "bearer",
        "private_key",
        "authorization",
        "cookie",
        "set-cookie",
        "token",
        "password",
        "secret",
        "session",
        "unknown_custom_key",
        "arbitrary_field",
    ]

    for bad_key in unapproved_keys:
        with pytest.raises(ValidationError, match="Unrecognized or forbidden metadata key"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                metadata={bad_key: "val"},
            )

        with pytest.raises(ValidationError, match="Unrecognized or forbidden metadata key"):
            KnowledgeChunk(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Content",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                metadata={bad_key: "val"},
            )


def test_metadata_accepts_all_approved_keys_with_valid_values():
    """Verification: Every approved key in ALLOWED_METADATA_KEYS is accepted when provided valid data."""
    valid_meta = {
        "tag": "products",
        "category": "catalog",
        "version": "v1.2",
        "status_code": 200,
        "section": "pagination",
        "doc_format": "markdown",
        "is_deprecated": False,
        "content_type": "application/json",
        "scenario": "get_product_success",
        "verified": True,
    }

    doc = KnowledgeDocument(
        document_id="doc-1",
        source_type=KnowledgeSourceType.OPENAPI_SPEC,
        title="Title",
        metadata=valid_meta,
    )
    assert doc.metadata == valid_meta
    assert len(doc.metadata) == len(ALLOWED_METADATA_KEYS)


def test_metadata_key_specific_type_validation():
    """Security Requirement (Correction 1): Enforce key-specific value types."""
    # 1. status_code must be int in 100..599 (bool, string, out-of-range rejected)
    for bad_sc in ["200", True, False, 99, 600, 3.14]:
        with pytest.raises(ValidationError, match="Metadata 'status_code'"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                metadata={"status_code": bad_sc},
            )

    # 2. is_deprecated must be bool
    for bad_dep in ["true", 1, 0, None, [True]]:
        with pytest.raises(ValidationError, match="Metadata 'is_deprecated' must be a boolean"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                metadata={"is_deprecated": bad_dep},
            )

    # 3. verified must be bool
    for bad_ver in ["false", 0, 1, None]:
        with pytest.raises(ValidationError, match="Metadata 'verified' must be a boolean"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                metadata={"verified": bad_ver},
            )

    # 4. string fields: empty or whitespace-only rejected
    for empty_str in ["", "   ", "\t\n"]:
        with pytest.raises(ValidationError, match="cannot be empty or whitespace-only"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                metadata={"tag": empty_str},
            )


def test_metadata_rejects_sensitive_value_patterns():
    """Security Requirement (Correction 1): Reject obvious bearer tokens, JWTs, private keys, and credential assignments."""
    sensitive_values = [
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDc",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgN",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...",
        "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49...",
        "password=SuperSecretPassword123",
        "passwd=admin123",
        "secret=MySecretKey2026",
        "api_key=live_sec_9999",
        "token=ghp_ABC1234567890",
    ]

    for sens_val in sensitive_values:
        with pytest.raises(ValidationError, match="prohibited sensitive credential pattern"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                metadata={"tag": sens_val},
            )

        with pytest.raises(ValidationError, match="prohibited sensitive credential pattern"):
            KnowledgeChunk(
                chunk_id="chunk-1",
                document_id="doc-1",
                content="Content",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                metadata={"section": sens_val},
            )


def test_metadata_rejects_nested_values_and_unbounded_sizes():
    """Security Requirement: Rejects nested dicts/lists and oversized strings."""
    with pytest.raises(ValidationError, match="Metadata 'tag' must be a string"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            metadata={"tag": {"nested": "dict"}},
        )

    with pytest.raises(ValidationError, match="Metadata 'tag' must be a string"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            metadata={"tag": ["list", "value"]},
        )

    # Value too long (> MAX_METADATA_VAL_LEN = 256)
    with pytest.raises(ValidationError, match="Metadata string value .* exceeds maximum length"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            metadata={"tag": "x" * (MAX_METADATA_VAL_LEN + 1)},
        )


# ===========================================================================
# 9. Correction 2: Secure Source Reference Handling
# ===========================================================================

def test_source_ref_query_tokens_cannot_survive():
    """Security Requirement (Correction 2): Query tokens must be stripped completely from stored references."""
    urls_with_queries = [
        ("https://example.com/docs?access_token=secret_token_123", "https://example.com/docs"),
        ("https://example.com/api/v1?token=jwt.abc.xyz&user=admin", "https://example.com/api/v1"),
        ("https://docs.myapi.org/guide?api_key=secret999#section-2", "https://docs.myapi.org/guide#section-2"),
    ]

    for raw_url, expected_normalized in urls_with_queries:
        doc = KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.API_DOCUMENTATION,
            title="Title",
            source_ref=raw_url,
        )
        assert doc.source_ref == expected_normalized
        assert "token" not in doc.source_ref
        assert "secret" not in doc.source_ref


def test_source_ref_fragment_tokens_and_unsafe_fragments_stripped():
    """Security Requirement (Correction 2): Unsafe fragments (tokens) removed; safe section anchors preserved."""
    unsafe_fragment_urls = [
        "https://example.com/docs#access_token=secret_token_abc",
        "https://example.com/docs#token=12345",
        "https://example.com/docs#section/1/2",
        "https://example.com/docs#heading with spaces",
    ]
    for raw_url in unsafe_fragment_urls:
        doc = KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.API_DOCUMENTATION,
            title="Title",
            source_ref=raw_url,
        )
        assert doc.source_ref == "https://example.com/docs"
        assert "token" not in doc.source_ref

    # Safe section anchors matching ^[A-Za-z0-9_.-]+$ must be preserved
    safe_fragment_urls = [
        ("https://example.com/docs#section-1", "https://example.com/docs#section-1"),
        ("https://example.com/docs#overview_v2.0", "https://example.com/docs#overview_v2.0"),
        ("docs/api_guide.md#auth_flow", "docs/api_guide.md#auth_flow"),
    ]
    for raw_url, expected in safe_fragment_urls:
        doc = KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.API_DOCUMENTATION,
            title="Title",
            source_ref=raw_url,
        )
        assert doc.source_ref == expected


def test_source_ref_rejects_literal_and_encoded_credentials():
    """Security Requirement (Correction 2): Reject literal and percent-encoded userinfo in URLs."""
    cred_urls = [
        "https://admin:SuperSecretPass@example.com/docs",
        "https://admin:pass%40example.com/docs",
        "https://user%3Apass%40internal.specs.com/openapi.json",
        "http://token_user@api.host.com/spec.yaml",
    ]

    for cred_url in cred_urls:
        with pytest.raises(ValidationError, match="embedded credentials in URL"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                source_ref=cred_url,
            )


def test_source_ref_rejects_absolute_paths_and_traversals():
    """Security Requirement (Correction 2): Reject absolute local paths, home dirs, and traversal segments."""
    bad_local_refs = [
        "/etc/passwd",
        "/var/run/secrets/token",
        "/home/developer/specs/openapi.json",
        "C:\\Users\\admin\\secrets.json",
        "C:/Windows/System32/drivers",
        "~/docs/api.md",
        "~/secrets.txt",
        "../secret_file.md",
        "docs/../../etc/shadow",
        "docs/../secret.json",
        "..",
    ]

    for bad_ref in bad_local_refs:
        with pytest.raises(ValidationError, match="(Absolute local filesystem paths|Home-directory|Path traversal)"):
            KnowledgeDocument(
                document_id="doc-1",
                source_type=KnowledgeSourceType.OPENAPI_SPEC,
                title="Title",
                source_ref=bad_ref,
            )


def test_source_ref_accepts_valid_relative_references():
    """Verification: Legitimate relative workspace references are preserved."""
    valid_local_refs = [
        "docs/products_v1.md",
        "mock_api/openapi.json",
        "mock_api/openapi.json#paths-products",
        "specs/v1/auth.md#oauth-overview",
        "RFC 9110 Section 15.5.1",
    ]

    for ref in valid_local_refs:
        doc = KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            source_ref=ref,
        )
        assert doc.source_ref == ref


def test_source_ref_serialization_contains_zero_secrets():
    """Security Requirement: Serialized JSON output never retains stripped credentials or query secrets."""
    secret_pass = "MySecretPass#2026"
    secret_token = "eyJhbGciOiJIUzI1NiJ9.leak_token_999"

    doc = KnowledgeDocument(
        document_id="doc-clean-01",
        source_type=KnowledgeSourceType.API_DOCUMENTATION,
        title="Sanitized Doc",
        source_ref=f"https://example.com/docs?token={secret_token}&auth={secret_pass}#frag_token={secret_token}",
        metadata={"tag": "public-docs", "section": "intro"},
    )

    dumped = doc.model_dump_json()
    assert secret_pass not in dumped
    assert secret_token not in dumped
    assert "token=" not in dumped
    assert "auth=" not in dumped
    assert doc.source_ref == "https://example.com/docs"


# ===========================================================================
# 10. Extra Field Injection Forbidden
# ===========================================================================

def test_extra_fields_forbidden():
    """Security Requirement: Reject unauthorized/injected fields (e.g. embeddings, raw bodies, secrets)."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        KnowledgeDocument(
            document_id="doc-1",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title="Title",
            raw_body={"injected": "payload"},  # Extra field
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        KnowledgeChunk(
            chunk_id="chunk-1",
            document_id="doc-1",
            content="Content",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            embedding=[0.1, 0.2, 0.3],  # Extra vector field
        )


# ===========================================================================
# 11. JSON Serialization and Deserialization Round-Trip
# ===========================================================================

def test_json_serialization_and_round_trip():
    """Contract Requirement: Models must be cleanly JSON serializable and deserializable."""
    created = datetime(2026, 11, 3, 14, 30, 0, tzinfo=timezone.utc)
    doc = KnowledgeDocument(
        document_id="doc-serialize-01",
        source_type=KnowledgeSourceType.OPENAPI_SPEC,
        title="Products Spec",
        source_ref="mock_api/openapi.json",
        http_method="GET",
        path_template="/api/products/{id}",
        operation_id="get_product",
        created_at=created,
        metadata={"tag": "products", "version": "v1.0", "status_code": 200},
    )

    chunk = KnowledgeChunk(
        chunk_id="chunk-serialize-01",
        document_id=doc.document_id,
        content="Detailed response schema for GET /api/products/{id}",
        chunk_index=0,
        source_type=KnowledgeSourceType.OPENAPI_SPEC,
        http_method="GET",
        path_template="/api/products/{id}",
        operation_id="get_product",
        failure_category=FailureCategory.RESPONSE_SCHEMA_MISMATCH,
        source_ref="mock_api/openapi.json#products-get",
        metadata={"status_code": 200, "verified": True},
    )

    # 1. Pydantic model_dump_json() round-trip
    doc_json = doc.model_dump_json()
    assert isinstance(doc_json, str)
    doc_restored = KnowledgeDocument.model_validate_json(doc_json)
    assert doc_restored.document_id == doc.document_id
    assert doc_restored.source_type == doc.source_type
    assert doc_restored.title == doc.title
    assert doc_restored.http_method == doc.http_method
    assert doc_restored.path_template == doc.path_template
    assert doc_restored.created_at == doc.created_at
    assert doc_restored.metadata == doc.metadata

    chunk_json = chunk.model_dump_json()
    assert isinstance(chunk_json, str)
    chunk_restored = KnowledgeChunk.model_validate_json(chunk_json)
    assert chunk_restored.chunk_id == chunk.chunk_id
    assert chunk_restored.document_id == chunk.document_id
    assert chunk_restored.content == chunk.content
    assert chunk_restored.failure_category == "response_schema_mismatch"
    assert chunk_restored.metadata == chunk.metadata

    # 2. Standard library json.dumps() compatibility
    doc_dict = doc.model_dump(mode="json")
    std_json_str = json.dumps(doc_dict)
    assert "doc-serialize-01" in std_json_str
    assert "2026-11-03T14:30:00Z" in std_json_str or "2026-11-03T14:30:00+00:00" in std_json_str


# ===========================================================================
# 12. Stable Identifiers & Parent-Document Relationships
# ===========================================================================

def test_stable_identifiers_and_parent_document_relationship():
    """Contract Requirement: Chunks must explicitly link to parent document via document_id."""
    parent_doc = KnowledgeDocument(
        document_id="doc-rfc9110-http-semantics",
        source_type=KnowledgeSourceType.API_DOCUMENTATION,
        title="RFC 9110: HTTP Semantics",
        source_ref="https://www.rfc-editor.org/rfc/rfc9110#section-15",
    )

    chunks = [
        KnowledgeChunk(
            chunk_id=f"chunk-rfc9110-{i}",
            document_id=parent_doc.document_id,
            content=f"RFC 9110 Section {i} content text...",
            chunk_index=i,
            source_type=parent_doc.source_type,
            source_ref=f"RFC 9110 Section {i}",
        )
        for i in range(3)
    ]

    for i, c in enumerate(chunks):
        assert c.document_id == parent_doc.document_id
        assert c.chunk_index == i
        assert c.source_type == KnowledgeSourceType.API_DOCUMENTATION

    # Negative chunk index rejected
    with pytest.raises(ValidationError, match="Input should be greater than or equal to 0"):
        KnowledgeChunk(
            chunk_id="chunk-neg",
            document_id=parent_doc.document_id,
            content="Negative index content",
            chunk_index=-1,
            source_type=KnowledgeSourceType.API_DOCUMENTATION,
        )

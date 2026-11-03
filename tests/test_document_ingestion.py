"""
Comprehensive test suite for Document Ingestion and Chunking Service (Phase 4B.2).
Validates filesystem security, component-by-component traversal, TOCTOU defenses,
identity collision-resistance, OpenAPI extraction limits, content sanitization & quarantine,
failure history provenance verification, and strict chunk-size boundaries.
"""

from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Dict, List
from unittest.mock import patch
import pytest

from backend.app.schemas.failure import (
    FailureCategory,
    FailureClassification,
    FailureSeverity,
    RetryGuidance,
    RetryPolicy,
    SanitizedFailureEvidence,
)
from backend.app.schemas.knowledge import (
    MAX_ID_LEN,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec
from backend.app.services.document_ingestion import (
    APPROVED_SOURCE_ROOTS,
    MAX_CHUNK_CONTENT_CHARS,
    MAX_FILE_SIZE_BYTES,
    MAX_OPERATION_TOTAL_PROPERTIES,
    DocumentIngestionService,
    DocumentSizeLimitError,
    IngestionError,
    IngestionResult,
    IngestionWarning,
    MalformedDocumentError,
    ProvenanceVerificationError,
    SecurityPathError,
    SourceNotFoundError,
    _read_bounded_fd,
    compute_source_fingerprint,
    compute_source_slug,
    read_secure_file,
    sanitize_text,
)


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def mock_spec() -> ParsedAPISpec:
    """Provides a normalized ParsedAPISpec matching mock_api."""
    return ParsedAPISpec(
        title="Mock Store API",
        version="1.0.0",
        openapi_version="3.0.3",
        description="Controlled test API",
        endpoints=[
            EndpointDefinition(
                path="/api/products",
                method="GET",
                summary="List products",
                description="Returns catalog products",
                operation_id="list_products",
                tags=["products"],
            ),
            EndpointDefinition(
                path="/api/products",
                method="POST",
                summary="Create product",
                description="Creates a new catalog product",
                operation_id="create_product",
                tags=["products"],
            ),
            EndpointDefinition(
                path="/api/products/{id}",
                method="GET",
                summary="Get product",
                description="Returns product by ID",
                operation_id="get_product",
                tags=["products"],
            ),
        ],
    )


@pytest.fixture
def test_workspace(tmp_path: Path) -> Path:
    """Sets up an isolated workspace with approved roots."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "docs").mkdir()
    (ws / "specs").mkdir()
    (ws / "mock_api").mkdir()
    return ws


# ===========================================================================
# 1. Filesystem Security Tests
# ===========================================================================

def test_read_secure_file_normal_file(test_workspace: Path):
    """Verifies reading a legitimate file inside an approved root."""
    guide_file = test_workspace / "docs" / "guide.md"
    guide_file.write_text("# Developer Guide\n\nContent here.", encoding="utf-8")

    data, st = read_secure_file(str(test_workspace), "docs", "guide.md")
    assert b"Developer Guide" in data
    assert stat.S_ISREG(st.st_mode)


def test_read_secure_file_unapproved_root_rejected(test_workspace: Path):
    """Verifies access to roots outside APPROVED_SOURCE_ROOTS is rejected."""
    secret_dir = test_workspace / "secret"
    secret_dir.mkdir()
    secret_file = secret_dir / "secret.txt"
    secret_file.write_text("classified", encoding="utf-8")

    with pytest.raises(SecurityPathError, match="not in allowed roots"):
        read_secure_file(str(test_workspace), "secret", "secret.txt")


def test_read_secure_file_absolute_paths_rejected(test_workspace: Path):
    """Verifies absolute paths, drive letters, and home directory prefixes are rejected."""
    with pytest.raises(SecurityPathError, match="Absolute or drive paths are not permitted"):
        read_secure_file(str(test_workspace), "docs", "/etc/passwd")

    with pytest.raises(SecurityPathError, match="Absolute or drive paths are not permitted"):
        read_secure_file(str(test_workspace), "docs", "C:\\Windows\\win.ini")

    with pytest.raises(SecurityPathError, match="Absolute or drive paths are not permitted"):
        read_secure_file(str(test_workspace), "docs", "~/secret.md")


def test_read_secure_file_traversal_rejected(test_workspace: Path):
    """Verifies standard and mixed separator directory traversal attempts are rejected."""
    traversal_paths = [
        "../secret.txt",
        "..\\secret.txt",
        "docs/../../etc/passwd",
        "sub/../..",
        "sub/..\\..",
    ]
    for p in traversal_paths:
        with pytest.raises(SecurityPathError, match="Path traversal"):
            read_secure_file(str(test_workspace), "docs", p)


def test_read_secure_file_encoded_traversal_rejected(test_workspace: Path):
    """Verifies single and double URL-encoded traversals are decoded and rejected."""
    encoded_paths = [
        "%2e%2e/secret.txt",
        "%2e%2e%2fsecret.txt",
        "%252e%252e/secret.txt",
        "sub/%2e%2e/%2e%2e/etc/passwd",
    ]
    for p in encoded_paths:
        with pytest.raises(SecurityPathError, match="Path traversal"):
            read_secure_file(str(test_workspace), "docs", p)


def test_read_secure_file_null_byte_rejected(test_workspace: Path):
    """Verifies paths containing null bytes are rejected."""
    with pytest.raises(SecurityPathError, match="Null byte detected"):
        read_secure_file(str(test_workspace), "docs", "guide.md\x00.exe")


def test_read_secure_file_final_component_symlink_rejected(test_workspace: Path):
    """Verifies symlinked target file fails regular file check or O_NOFOLLOW."""
    real_file = test_workspace / "docs" / "real.txt"
    real_file.write_text("Real content", encoding="utf-8")
    symlink_file = test_workspace / "docs" / "link.txt"
    try:
        symlink_file.symlink_to(real_file)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/filesystem.")

    with pytest.raises(SecurityPathError):
        read_secure_file(str(test_workspace), "docs", "link.txt")


def test_read_secure_file_intermediate_component_symlink_rejected(test_workspace: Path):
    """Verifies symlinked intermediate directory components are blocked by O_NOFOLLOW."""
    real_dir = test_workspace / "docs" / "real_dir"
    real_dir.mkdir()
    (real_dir / "target.txt").write_text("Content", encoding="utf-8")

    symlink_dir = test_workspace / "docs" / "sym_dir"
    try:
        symlink_dir.symlink_to(real_dir)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/filesystem.")

    with pytest.raises(SecurityPathError):
        read_secure_file(str(test_workspace), "docs", "sym_dir/target.txt")


def test_read_secure_file_symlink_escape_rejected(test_workspace: Path):
    """Verifies symlink pointing outside approved root is rejected."""
    outside_file = test_workspace / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    escape_link = test_workspace / "docs" / "escape.txt"
    try:
        escape_link.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/filesystem.")

    with pytest.raises(SecurityPathError):
        read_secure_file(str(test_workspace), "docs", "escape.txt")


def test_read_secure_file_oversized_file_rejected(test_workspace: Path):
    """Verifies files exceeding 1 MB are rejected before/during read."""
    oversized = test_workspace / "docs" / "large.txt"
    oversized.write_bytes(b"A" * (MAX_FILE_SIZE_BYTES + 1024))

    with pytest.raises(DocumentSizeLimitError, match="exceeds limit"):
        read_secure_file(str(test_workspace), "docs", "large.txt")


def test_read_secure_file_non_posix_fallback(test_workspace: Path, monkeypatch):
    """Verifies fallback path containment and reading when POSIX dir_fd is disabled."""
    guide_file = test_workspace / "docs" / "fallback_guide.md"
    guide_file.write_text("# Fallback Guide\n\nContent.", encoding="utf-8")

    # Force fallback branch
    monkeypatch.setattr(os, "supports_dir_fd", set())

    data, st = read_secure_file(str(test_workspace), "docs", "fallback_guide.md")
    assert b"Fallback Guide" in data
    assert stat.S_ISREG(st.st_mode)


# ===========================================================================
# 2. Identity and Reproducibility Tests
# ===========================================================================

def test_identical_input_produces_identical_ids(test_workspace: Path):
    """Verifies that re-ingesting identical content produces identical document and chunk IDs."""
    svc = DocumentIngestionService(str(test_workspace))
    file_path = test_workspace / "docs" / "api.md"
    file_path.write_text("# Store API\n\n## Overview\n\nWelcome to Store API.", encoding="utf-8")

    fixed_time = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
    res1 = svc.ingest_documentation_file("api.md", root_name="docs", fixed_timestamp=fixed_time)
    res2 = svc.ingest_documentation_file("api.md", root_name="docs", fixed_timestamp=fixed_time)

    assert res1.documents[0].document_id == res2.documents[0].document_id
    assert [c.chunk_id for c in res1.chunks] == [c.chunk_id for c in res2.chunks]
    assert res1.documents[0].model_dump_json() == res2.documents[0].model_dump_json()


def test_different_paths_identical_content_distinct_identities(test_workspace: Path):
    """Verifies different files with identical content maintain distinct provenance and IDs."""
    svc = DocumentIngestionService(str(test_workspace))
    (test_workspace / "docs" / "v1").mkdir()
    (test_workspace / "docs" / "v2").mkdir()

    f1 = test_workspace / "docs" / "v1" / "api.md"
    f2 = test_workspace / "docs" / "v2" / "api.md"
    f1.write_text("# Store API\n\n## Overview\n\nIdentical text in both files.", encoding="utf-8")
    f2.write_text("# Store API\n\n## Overview\n\nIdentical text in both files.", encoding="utf-8")

    res1 = svc.ingest_documentation_file("v1/api.md", root_name="docs")
    res2 = svc.ingest_documentation_file("v2/api.md", root_name="docs")

    assert res1.documents[0].document_id != res2.documents[0].document_id
    assert res1.chunks[0].chunk_id != res2.chunks[0].chunk_id


def test_case_sensitive_paths_distinct_identities():
    """Verifies case-sensitive paths remain distinct in source fingerprints."""
    fp1 = compute_source_fingerprint(KnowledgeSourceType.API_DOCUMENTATION, "docs", "guide.md")
    fp2 = compute_source_fingerprint(KnowledgeSourceType.API_DOCUMENTATION, "docs", "Guide.md")
    assert fp1 != fp2


def test_unicode_nfc_paths_normalize_consistently():
    """Verifies Unicode NFC normalization ensures equivalent paths produce identical fingerprint."""
    # NFD decomposed 'e\u0301' vs NFC composed '\u00e9'
    path_nfd = "cafe\u0301.md"
    path_nfc = "caf\u00e9.md"

    fp_nfd = compute_source_fingerprint(KnowledgeSourceType.API_DOCUMENTATION, "docs", path_nfd)
    fp_nfc = compute_source_fingerprint(KnowledgeSourceType.API_DOCUMENTATION, "docs", path_nfc)
    assert fp_nfd == fp_nfc


def test_source_type_and_root_namespace_affect_identity():
    """Verifies source type and approved root namespace partition source fingerprints."""
    fp_docs = compute_source_fingerprint(KnowledgeSourceType.API_DOCUMENTATION, "docs", "spec.json")
    fp_specs = compute_source_fingerprint(KnowledgeSourceType.OPENAPI_SPEC, "specs", "spec.json")
    assert fp_docs != fp_specs


def test_unchanged_sections_retain_chunk_ids_after_unrelated_edit(test_workspace: Path):
    """
    Contract Requirement: Unchanged sections retain their chunk IDs even if another
    section is edited or added elsewhere in the document.
    """
    svc = DocumentIngestionService(str(test_workspace))
    doc_file = test_workspace / "docs" / "manual.md"

    # Version 1
    doc_file.write_text(
        "# Manual\n\n"
        "## Authentication\n\nAuthentication requires Bearer token format.\n\n"
        "## Pagination\n\nUse limit and offset parameters to page results.",
        encoding="utf-8",
    )
    res1 = svc.ingest_documentation_file("manual.md", root_name="docs")
    pagination_chunk_v1 = next(c for c in res1.chunks if "pagination" in c.chunk_id)

    # Version 2: Inserted new introductory section at the top, modified Authentication
    doc_file.write_text(
        "# Manual\n\n"
        "## Welcome\n\nNew introductory section added at the beginning.\n\n"
        "## Authentication\n\nUpdated auth explanation text.\n\n"
        "## Pagination\n\nUse limit and offset parameters to page results.",
        encoding="utf-8",
    )
    res2 = svc.ingest_documentation_file("manual.md", root_name="docs")
    pagination_chunk_v2 = next(c for c in res2.chunks if "pagination" in c.chunk_id)

    # Pagination section content was completely untouched -> chunk ID must be IDENTICAL
    assert pagination_chunk_v1.chunk_id == pagination_chunk_v2.chunk_id
    assert pagination_chunk_v1.content == pagination_chunk_v2.content


def test_duplicate_heading_occurrence_behavior(test_workspace: Path):
    """Verifies duplicate headings in a document are sequentially disambiguated."""
    svc = DocumentIngestionService(str(test_workspace))
    doc_file = test_workspace / "docs" / "dups.md"
    doc_file.write_text(
        "# Document\n\n"
        "## Overview\n\nFirst overview content section.\n\n"
        "## Overview\n\nSecond overview content section.",
        encoding="utf-8",
    )
    res = svc.ingest_documentation_file("dups.md", root_name="docs")
    assert len(res.chunks) == 2
    assert res.chunks[0].metadata["section"] == "overview"
    assert res.chunks[1].metadata["section"] == "overview-2"
    assert res.chunks[0].chunk_id != res.chunks[1].chunk_id


def test_oversized_section_partition_shifts(test_workspace: Path):
    """Verifies editing inside an oversized section shifts subsequent partition IDs cleanly."""
    svc = DocumentIngestionService(str(test_workspace))
    doc_file = test_workspace / "docs" / "oversized.md"

    # Single oversized paragraph containing many sentences (> 2,048 characters)
    sentences = [f"Detailed sentence with extensive information number {i}." for i in range(60)]
    long_content = " ".join(sentences)

    doc_file.write_text(f"# Guide\n\n## Deep Dive\n\n{long_content}", encoding="utf-8")
    res1 = svc.ingest_documentation_file("oversized.md", root_name="docs")
    assert len(res1.chunks) >= 2
    part0_id_v1 = res1.chunks[0].chunk_id
    part1_id_v1 = res1.chunks[1].chunk_id

    # Edit earlier text by inserting a new sentence at the beginning with enough length to push boundary
    doc_file.write_text(f"# Guide\n\n## Deep Dive\n\nNewly inserted sentence with enough characters to force boundary shift. {long_content}", encoding="utf-8")
    res2 = svc.ingest_documentation_file("oversized.md", root_name="docs")
    assert res2.chunks[0].chunk_id != part0_id_v1
    assert res2.chunks[1].chunk_id != part1_id_v1  # Downstream partition shifted


def test_fixed_timestamp_reproducible_serialization(test_workspace: Path):
    """Verifies fixed timestamp injection produces bit-for-bit identical JSON serialization."""
    svc = DocumentIngestionService(str(test_workspace))
    doc_file = test_workspace / "docs" / "fixed.md"
    doc_file.write_text("# Doc\n\n## Intro\n\nSample intro text.", encoding="utf-8")

    fixed_time = datetime(2026, 11, 3, 14, 0, 0, tzinfo=timezone.utc)
    res1 = svc.ingest_documentation_file("fixed.md", root_name="docs", fixed_timestamp=fixed_time)
    res2 = svc.ingest_documentation_file("fixed.md", root_name="docs", fixed_timestamp=fixed_time)

    assert res1.documents[0].model_dump_json() == res2.documents[0].model_dump_json()
    assert res1.chunks[0].model_dump_json() == res2.chunks[0].model_dump_json()


# ===========================================================================
# 3. OpenAPI Extraction Tests
# ===========================================================================

def test_openapi_valid_operation_extraction(mock_spec: ParsedAPISpec, test_workspace: Path):
    """Verifies extracting operations from a parsed OpenAPI spec into KnowledgeChunks."""
    svc = DocumentIngestionService(str(test_workspace))
    res = svc.ingest_openapi_spec(mock_spec)

    assert len(res.documents) == 1
    assert len(res.chunks) == 3
    assert res.documents[0].source_type == KnowledgeSourceType.OPENAPI_SPEC

    methods = [c.http_method for c in res.chunks]
    assert "GET" in methods
    assert "POST" in methods

    for c in res.chunks:
        assert c.document_id == res.documents[0].document_id
        assert c.source_type == KnowledgeSourceType.OPENAPI_SPEC
        assert c.content.startswith("### ")


def test_openapi_exclusion_of_examples_and_credentials(test_workspace: Path):
    """Security Requirement: Raw examples and credentials in OpenAPI schemas are excluded."""
    spec = ParsedAPISpec(
        title="Auth API",
        version="1.0.0",
        openapi_version="3.0.3",
        endpoints=[
            EndpointDefinition(
                path="/api/login",
                method="POST",
                summary="Login endpoint",
                description="Performs user login",
                operation_id="login",
                request_body={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "username": {"type": "string", "example": "admin_user"},
                                    "password": {"type": "string", "example": "SuperSecretPass123"},
                                },
                            },
                            "example": {"username": "admin", "password": "RAW_PASSWORD_LEAK"},
                        }
                    },
                },
            )
        ],
    )
    svc = DocumentIngestionService(str(test_workspace))
    res = svc.ingest_openapi_spec(spec)
    chunk_text = res.chunks[0].content

    assert "RAW_PASSWORD_LEAK" not in chunk_text
    assert "SuperSecretPass123" not in chunk_text
    assert "password: [SENSITIVE_FIELD_REDACTED]" in chunk_text


def test_openapi_depth_limit_enforced(test_workspace: Path):
    """Verifies schema nesting deeper than MAX_SCHEMA_DEPTH (4) is truncated."""
    deep_schema: Dict[str, Any] = {"type": "object", "properties": {}}
    curr = deep_schema
    for i in range(7):
        curr["properties"] = {f"level_{i}": {"type": "object", "properties": {}}}
        curr = curr["properties"][f"level_{i}"]

    spec = ParsedAPISpec(
        title="Deep API",
        version="1.0.0",
        openapi_version="3.0.3",
        endpoints=[
            EndpointDefinition(
                path="/api/deep",
                method="POST",
                operation_id="deep_op",
                request_body={"content": {"application/json": {"schema": deep_schema}}},
            )
        ],
    )
    svc = DocumentIngestionService(str(test_workspace))
    res = svc.ingest_openapi_spec(spec)
    assert "[TRUNCATED_SCHEMA_DEPTH_EXCEEDED]" in res.chunks[0].content


def test_openapi_cumulative_property_budget_enforced(test_workspace: Path):
    """Verifies cumulative property count across operation is bounded by MAX_OPERATION_TOTAL_PROPERTIES (80)."""
    # 90 properties
    many_props = {f"prop_{i}": {"type": "string"} for i in range(90)}
    spec = ParsedAPISpec(
        title="Wide API",
        version="1.0.0",
        openapi_version="3.0.3",
        endpoints=[
            EndpointDefinition(
                path="/api/wide",
                method="POST",
                operation_id="wide_op",
                request_body={"content": {"application/json": {"schema": {"type": "object", "properties": many_props}}}},
            )
        ],
    )
    svc = DocumentIngestionService(str(test_workspace))
    res = svc.ingest_openapi_spec(spec)

    assert "[TRUNCATED_OPERATION_PROPERTY_BUDGET_EXCEEDED]" in res.chunks[0].content
    assert any(w.warning_code == "SCHEMA_BUDGET_TRUNCATED" for w in res.warnings)


def test_openapi_circular_local_references(test_workspace: Path):
    """Verifies local $ref is preserved without recursive resolution error."""
    spec = ParsedAPISpec(
        title="Recursive API",
        version="1.0.0",
        openapi_version="3.0.3",
        endpoints=[
            EndpointDefinition(
                path="/api/node",
                method="GET",
                operation_id="get_node",
                request_body={"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}},
            )
        ],
    )
    svc = DocumentIngestionService(str(test_workspace))
    res = svc.ingest_openapi_spec(spec)
    assert "$ref: #/components/schemas/Node" in res.chunks[0].content


def test_openapi_quarantine_suspicious_content(test_workspace: Path):
    """Verifies operation with suspicious unredacted header or token is quarantined."""
    spec = ParsedAPISpec(
        title="Quarantine API",
        version="1.0.0",
        openapi_version="3.0.3",
        endpoints=[
            EndpointDefinition(
                path="/api/bad",
                method="GET",
                summary="Bad op Set-Cookie: session=secret_cookie_leak_9999",
                operation_id="bad_op",
            )
        ],
    )
    svc = DocumentIngestionService(str(test_workspace))
    res = svc.ingest_openapi_spec(spec)

    assert len(res.chunks) == 0
    assert res.quarantined_count == 1
    assert any(w.warning_code == "QUARANTINED_SENSITIVE_CONTENT" for w in res.warnings)
    # Ensure raw secret is NOT in warning
    for w in res.warnings:
        assert "secret_cookie_leak_9999" not in w.reason
        assert "secret_cookie_leak_9999" not in w.scope


# ===========================================================================
# 4. Failure History Ingestion Tests
# ===========================================================================

def test_failure_history_valid_classification(mock_spec: ParsedAPISpec, test_workspace: Path):
    """Verifies ingesting a validated FailureClassification outcome."""
    svc = DocumentIngestionService(str(test_workspace))
    fc = FailureClassification(
        has_failure=True,
        primary_category=FailureCategory.SERVER_ERROR,
        severity=FailureSeverity.HIGH,
        summary="Internal server error observed on negative price",
        retry_guidance=RetryGuidance(
            policy=RetryPolicy.DO_NOT_RETRY,
            is_idempotent_method=False,
            reason="500 Internal Server Error represents server-side defect.",
        ),
        evidence=SanitizedFailureEvidence(
            http_method="POST",
            path_template="/api/products",
            status_code=500,
            contract_violation_summaries=["Unexpected HTTP 500 status"],
        ),
    )

    res = svc.ingest_failure_classification(fc, spec=mock_spec, run_id="run_42")
    assert len(res.documents) == 1
    assert len(res.chunks) == 1
    chunk = res.chunks[0]

    assert chunk.source_type == KnowledgeSourceType.FAILURE_HISTORY
    assert chunk.http_method == "POST"
    assert chunk.path_template == "/api/products"
    assert "Server Error" in chunk.content or "server_error" in chunk.content
    assert chunk.metadata["status_code"] == 500
    assert chunk.metadata["verified"] is True


def test_failure_history_valid_sanitized_record(mock_spec: ParsedAPISpec, test_workspace: Path):
    """Verifies ingesting a sanitized Phase 4A compact history dictionary."""
    svc = DocumentIngestionService(str(test_workspace))
    history_entry = {
        "status": "evaluated",
        "method": "GET",
        "path_template": "/api/products/{id}",
        "status_code": 404,
        "latency_ms": 12.5,
        "error_type": None,
        "error_step": None,
    }

    res = svc.ingest_failure_classification(history_entry, spec=mock_spec, run_id="run_43")
    assert len(res.chunks) == 1
    assert res.chunks[0].path_template == "/api/products/{id}"
    assert res.chunks[0].failure_category == "resource_not_found"


def test_failure_history_raw_agent_state_rejected(mock_spec: ParsedAPISpec, test_workspace: Path):
    """Security Requirement: Raw AgentState dictionary is rejected."""
    svc = DocumentIngestionService(str(test_workspace))
    raw_state = {
        "status": "error",
        "messages": ["some raw message"],
        "raw_response": "unfiltered response",
    }
    with pytest.raises(MalformedDocumentError, match="Arbitrary dictionaries are not accepted"):
        svc.ingest_failure_classification(raw_state, spec=mock_spec)


def test_failure_history_arbitrary_dict_rejected(mock_spec: ParsedAPISpec, test_workspace: Path):
    """Security Requirement: Arbitrary dictionaries with injected fields are rejected."""
    svc = DocumentIngestionService(str(test_workspace))
    with pytest.raises(MalformedDocumentError):
        svc.ingest_failure_classification({"arbitrary_field": "injected_val"}, spec=mock_spec)


def test_failure_history_unverified_endpoint_provenance_rejected(mock_spec: ParsedAPISpec, test_workspace: Path):
    """Security Requirement: Failure pointing to path not in spec.endpoints is rejected."""
    svc = DocumentIngestionService(str(test_workspace))
    fc = FailureClassification(
        has_failure=True,
        primary_category=FailureCategory.SERVER_ERROR,
        summary="Server error",
        retry_guidance=RetryGuidance(policy=RetryPolicy.DO_NOT_RETRY, is_idempotent_method=False, reason="error"),
        evidence=SanitizedFailureEvidence(
            http_method="POST",
            path_template="/api/unverified_endpoint_hack",  # Not in mock_spec!
            status_code=500,
        ),
    )

    with pytest.raises(ProvenanceVerificationError, match="provenance validation failed"):
        svc.ingest_failure_classification(fc, spec=mock_spec)


def test_failure_history_zero_secrets_in_chunks_or_warnings(mock_spec: ParsedAPISpec, test_workspace: Path):
    """Security Requirement: Generated history chunks contain zero auth tokens or raw passwords."""
    svc = DocumentIngestionService(str(test_workspace))
    fc = FailureClassification(
        has_failure=True,
        primary_category=FailureCategory.AUTHENTICATION_FAILURE,
        summary="Auth failure with Bearer eyJhbGciOiJIUzI1NiJ9.leak_token_999",
        retry_guidance=RetryGuidance(policy=RetryPolicy.DO_NOT_RETRY, is_idempotent_method=True, reason="401 error"),
        evidence=SanitizedFailureEvidence(
            http_method="GET",
            path_template="/api/products",
            status_code=401,
        ),
    )
    res = svc.ingest_failure_classification(fc, spec=mock_spec, run_id="run_auth")
    chunk_text = res.chunks[0].content

    assert "leak_token_999" not in chunk_text
    assert "[REDACTED_TOKEN]" in chunk_text or "[REDACTED_JWT]" in chunk_text


# ===========================================================================
# 5. General Behavior & Limits Tests
# ===========================================================================

def test_markdown_and_plain_text_ingestion(test_workspace: Path):
    """Verifies ingesting both .md and .txt documents from approved root."""
    svc = DocumentIngestionService(str(test_workspace))
    txt_file = test_workspace / "docs" / "rfc.txt"
    txt_file.write_text("RFC 9110 HTTP Semantics overview text.", encoding="utf-8")

    res = svc.ingest_documentation_file("rfc.txt", root_name="docs")
    assert res.documents[0].metadata["doc_format"] == "text"
    assert len(res.chunks) == 1


def test_empty_and_whitespace_document_rejected(test_workspace: Path):
    """Contract Requirement: Empty or whitespace-only documents are rejected."""
    svc = DocumentIngestionService(str(test_workspace))
    empty_file = test_workspace / "docs" / "empty.md"
    empty_file.write_text("   \n\t\n   ", encoding="utf-8")

    with pytest.raises(MalformedDocumentError, match="empty or whitespace-only"):
        svc.ingest_documentation_file("empty.md", root_name="docs")


def test_chunk_size_strict_limit(test_workspace: Path):
    """Contract Requirement: No generated chunk may exceed MAX_CHUNK_CONTENT_CHARS (2048)."""
    svc = DocumentIngestionService(str(test_workspace))
    huge_doc = test_workspace / "docs" / "huge.md"
    huge_content = "# Big Document\n\n" + ("This is an extensive sentence for chunking tests. " * 300)
    huge_doc.write_text(huge_content, encoding="utf-8")

    res = svc.ingest_documentation_file("huge.md", root_name="docs")
    assert len(res.chunks) > 1
    for c in res.chunks:
        assert len(c.content) <= MAX_CHUNK_CONTENT_CHARS


def test_deterministic_ordering_and_metadata(test_workspace: Path):
    """Verifies chunk ordering is strictly monotonic and metadata conforms to allowlist."""
    svc = DocumentIngestionService(str(test_workspace))
    doc_file = test_workspace / "docs" / "ordered.md"
    doc_file.write_text(
        "# Heading\n\n"
        "## Section A\n\nContent for section A.\n\n"
        "## Section B\n\nContent for section B.\n\n"
        "## Section C\n\nContent for section C.",
        encoding="utf-8",
    )
    res = svc.ingest_documentation_file("ordered.md", root_name="docs")
    indices = [c.chunk_index for c in res.chunks]
    assert indices == [0, 1, 2]

    for c in res.chunks:
        for k in c.metadata:
            assert k in APPROVED_SOURCE_ROOTS or k in ["category", "section", "doc_format"]


# ===========================================================================
# Audit Finding Corrections (F-01, F-02, F-03, F-04)
# ===========================================================================

def test_source_slug_and_document_id_full_fingerprint(test_workspace: Path, mock_spec: ParsedAPISpec):
    """
    Audit Finding F-01:
    Verifies that compute_source_slug() and all generated document IDs incorporate
    the full 16-character source fingerprint while maintaining readability,
    content-versioning hashes, and remaining within MAX_ID_LEN (128).
    """
    svc = DocumentIngestionService(str(test_workspace))

    # 1. compute_source_slug incorporates 16-char fingerprint
    fp = compute_source_fingerprint(KnowledgeSourceType.API_DOCUMENTATION, "docs", "guides/getting_started.md")
    assert len(fp) == 16
    slug = compute_source_slug("guides/getting_started.md", fp)
    assert fp in slug
    assert slug.endswith(f"_{fp}")

    # 2. Markdown / API Documentation document ID
    doc_file = test_workspace / "docs" / "api_guide.md"
    doc_file.write_text("# API Guide\n\n## Introduction\n\nGuide content.", encoding="utf-8")
    doc_res = svc.ingest_documentation_file("api_guide.md", root_name="docs")
    doc_id = doc_res.documents[0].document_id
    doc_fp = compute_source_fingerprint(KnowledgeSourceType.API_DOCUMENTATION, "docs", "api_guide.md")
    assert doc_fp in doc_id
    assert len(doc_id) <= MAX_ID_LEN
    assert doc_id.startswith("doc-doc-")

    # 3. OpenAPI Spec document ID
    spec_path = test_workspace / "specs" / "service.json"
    spec_path.write_text(
        json.dumps({
            "openapi": "3.0.3",
            "info": {"title": "Test Service", "version": "1.2.3"},
            "paths": {
                "/status": {
                    "get": {
                        "summary": "Check status",
                        "responses": {"200": {"description": "OK"}}
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    spec_res = svc.ingest_openapi_spec("service.json", root_name="specs")
    spec_doc_id = spec_res.documents[0].document_id
    spec_fp = compute_source_fingerprint(KnowledgeSourceType.OPENAPI_SPEC, "specs", "service.json")
    assert spec_fp in spec_doc_id
    assert len(spec_doc_id) <= MAX_ID_LEN
    assert spec_doc_id.startswith("doc-spec-")

    # 4. Failure History document ID
    fc = FailureClassification(
        has_failure=True,
        run_id="run-2026-audit-test",
        primary_category=FailureCategory.SERVER_ERROR,
        confidence=0.95,
        severity=FailureSeverity.HIGH,
        summary="Audit test failure",
        evidence=SanitizedFailureEvidence(
            status_code=500,
            response_body='{"detail": "server error"}',
            http_method="GET",
            path_template="/api/products",
        ),
        retry_guidance=RetryGuidance(
            policy=RetryPolicy.DO_NOT_RETRY,
            is_idempotent_method=False,
            reason="Internal error",
        ),
    )
    hist_res = svc.ingest_failure_classification(fc, spec=mock_spec, run_id="run-2026-audit-test")
    hist_doc_id = hist_res.documents[0].document_id
    hist_fp = hashlib.sha256(b"failure_history:run:run-2026-audit-test").hexdigest()[:16]
    assert hist_fp in hist_doc_id
    assert len(hist_doc_id) <= MAX_ID_LEN
    assert hist_doc_id.startswith("doc-hist-run_")

    # 5. Chunk IDs design is independent and preserved
    for chunk in doc_res.chunks:
        assert doc_fp in chunk.chunk_id
        assert chunk.chunk_id.startswith("chk-doc-")
        assert len(chunk.chunk_id) <= MAX_ID_LEN


def test_read_secure_file_directory_as_file_rejected(test_workspace: Path):
    """
    Audit Finding F-02:
    Verifies that attempting to ingest an existing directory as a target file
    is rejected through the non-regular-file validation path, ensuring zero
    documents or chunks are produced.
    """
    sub_dir = test_workspace / "docs" / "sub_folder"
    sub_dir.mkdir()

    # Direct read_secure_file call raises SecurityPathError
    with pytest.raises(SecurityPathError, match="Target is not a regular file"):
        read_secure_file(str(test_workspace), "docs", "sub_folder")

    # Ingestion service call raises SecurityPathError and yields no documents or chunks
    svc = DocumentIngestionService(str(test_workspace))
    with pytest.raises(SecurityPathError, match="Target is not a regular file"):
        svc.ingest_documentation_file("sub_folder", root_name="docs")


def test_read_bounded_fd_file_growth_rejected(tmp_path: Path):
    """
    Audit Finding F-03:
    Verifies that _read_bounded_fd() rejects content when a file grows after its
    initial fstat() size check, raising DocumentSizeLimitError and refusing to
    return oversized content.
    """
    test_file = tmp_path / "growing_file.dat"
    test_file.write_bytes(b"Small initial content under 100 bytes.")

    read_fd = os.open(str(test_file), os.O_RDONLY)
    try:
        # Initial fstat check simulates passing size limit
        st = os.fstat(read_fd)
        assert stat.S_ISREG(st.st_mode)
        assert st.st_size <= MAX_FILE_SIZE_BYTES

        # File grows concurrently / after fstat beyond MAX_FILE_SIZE_BYTES
        with open(test_file, "ab") as f:
            f.write(b"X" * (MAX_FILE_SIZE_BYTES + 512))
            f.flush()

        # Bounded read loop must detect oversized content and reject it
        with pytest.raises(DocumentSizeLimitError, match="exceeds maximum limit"):
            _read_bounded_fd(read_fd, MAX_FILE_SIZE_BYTES)
    finally:
        os.close(read_fd)


def test_read_secure_file_descriptor_cleanup_on_traversal_failure(test_workspace: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Audit Finding F-04:
    Verifies that all file descriptors opened by read_secure_file are safely closed
    when traversal fails partway through. Tracks descriptors specifically opened
    by this operation and verifies every tracked descriptor is closed (EBADF).
    """
    # Create hierarchy: workspace/docs/dir1/dir2
    (test_workspace / "docs" / "dir1" / "dir2").mkdir(parents=True)
    # Place a regular file at dir3 so that opening it as a directory fails with ENOTDIR (Errno 20)
    (test_workspace / "docs" / "dir1" / "dir2" / "dir3").write_text("not a directory", encoding="utf-8")

    opened_fds: List[int] = []
    closed_fds: List[int] = []
    real_open = os.open
    real_close = os.close

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened_fds.append(fd)
        return fd

    def tracking_close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    import backend.app.services.document_ingestion as di
    monkeypatch.setattr(di.os, "open", tracking_open)
    monkeypatch.setattr(di.os, "close", tracking_close)
    if hasattr(di.os, "supports_dir_fd") and di.os.supports_dir_fd is not None:
        monkeypatch.setattr(di.os, "supports_dir_fd", di.os.supports_dir_fd | {tracking_open})

    # Traversal should fail at dir3 with SecurityPathError (wrapping ENOTDIR)
    with pytest.raises(SecurityPathError, match="Symlink or path structure violation"):
        read_secure_file(str(test_workspace), "docs", "dir1/dir2/dir3/final.md")

    # Verify that multiple descriptors were opened prior to failure
    assert len(opened_fds) >= 3, f"Expected at least 3 FDs opened before failure, got {len(opened_fds)}"

    # Verify that every opened descriptor was explicitly closed
    assert set(opened_fds) == set(closed_fds), "Not all opened file descriptors were closed!"

    # Verify at OS level that all tracked FDs are truly closed (EBADF)
    for fd in opened_fds:
        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF, f"Expected EBADF for fd {fd}, got {exc_info.value.errno}"


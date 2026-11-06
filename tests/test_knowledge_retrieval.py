"""
Comprehensive test suite for Phase 4B.4: Retrieval and Metadata Filtering.
Tests semantic retrieval against ChromaDB index with canonical SQLite hydration,
reader snapshot validation, commit-sequence consistency, metadata filtering,
bounded candidate expansion, input validation, and score calculation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

import pytest

from backend.app.schemas.failure import FailureCategory
from backend.app.schemas.knowledge import (
    ALLOWED_METADATA_KEYS,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from backend.app.services.knowledge_store import (
    ClosedSessionError,
    CorruptedIndexIntegrityError,
    DeterministicHashEmbeddingProvider,
    InvalidSessionError,
    LocalKnowledgeStore,
    RetrievalFilter,
    RetrievalResult,
    StoreConfig,
    compute_relevance_score,
    format_chroma_id,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> LocalKnowledgeStore:
    """Provides an isolated LocalKnowledgeStore initialized for testing."""
    config = StoreConfig(
        storage_path=tmp_path / "knowledge_store",
        skip_security_checks=True,
    )
    return LocalKnowledgeStore(config)


def _make_doc(
    doc_id: str,
    title: str = "Test Document",
    source_type: KnowledgeSourceType = KnowledgeSourceType.OPENAPI_SPEC,
    http_method: Optional[str] = "GET",
    path_template: Optional[str] = "/api/v1/resource",
    operation_id: Optional[str] = "getResource",
    metadata: Optional[Dict[str, Any]] = None,
) -> KnowledgeDocument:
    """Helper to construct a valid KnowledgeDocument."""
    return KnowledgeDocument(
        document_id=doc_id,
        source_type=source_type,
        title=title,
        source_ref=f"specs/{doc_id}.json",
        http_method=http_method,
        path_template=path_template,
        operation_id=operation_id,
        metadata=metadata or {},
    )


def _make_chunk(
    chunk_id: str,
    doc_id: str,
    content: str,
    chunk_index: int = 0,
    source_type: KnowledgeSourceType = KnowledgeSourceType.OPENAPI_SPEC,
    http_method: Optional[str] = "GET",
    path_template: Optional[str] = "/api/v1/resource",
    operation_id: Optional[str] = "getResource",
    failure_category: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> KnowledgeChunk:
    """Helper to construct a valid KnowledgeChunk."""
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=content,
        chunk_index=chunk_index,
        source_type=source_type,
        http_method=http_method,
        path_template=path_template,
        operation_id=operation_id,
        failure_category=failure_category,
        source_ref=f"specs/{doc_id}.json#section",
        metadata=metadata or {},
    )


def _ingest(
    store: LocalKnowledgeStore,
    doc: KnowledgeDocument,
    chunks: List[KnowledgeChunk],
    source_identity: Optional[str] = None,
) -> Any:
    """Helper to ingest document with auto-generated source_identity."""
    sid = source_identity or f"{doc.source_type.value}:{doc.document_id}"
    return store.ingest_document(doc, chunks, source_identity=sid)


# ---------------------------------------------------------------------------
# 1. RetrievalResult Structure & Relevance Score Tests
# ---------------------------------------------------------------------------

def test_relevance_score_computation():
    """Verify compute_relevance_score correctly normalizes cosine distance to [0.0, 1.0]."""
    # Identical vectors (distance 0.0) -> score 1.0
    assert compute_relevance_score(0.0) == 1.0
    # Orthogonal vectors (distance 1.0) -> score 0.5
    assert compute_relevance_score(1.0) == 0.5
    # Opposite vectors (distance 2.0) -> score 0.0
    assert compute_relevance_score(2.0) == 0.0
    # Clamping below 0.0
    assert compute_relevance_score(-0.1) == 1.0
    # Clamping above 2.0
    assert compute_relevance_score(2.5) == 0.0
    # Monotonicity
    s1 = compute_relevance_score(0.2)
    s2 = compute_relevance_score(0.8)
    assert s1 > s2


def test_retrieval_result_structure_and_immutability(store: LocalKnowledgeStore):
    """Verify RetrievalResult model invariants, types, and frozen immutability."""
    doc = _make_doc("doc_res_struct")
    chunk = _make_chunk("c1", "doc_res_struct", "Authentication endpoint tokens and keys")
    _ingest(store, doc, [chunk])

    results = store.retrieve("authentication tokens", top_k=1)
    assert len(results) == 1
    res = results[0]

    assert isinstance(res, RetrievalResult)
    assert isinstance(res.chunk, KnowledgeChunk)
    assert isinstance(res.score, float)
    assert 0.0 <= res.score <= 1.0
    assert isinstance(res.distance, float)
    assert res.distance >= 0.0
    assert res.chunk.chunk_id == "c1"

    # Frozen model: mutations should raise an error
    with pytest.raises(Exception):
        res.score = 0.99  # type: ignore


# ---------------------------------------------------------------------------
# 2. Top-k Bounding Tests
# ---------------------------------------------------------------------------

def test_top_k_bounding(store: LocalKnowledgeStore):
    """Verify top_k bounds the returned result count properly."""
    doc = _make_doc("doc_top_k")
    chunks = [
        _make_chunk(f"chk_{i}", "doc_top_k", f"Database connection pool pool_size={i * 10} setting")
        for i in range(10)
    ]
    _ingest(store, doc, chunks)

    # top_k = 1
    res1 = store.retrieve("database connection pool", top_k=1)
    assert len(res1) == 1

    # top_k = 3
    res3 = store.retrieve("database connection pool", top_k=3)
    assert len(res3) == 3

    # top_k = 10
    res10 = store.retrieve("database connection pool", top_k=10)
    assert len(res10) == 10

    # top_k greater than available chunks
    res20 = store.retrieve("database connection pool", top_k=20)
    assert len(res20) == 10


# ---------------------------------------------------------------------------
# 3. Canonical SQLite Hydration & Provenance
# ---------------------------------------------------------------------------

def test_canonical_sqlite_hydration_provenance(store: LocalKnowledgeStore):
    """
    Verify chunk content and all rich metadata are hydrated directly from SQLite,
    never from Chroma, preserving canonical truth.
    """
    doc = _make_doc("doc_hydra", path_template="/api/v1/payments", operation_id="createPayment")
    chunk = _make_chunk(
        "c_hydra",
        "doc_hydra",
        content="Payment processing gateway transaction endpoints",
        http_method="POST",
        path_template="/api/v1/payments",
        operation_id="createPayment",
        failure_category="GATEWAY_TIMEOUT",
        metadata={"tag": "billing", "status_code": 201, "is_deprecated": False},
    )
    _ingest(store, doc, [chunk])

    results = store.retrieve("payment gateway transaction", top_k=1)
    assert len(results) == 1
    r_chunk = results[0].chunk

    assert r_chunk.content == "Payment processing gateway transaction endpoints"
    assert r_chunk.http_method == "POST"
    assert r_chunk.path_template == "/api/v1/payments"
    assert r_chunk.operation_id == "createPayment"
    assert r_chunk.failure_category == "GATEWAY_TIMEOUT"
    assert r_chunk.metadata["tag"] == "billing"
    assert r_chunk.metadata["status_code"] == 201
    assert r_chunk.metadata["is_deprecated"] is False


def test_corrupted_index_integrity_error(store: LocalKnowledgeStore):
    """Verify CorruptedIndexIntegrityError is raised if Chroma returns a chunk missing from SQLite."""
    doc = _make_doc("doc_corrupt")
    chunk = _make_chunk("c_real", "doc_corrupt", "Real document chunk content")
    _ingest(store, doc, [chunk])

    # Inject a phantom chunk ID directly into ChromaDB
    col = store.chroma_client.get_collection("chunks_gen_1")
    phantom_storage_id = format_chroma_id("doc_corrupt", "c_phantom_nonexistent")
    col.add(
        ids=[phantom_storage_id],
        embeddings=[[0.1] * 384],
        metadatas=[{
            "document_id": "doc_corrupt",
            "chunk_id": "c_phantom_nonexistent",
            "chunk_index": 99,
            "source_identity": "specs/doc_corrupt.json",
            "source_type": "openapi_spec",
            "generation": 1,
        }],
    )

    with pytest.raises(CorruptedIndexIntegrityError) as exc_info:
        store.retrieve("Real document chunk content", top_k=5)
    assert "missing from canonical storage" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Metadata Filtering Tests (Single Fields)
# ---------------------------------------------------------------------------

def test_filter_by_source_type(store: LocalKnowledgeStore):
    """Verify filtering by source_type via RetrievalFilter, dict, and keyword argument."""
    d1 = _make_doc("d_spec", source_type=KnowledgeSourceType.OPENAPI_SPEC)
    c1 = _make_chunk("c_spec", "d_spec", "API spec endpoint user profiles", source_type=KnowledgeSourceType.OPENAPI_SPEC)
    _ingest(store, d1, [c1])

    d2 = _make_doc("d_doc", source_type=KnowledgeSourceType.API_DOCUMENTATION)
    c2 = _make_chunk("c_doc", "d_doc", "API doc guide user profiles tutorial", source_type=KnowledgeSourceType.API_DOCUMENTATION)
    _ingest(store, d2, [c2])

    d3 = _make_doc("d_fail", source_type=KnowledgeSourceType.FAILURE_HISTORY)
    c3 = _make_chunk("c_fail", "d_fail", "Failure incident user profiles 500 error", source_type=KnowledgeSourceType.FAILURE_HISTORY)
    _ingest(store, d3, [c3])

    # 1. Via source_type argument
    res1 = store.retrieve("user profiles", source_type=KnowledgeSourceType.OPENAPI_SPEC)
    assert len(res1) == 1
    assert res1[0].chunk.source_type == KnowledgeSourceType.OPENAPI_SPEC

    # 2. Via filter dict
    res2 = store.retrieve("user profiles", filter={"source_type": "api_documentation"})
    assert len(res2) == 1
    assert res2[0].chunk.source_type == KnowledgeSourceType.API_DOCUMENTATION

    # 3. Via RetrievalFilter object
    res3 = store.retrieve("user profiles", filter=RetrievalFilter(source_type=KnowledgeSourceType.FAILURE_HISTORY))
    assert len(res3) == 1
    assert res3[0].chunk.source_type == KnowledgeSourceType.FAILURE_HISTORY


def test_filter_by_http_method(store: LocalKnowledgeStore):
    """Verify filtering by HTTP method (case-insensitive normalization)."""
    doc = _make_doc("doc_http")
    c_get = _make_chunk("c_get", "doc_http", "Orders listing query resource", http_method="GET")
    c_post = _make_chunk("c_post", "doc_http", "Orders creation post resource", http_method="POST")
    c_del = _make_chunk("c_del", "doc_http", "Orders cancellation delete resource", http_method="DELETE")
    _ingest(store, doc, [c_get, c_post, c_del])

    # Case-insensitive "post"
    res_post = store.retrieve("Orders resource", filter={"http_method": "post"})
    assert len(res_post) == 1
    assert res_post[0].chunk.http_method == "POST"

    res_del = store.retrieve("Orders resource", filter={"http_method": "DELETE"})
    assert len(res_del) == 1
    assert res_del[0].chunk.http_method == "DELETE"


def test_filter_by_path_and_path_template(store: LocalKnowledgeStore):
    """Verify filtering by path_template and its alias 'path'."""
    doc = _make_doc("doc_paths")
    c_users = _make_chunk("c_users", "doc_paths", "User details endpoint resource", path_template="/api/v1/users")
    c_items = _make_chunk("c_items", "doc_paths", "Item catalog endpoint resource", path_template="/api/v1/items")
    _ingest(store, doc, [c_users, c_items])

    # Using 'path' alias
    res_alias = store.retrieve("endpoint resource", filter={"path": "/api/v1/users"})
    assert len(res_alias) == 1
    assert res_alias[0].chunk.path_template == "/api/v1/users"

    # Using 'path_template'
    res_exact = store.retrieve("endpoint resource", filter={"path_template": "/api/v1/items"})
    assert len(res_exact) == 1
    assert res_exact[0].chunk.path_template == "/api/v1/items"


def test_filter_by_operation_id(store: LocalKnowledgeStore):
    """Verify filtering by operation_id."""
    doc = _make_doc("doc_ops")
    c1 = _make_chunk("c_op1", "doc_ops", "Account balance retrieval query", operation_id="getBalance")
    c2 = _make_chunk("c_op2", "doc_ops", "Account statement PDF export", operation_id="exportStatement")
    _ingest(store, doc, [c1, c2])

    res = store.retrieve("Account operations", filter={"operation_id": "getBalance"})
    assert len(res) == 1
    assert res[0].chunk.operation_id == "getBalance"


def test_filter_by_failure_category(store: LocalKnowledgeStore):
    """Verify filtering by failure_category using enum or string."""
    doc = _make_doc("doc_fails", source_type=KnowledgeSourceType.FAILURE_HISTORY)
    c1 = _make_chunk(
        "c_f1",
        "doc_fails",
        "Token expired validation failure log",
        source_type=KnowledgeSourceType.FAILURE_HISTORY,
        failure_category="UNAUTHORIZED",
    )
    c2 = _make_chunk(
        "c_f2",
        "doc_fails",
        "Database pool exhausted resource error log",
        source_type=KnowledgeSourceType.FAILURE_HISTORY,
        failure_category="DATABASE_ERROR",
    )
    _ingest(store, doc, [c1, c2])

    res = store.retrieve("failure log", filter={"failure_category": "UNAUTHORIZED"})
    assert len(res) == 1
    assert res[0].chunk.failure_category == "UNAUTHORIZED"


def test_filter_by_allowed_metadata_keys(store: LocalKnowledgeStore):
    """Verify filtering by Phase 4B.1 allowed metadata keys (status_code, tag, is_deprecated)."""
    doc = _make_doc("doc_meta")
    c1 = _make_chunk(
        "c_m1",
        "doc_meta",
        "Customer authentication service response",
        metadata={"tag": "auth", "status_code": 200, "is_deprecated": False},
    )
    c2 = _make_chunk(
        "c_m2",
        "doc_meta",
        "Customer authorization denied service response",
        metadata={"tag": "auth", "status_code": 403, "is_deprecated": False},
    )
    c3 = _make_chunk(
        "c_m3",
        "doc_meta",
        "Customer legacy session service response",
        metadata={"tag": "legacy", "status_code": 200, "is_deprecated": True},
    )
    _ingest(store, doc, [c1, c2, c3])

    # Filter by top-level tag
    res_tag = store.retrieve("Customer service response", filter={"tag": "auth"})
    assert len(res_tag) == 2
    assert all(r.chunk.metadata.get("tag") == "auth" for r in res_tag)

    # Filter by status_code
    res_status = store.retrieve("Customer service response", filter={"status_code": 403})
    assert len(res_status) == 1
    assert res_status[0].chunk.chunk_id == "c_m2"

    # Filter by is_deprecated
    res_dep = store.retrieve("Customer service response", filter={"is_deprecated": True})
    assert len(res_dep) == 1
    assert res_dep[0].chunk.chunk_id == "c_m3"


# ---------------------------------------------------------------------------
# 5. Composite / Multi-field Filtering
# ---------------------------------------------------------------------------

def test_composite_conjunction_filtering(store: LocalKnowledgeStore):
    """Verify that multiple filter dimensions are evaluated with AND logic."""
    doc = _make_doc("doc_comp")
    c1 = _make_chunk(
        "c_post_201",
        "doc_comp",
        "Orders checkout processing endpoint",
        http_method="POST",
        path_template="/api/v1/orders",
        operation_id="createOrder",
        metadata={"status_code": 201, "tag": "checkout"},
    )
    c2 = _make_chunk(
        "c_get_200",
        "doc_comp",
        "Orders details retrieval endpoint",
        http_method="GET",
        path_template="/api/v1/orders",
        operation_id="getOrder",
        metadata={"status_code": 200, "tag": "checkout"},
    )
    c3 = _make_chunk(
        "c_post_400",
        "doc_comp",
        "Orders checkout invalid payload response",
        http_method="POST",
        path_template="/api/v1/orders",
        operation_id="createOrder",
        metadata={"status_code": 400, "tag": "checkout"},
    )
    _ingest(store, doc, [c1, c2, c3])

    # All criteria match only c1
    res = store.retrieve(
        "Orders checkout endpoint",
        filter={
            "http_method": "POST",
            "path": "/api/v1/orders",
            "operation_id": "createOrder",
            "status_code": 201,
            "tag": "checkout",
        },
    )
    assert len(res) == 1
    assert res[0].chunk.chunk_id == "c_post_201"


# ---------------------------------------------------------------------------
# 6. Bounded Candidate Expansion Tests
# ---------------------------------------------------------------------------

def test_bounded_candidate_expansion(store: LocalKnowledgeStore):
    """
    Critical Test:
    When earlier Chroma candidates do not match SQLite filters,
    verify the retrieval engine expands the candidate batch and retrieves
    matching chunks deeper in the index without silently returning fewer results.
    """
    doc = _make_doc("doc_expansion")

    # Ingest 15 GET chunks with heavy keyword overlap with "inventory report"
    get_chunks = [
        _make_chunk(
            f"chk_get_{i}",
            "doc_expansion",
            f"inventory report daily summary overview chunk {i}",
            chunk_index=i,
            http_method="GET",
            path_template="/api/v1/inventory",
        )
        for i in range(15)
    ]

    # Ingest 3 POST chunks with moderate keyword overlap
    post_chunks = [
        _make_chunk(
            f"chk_post_{i}",
            "doc_expansion",
            f"inventory update item record post chunk {i}",
            chunk_index=15 + i,
            http_method="POST",
            path_template="/api/v1/inventory",
        )
        for i in range(3)
    ]

    _ingest(store, doc, get_chunks + post_chunks)

    # Query for POST chunks with query text closer to the GET chunks.
    # Without candidate expansion, Chroma returning only top_k=2 candidates
    # would return only GET chunks, yielding 0 results.
    results = store.retrieve(
        "inventory report update",
        top_k=2,
        filter={"http_method": "POST"},
    )

    assert len(results) == 2
    for r in results:
        assert r.chunk.http_method == "POST"
        assert r.chunk.chunk_id.startswith("chk_post_")


def test_no_match_filter_returns_empty(store: LocalKnowledgeStore):
    """Verify that a filter matching no canonical records returns [] cleanly."""
    doc = _make_doc("doc_nomatch")
    chunk = _make_chunk("c_nm", "doc_nomatch", "General documentation about settings")
    _ingest(store, doc, [chunk])

    res = store.retrieve("General documentation", filter={"path": "/nonexistent/endpoint"})
    assert res == []


# ---------------------------------------------------------------------------
# 7. Snapshot Isolation & Reader Protection Tests
# ---------------------------------------------------------------------------

def test_snapshot_active_set_isolation(store: LocalKnowledgeStore):
    """
    Verify snapshot isolation: documents ingested AFTER a reader session
    acquires its snapshot are strictly invisible to that reader session.
    """
    doc1 = _make_doc("doc_v1")
    c1 = _make_chunk("c_v1", "doc_v1", "Initial version of knowledge document")
    _ingest(store, doc1, [c1])

    # Acquire reader session S1 pinned at doc1
    session1 = store.acquire_query_session()
    try:
        # Ingest doc2 while session1 is open
        doc2 = _make_doc("doc_v2")
        c2 = _make_chunk("c_v2", "doc_v2", "Second version of knowledge document")
        _ingest(store, doc2, [c2])

        # Query via session1: MUST ONLY see doc1
        res_s1 = store.retrieve("knowledge document", session=session1)
        doc_ids_s1 = {r.chunk.document_id for r in res_s1}
        assert doc_ids_s1 == {"doc_v1"}
        assert "doc_v2" not in doc_ids_s1

        # Query via a fresh session: sees BOTH doc1 and doc2
        res_fresh = store.retrieve("knowledge document")
        doc_ids_fresh = {r.chunk.document_id for r in res_fresh}
        assert doc_ids_fresh == {"doc_v1", "doc_v2"}
    finally:
        store.release_query_session(session1)


def test_tombstoned_deleted_document_exclusion(store: LocalKnowledgeStore):
    """
    Verify that deleted / tombstoned documents are strictly excluded from retrieval,
    ensuring zero exposure of inactive data.
    """
    doc1 = _make_doc("doc_to_delete")
    c1 = _make_chunk("c_del1", "doc_to_delete", "Confidential secrets and deprecated keys")
    doc2 = _make_doc("doc_to_keep")
    c2 = _make_chunk("c_keep", "doc_to_keep", "Public documentation and guidelines")
    _ingest(store, doc1, [c1])
    _ingest(store, doc2, [c2])

    # Delete doc1
    deleted = store.delete_document("doc_to_delete")
    assert deleted is True

    # Query for doc1's content: must return 0 results matching doc_to_delete
    results = store.retrieve("Confidential secrets and deprecated keys")
    doc_ids = {r.chunk.document_id for r in results}
    assert "doc_to_delete" not in doc_ids


def test_generation_isolation_during_migration(store: LocalKnowledgeStore):
    """
    Verify reader sessions pinned to an older generation continue to retrieve
    from that generation during and after index rebuilds.
    """
    doc = _make_doc("doc_gen_iso")
    chunk = _make_chunk("c_gen1", "doc_gen_iso", "Generation one search content")
    _ingest(store, doc, [chunk])

    # Open session pinned to Gen 1
    session_gen1 = store.acquire_query_session()
    assert session_gen1.generation == 1
    try:
        # Rebuild index to transition to Gen 2
        mig_res = store.rebuild_index()
        assert mig_res.to_generation == 2

        # Pinned session_gen1 must still retrieve from Gen 1 successfully
        res1 = store.retrieve("Generation one search content", session=session_gen1)
        assert len(res1) == 1
        assert res1[0].chunk.chunk_id == "c_gen1"

        # New query session retrieves from Gen 2
        with store.acquire_query_session() as session_gen2:
            assert session_gen2.generation == 2
            res2 = store.retrieve("Generation one search content", session=session_gen2)
            assert len(res2) == 1
            assert res2[0].chunk.chunk_id == "c_gen1"
    finally:
        store.release_query_session(session_gen1)


# ---------------------------------------------------------------------------
# 8. Empty Store, Non-existent Collections, and Session Lifecycle
# ---------------------------------------------------------------------------

def test_empty_store_and_reset_store(store: LocalKnowledgeStore):
    """Verify clean empty returns on unpopulated or reset store."""
    # Empty store returns []
    assert store.retrieve("any query") == []

    # Ingest then reset
    doc = _make_doc("doc_rst")
    chunk = _make_chunk("c_rst", "doc_rst", "Content before store reset")
    _ingest(store, doc, [chunk])
    assert len(store.retrieve("Content before reset")) == 1

    store.reset_store()
    assert store.retrieve("Content before reset") == []


def test_session_closed_and_foreign_validation(store: LocalKnowledgeStore, tmp_path: Path):
    """Verify ClosedSessionError and InvalidSessionError enforcement."""
    session = store.acquire_query_session()
    session.close()

    # Closed session raises ClosedSessionError
    with pytest.raises(ClosedSessionError):
        store.retrieve("test query", session=session)

    with pytest.raises(ClosedSessionError):
        session.retrieve("test query")

    # Foreign session from a different store instance
    other_config = StoreConfig(
        storage_path=tmp_path / "other_store",
        skip_security_checks=True,
    )
    other_store = LocalKnowledgeStore(other_config)

    other_session = other_store.acquire_query_session()
    try:
        with pytest.raises(InvalidSessionError):
            store.retrieve("test query", session=other_session)
    finally:
        other_store.release_query_session(other_session)


def test_session_context_manager(store: LocalKnowledgeStore):
    """Verify QueryReaderSession context manager acquires and cleanly releases pin."""
    doc = _make_doc("doc_ctx")
    chunk = _make_chunk("c_ctx", "doc_ctx", "Context manager test chunk")
    _ingest(store, doc, [chunk])

    with store.acquire_query_session() as session:
        assert session.is_active is True
        results = session.retrieve("Context manager test chunk")
        assert len(results) == 1

    # Session is closed upon exiting the with block
    assert session.is_active is False
    with pytest.raises(ClosedSessionError):
        session.retrieve("Context manager test chunk")


# ---------------------------------------------------------------------------
# 9. Input Validation & Error Handling
# ---------------------------------------------------------------------------

def test_query_text_validation_errors(store: LocalKnowledgeStore):
    """Verify invalid query_text inputs raise ValueError."""
    # Non-string
    with pytest.raises(ValueError, match="query_text must be a string"):
        store.retrieve(12345)  # type: ignore

    # Empty string
    with pytest.raises(ValueError, match="query_text cannot be empty"):
        store.retrieve("")

    # Whitespace-only string
    with pytest.raises(ValueError, match="query_text cannot be empty"):
        store.retrieve("   \n\t  ")

    # Exceeding 16384 characters
    with pytest.raises(ValueError, match="exceeds maximum length"):
        store.retrieve("a" * 16385)


def test_top_k_validation_errors(store: LocalKnowledgeStore):
    """Verify invalid top_k inputs raise ValueError."""
    # Boolean
    with pytest.raises(ValueError, match="top_k must be an integer"):
        store.retrieve("valid query", top_k=True)  # type: ignore

    # Float
    with pytest.raises(ValueError, match="top_k must be an integer"):
        store.retrieve("valid query", top_k=5.5)  # type: ignore

    # Zero
    with pytest.raises(ValueError, match="top_k must be between 1 and 1000"):
        store.retrieve("valid query", top_k=0)

    # Negative
    with pytest.raises(ValueError, match="top_k must be between 1 and 1000"):
        store.retrieve("valid query", top_k=-5)

    # Greater than 1000
    with pytest.raises(ValueError, match="top_k must be between 1 and 1000"):
        store.retrieve("valid query", top_k=1001)


def test_filter_validation_errors(store: LocalKnowledgeStore):
    """Verify invalid filter keys and malformed values raise ValueError."""
    # Invalid HTTP method
    with pytest.raises(ValueError, match="Invalid HTTP method"):
        store.retrieve("query", filter={"http_method": "INVALID_METHOD"})

    # Invalid path template (missing leading slash)
    with pytest.raises(ValueError, match="must begin with '/'"):
        store.retrieve("query", filter={"path": "api/no_leading_slash"})

    # Path template with query params
    with pytest.raises(ValueError, match="cannot contain query parameters"):
        store.retrieve("query", filter={"path": "/api/users?id=123"})

    # Unrecognized / forbidden filter key
    with pytest.raises(ValueError, match="Unrecognized or forbidden filter field"):
        store.retrieve("query", filter={"unauthorized_filter_key": "val"})

    # Invalid metadata status_code out of 100-599 range
    with pytest.raises(ValueError, match="must be between 100 and 599"):
        store.retrieve("query", filter={"status_code": 999})

    # Conflicting source_type in args vs filter
    with pytest.raises(ValueError, match="Conflicting source_type"):
        store.retrieve(
            "query",
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            filter={"source_type": "api_documentation"},
        )


# ---------------------------------------------------------------------------
# 10. Backward Compatibility & query_chunks Delegation
# ---------------------------------------------------------------------------

def test_query_chunks_backward_compatibility(store: LocalKnowledgeStore):
    """
    Verify query_chunks maintains 100% backward compatibility:
    - returns List[KnowledgeChunk] by default (return_scores=False)
    - returns List[RetrievalResult] when return_scores=True
    - supports QueryReaderSession helper methods
    """
    doc = _make_doc("doc_compat")
    chunk = _make_chunk("c_compat", "doc_compat", "Backward compatibility regression testing")
    _ingest(store, doc, [chunk])

    # Default return: List[KnowledgeChunk]
    chunks = store.query_chunks("regression testing")
    assert isinstance(chunks, list)
    assert len(chunks) == 1
    assert isinstance(chunks[0], KnowledgeChunk)
    assert chunks[0].chunk_id == "c_compat"

    # return_scores=True: List[RetrievalResult]
    results = store.query_chunks("regression testing", return_scores=True)
    assert isinstance(results, list)
    assert len(results) == 1
    assert isinstance(results[0], RetrievalResult)
    assert results[0].chunk.chunk_id == "c_compat"
    assert results[0].score > 0.0

    # Pinned session helper query_chunks
    with store.acquire_query_session() as s:
        s_chunks = s.query_chunks("regression testing")
        assert isinstance(s_chunks, list)
        assert len(s_chunks) == 1
        assert isinstance(s_chunks[0], KnowledgeChunk)

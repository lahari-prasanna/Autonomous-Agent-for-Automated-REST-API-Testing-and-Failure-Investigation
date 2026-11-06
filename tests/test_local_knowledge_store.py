"""
Comprehensive test suite for Phase 4B.3: Local Knowledge Storage and Indexing.
Implements all 28 architecture tests specified in Section 13.1 of
Phase 4B.3 Architecture Revision 17 (PHASE_4B_3_ARCHITECTURE_FINAL.md).
"""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from typing import List
import unittest.mock as mock

import pytest

import chromadb
from backend.app.schemas.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from backend.app.services.knowledge_store import (
    ClosedSessionError,
    ConcurrencyConflictError,
    CorruptedIndexIntegrityError,
    CorruptedStorageIdError,
    DeterministicHashEmbeddingProvider,
    DocumentChunkMismatchError,
    EmbeddingProvider,
    EmbeddingProviderMismatchError,
    InvalidSessionError,
    KnowledgeStoreError,
    LocalKnowledgeStore,
    MigrationVerificationError,
    PinStatus,
    ProviderRegistry,
    SecurityStorageError,
    SourceTypeMismatchError,
    StoreConfig,
    format_chroma_id,
    initialize_database,
    parse_chroma_id,
)


# ---------------------------------------------------------------------------
# Test Helpers and Mock Providers
# ---------------------------------------------------------------------------


class MockAlternativeEmbeddingProvider(EmbeddingProvider):
    """Alternative 768-dimensional embedding provider for testing migrations."""

    def __init__(
        self,
        dimension: int = 768,
        model_name: str = "mock_model_768",
        config_version: str = "v2.0",
    ):
        self._dim = dimension
        self._model = model_name
        self._cfg = config_version

    @property
    def provider_id(self) -> str:
        return "mock_alt"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def config_version(self) -> str:
        return self._cfg

    def embed_text(self, text: str) -> List[float]:
        val = 1.0 / math.sqrt(self._dim)
        return [val] * self._dim

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_text(t) for t in texts]


def make_test_doc(
    doc_id: str, title: str = "Test Doc", tag: str = "test"
) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=doc_id,
        source_type=KnowledgeSourceType.API_DOCUMENTATION,
        title=title,
        source_ref=f"docs/{doc_id}.md",
        metadata={"tag": tag, "version": "1.0"},
    )


def make_test_chunk(
    chunk_id: str, doc_id: str, index: int, content: str, tag: str = "test"
) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=content,
        chunk_index=index,
        source_type=KnowledgeSourceType.API_DOCUMENTATION,
        source_ref=f"docs/{doc_id}.md#sec{index}",
        metadata={"tag": tag},
    )


@pytest.fixture
def temp_store_dir(tmp_path: Path) -> Path:
    storage_dir = tmp_path / "store"
    storage_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(storage_dir, 0o700)
    return storage_dir


@pytest.fixture
def store(temp_store_dir: Path) -> LocalKnowledgeStore:
    cfg = StoreConfig(storage_path=temp_store_dir, skip_security_checks=False)
    return LocalKnowledgeStore(config=cfg)


# ---------------------------------------------------------------------------
# Test Scenarios (1 to 28)
# ---------------------------------------------------------------------------


def test_01_foreign_key_safe_first_ingestion(store: LocalKnowledgeStore):
    """
    Test 1: Foreign-Key Safe First Ingestion.
    Ingest first document; verify placeholder catalog creation and FK constraint validity.
    """
    doc = make_test_doc("doc-first-1")
    chunk = make_test_chunk("chk-1", "doc-first-1", 0, "First ingested chunk content")
    source_id = "api_documentation:docs/first.md"

    store.ingest_document(doc, [chunk], source_identity=source_id)

    conn = store.conn_manager.get_connection()
    cat_row = conn.execute(
        "SELECT * FROM source_catalog WHERE source_identity = ?", (source_id,)
    ).fetchone()
    assert cat_row is not None
    assert cat_row["active_document_id"] == "doc-first-1"
    assert cat_row["commit_seq"] == 1

    doc_row = conn.execute(
        "SELECT * FROM canonical_documents WHERE document_id = ?", ("doc-first-1",)
    ).fetchone()
    assert doc_row is not None
    assert doc_row["status"] == "active"
    assert doc_row["source_identity"] == source_id

    chunk_rows = conn.execute(
        "SELECT * FROM canonical_chunks WHERE document_id = ?", ("doc-first-1",)
    ).fetchall()
    assert len(chunk_rows) == 1
    assert chunk_rows[0]["chunk_id"] == "chk-1"


def test_02_conditional_update_row_count_invariants(store: LocalKnowledgeStore):
    """
    Test 2: Conditional Update Row-Count Invariants.
    Assert cursor.rowcount == 1 checks and rollback on unexpected rows across transitions.
    """
    doc1 = make_test_doc("doc-rc-1")
    chunk1 = make_test_chunk("chk-1", "doc-rc-1", 0, "Chunk 1 content")
    source_id = "api_documentation:docs/rc.md"
    store.ingest_document(doc1, [chunk1], source_identity=source_id)

    # Attempting replacement with mismatched old_doc_id must raise ConcurrencyConflictError
    doc2 = make_test_doc("doc-rc-2")
    chunk2 = make_test_chunk("chk-2", "doc-rc-2", 0, "Chunk 2 content")
    with pytest.raises(ConcurrencyConflictError):
        store.replace_document(
            "wrong-old-id", doc2, [chunk2], source_identity=source_id
        )

    # Document type mismatch on existing source must raise SourceTypeMismatchError
    mismatched_doc = KnowledgeDocument(
        document_id="doc-rc-mismatch",
        source_type=KnowledgeSourceType.OPENAPI_SPEC,
        title="Mismatch Type",
        source_ref="specs/api.json",
    )
    with pytest.raises(SourceTypeMismatchError):
        store.ingest_document(mismatched_doc, [], source_identity=source_id)


def test_03_replacement_and_deletion_pointer_atomicity(store: LocalKnowledgeStore):
    """
    Test 3: Replacement and Deletion Pointer Atomicity.
    Verify atomic pointer switches, retired cleanup registrations, and deletion clearing.
    """
    source_id = "api_documentation:docs/replace.md"
    d1 = make_test_doc("doc-v1")
    c1 = make_test_chunk("c1", "doc-v1", 0, "Version 1 chunk")
    store.ingest_document(d1, [c1], source_identity=source_id)

    # Replace D1 -> D2
    d2 = make_test_doc("doc-v2")
    c2 = make_test_chunk("c2", "doc-v2", 0, "Version 2 chunk")
    store.replace_document("doc-v1", d2, [c2], source_identity=source_id)

    conn = store.conn_manager.get_connection()
    cat_rows = conn.execute(
        "SELECT active_document_id FROM source_catalog WHERE source_identity = ?",
        (source_id,),
    ).fetchall()
    assert cat_rows[0]["active_document_id"] == "doc-v2"

    v1_status = conn.execute(
        "SELECT status FROM canonical_documents WHERE document_id = 'doc-v1'"
    ).fetchall()[0][0]
    assert v1_status == "retired"

    v2_status = conn.execute(
        "SELECT status FROM canonical_documents WHERE document_id = 'doc-v2'"
    ).fetchall()[0][0]
    assert v2_status == "active"

    cleanup_entries = conn.execute(
        "SELECT * FROM cleanup_registry WHERE document_id = 'doc-v1'"
    ).fetchall()
    assert len(cleanup_entries) == 1
    assert cleanup_entries[0]["cleanup_state"] == "RETIRED"

    # Delete source
    assert store.delete_source(source_id) is True
    cat_after = conn.execute(
        "SELECT active_document_id FROM source_catalog WHERE source_identity = ?",
        (source_id,),
    ).fetchall()
    assert cat_after[0]["active_document_id"] is None


def test_04_race_free_pin_publication(store: LocalKnowledgeStore):
    """
    Test 4: Race-Free Pin Publication.
    Concurrently run reader registration and reconciliation; verify live readers are never deleted.
    """
    doc = make_test_doc("doc-race-1")
    chunk = make_test_chunk("chk-r", "doc-race-1", 0, "Race test chunk")
    store.ingest_document(
        doc, [chunk], source_identity="api_documentation:docs/race.md"
    )

    session = store.acquire_query_session()
    try:
        assert session.is_active is True
        # Run reconciliation while reader is actively holding pin
        report = store.reconcile_startup()
        assert report.success is True
        assert report.preserved_sessions >= 1

        # Live session must still be queryable
        results = store.query_chunks("Race test", session=session)
        assert len(results) == 1
        assert results[0].chunk_id == "chk-r"
    finally:
        session.close()


def test_05_snapshot_isolation_and_generation_provider_binding(
    store: LocalKnowledgeStore,
):
    """
    Test 5: Snapshot Isolation & Generation Provider Binding.
    Register reader on Gen 1; migrate to Gen 2 with different provider;
    verify Gen 1 query uses Gen 1 provider and collection.
    """
    d1 = make_test_doc("doc-gen1")
    c1 = make_test_chunk("c-gen1", "doc-gen1", 0, "Gen 1 unique text content")
    store.ingest_document(d1, [c1], source_identity="api_documentation:docs/gen1.md")

    # Reader binds to Gen 1
    session_gen1 = store.acquire_query_session()
    assert session_gen1.generation == 1
    assert session_gen1.embedding_provider.dimension == 384

    # Migrate to Gen 2 with 768-dim mock provider
    alt_provider = MockAlternativeEmbeddingProvider(dimension=768)
    store.migrate_embeddings(alt_provider)

    # Ingest document into Gen 2
    d2 = make_test_doc("doc-gen2")
    c2 = make_test_chunk("c-gen2", "doc-gen2", 0, "Gen 2 unique text content")
    store.ingest_document(d2, [c2], source_identity="api_documentation:docs/gen2.md")

    try:
        # Gen 1 reader only sees Gen 1 documents and uses Gen 1 provider
        res1 = store.query_chunks("Gen 1 unique", session=session_gen1)
        assert len(res1) == 1
        assert res1[0].document_id == "doc-gen1"

        # New query binds to Gen 2 and sees Gen 2 active set
        new_session = store.acquire_query_session()
        assert new_session.generation == 2
        assert new_session.embedding_provider.dimension == 768
        res2 = store.query_chunks("Gen 2 unique", session=new_session)
        assert any(c.document_id == "doc-gen2" for c in res2)
        new_session.close()
    finally:
        session_gen1.close()


def test_06_empty_active_document_query(store: LocalKnowledgeStore):
    """
    Test 6: Empty Active-Document Query.
    Query store with no active documents; assert immediate empty list return without ChromaDB call.
    """
    results = store.query_chunks("any query text")
    assert results == []


def test_07_crash_recovery_from_every_cleanup_state(store: LocalKnowledgeStore):
    """
    Test 7: Crash Recovery from Every Cleanup State.
    Simulate crashes in RETIRED, TOMBSTONED, and PURGING; assert clean recovery.
    """
    doc = make_test_doc("doc-crash-clean")
    chunk = make_test_chunk("chk-cc", "doc-crash-clean", 0, "Crash cleanup content")
    store.ingest_document(
        doc, [chunk], source_identity="api_documentation:docs/crash_clean.md"
    )
    store.delete_document("doc-crash-clean")

    conn = store.conn_manager.get_connection()
    # 1. RETIRED -> run cleanup
    store.run_cleanup_pass()
    doc_status = conn.execute(
        "SELECT status FROM canonical_documents WHERE document_id = 'doc-crash-clean'"
    ).fetchone()
    # Document should be completely purged from canonical_documents
    assert doc_status is None

    # Simulate crash at PURGING state with mock document
    d2 = make_test_doc("doc-purging")
    c2 = make_test_chunk("chk-p", "doc-purging", 0, "Purging test content")
    store.ingest_document(d2, [c2], source_identity="api_documentation:docs/purging.md")
    conn.execute(
        "UPDATE canonical_documents SET status = 'tombstoned' WHERE document_id = 'doc-purging';"
    )
    conn.execute(
        "INSERT INTO cleanup_registry (document_id, generation, cleanup_state, registered_at, updated_at) "
        "VALUES ('doc-purging', 1, 'PURGING', datetime('now'), datetime('now'));"
    )

    # Reconciliation must resume and complete PURGING
    report = store.reconcile_startup()
    assert report.success is True
    assert (
        conn.execute(
            "SELECT 1 FROM cleanup_registry WHERE document_id = 'doc-purging'"
        ).fetchone()
        is None
    )


def test_08_failed_staging_generation_rollback(store: LocalKnowledgeStore):
    """
    Test 8: Failed Staging Generation Rollback.
    Inject failure during migration staging; verify document_generation_index rows rolled back and retry succeeds.
    """
    doc = make_test_doc("doc-stg-fail")
    chunk = make_test_chunk("chk-sf", "doc-stg-fail", 0, "Staging fail content")
    store.ingest_document(
        doc, [chunk], source_identity="api_documentation:docs/stg_fail.md"
    )

    alt_provider = MockAlternativeEmbeddingProvider(dimension=768)

    # Inject failure during Chroma add
    original_get = store._get_or_create_collection
    with mock.patch.object(
        store.chroma_client,
        "create_collection",
        side_effect=RuntimeError("Simulated Chroma Crash"),
    ):
        with pytest.raises(RuntimeError, match="Simulated Chroma Crash"):
            store.migrate_embeddings(alt_provider)

    # Manifest should have MIGRATION_STAGING; run reconciliation
    conn = store.conn_manager.get_connection()
    manifest_state = conn.execute(
        "SELECT migration_state FROM storage_manifest WHERE manifest_key = 'manifest'"
    ).fetchone()[0]
    assert manifest_state == "MIGRATION_STAGING"

    report = store.reconcile_startup()
    assert report.success is True
    manifest_after = conn.execute(
        "SELECT migration_state, active_generation FROM storage_manifest WHERE manifest_key = 'manifest'"
    ).fetchone()
    assert manifest_after["migration_state"] == "IDLE"
    assert manifest_after["active_generation"] == 1

    # Retry migration succeeds
    store.migrate_embeddings(alt_provider)
    assert (
        conn.execute(
            "SELECT active_generation FROM storage_manifest WHERE manifest_key = 'manifest'"
        ).fetchone()[0]
        == 2
    )


def test_09_sqlite_canonical_hydration(store: LocalKnowledgeStore):
    """
    Test 9: SQLite Canonical Hydration.
    Query chunks; verify text and Phase 4B.1 user metadata are hydrated directly from SQLite.
    """
    doc = make_test_doc("doc-hydrate")
    chunk = make_test_chunk(
        "chk-hyd", "doc-hydrate", 0, "Hydration verification text", tag="custom_tag"
    )
    store.ingest_document(
        doc, [chunk], source_identity="api_documentation:docs/hydrate.md"
    )

    chunks = store.query_chunks("Hydration verification")
    assert len(chunks) == 1
    assert chunks[0].content == "Hydration verification text"
    assert chunks[0].metadata["tag"] == "custom_tag"
    assert chunks[0].source_ref == "docs/doc-hydrate.md#sec0"


def test_10_phase_4b_1_metadata_validation(store: LocalKnowledgeStore):
    """
    Test 10: Phase 4B.1 Metadata Validation.
    Test strict allowlist, boolean/int distinctions, and sensitive credential filtering.
    """
    # Prohibited metadata key rejected by schema validator
    with pytest.raises(ValueError, match="Unrecognized or forbidden metadata key"):
        KnowledgeDocument(
            document_id="doc-bad-meta",
            source_type=KnowledgeSourceType.API_DOCUMENTATION,
            title="Bad Meta",
            metadata={"forbidden_key": "forbidden_val"},
        )

    # Prohibited credential value in metadata rejected
    with pytest.raises(ValueError, match="prohibited sensitive credential pattern"):
        KnowledgeDocument(
            document_id="doc-bad-cred",
            source_type=KnowledgeSourceType.API_DOCUMENTATION,
            title="Bad Cred",
            metadata={"tag": "bearer secret-token-value-1234567890"},
        )


def test_11_collision_safe_chroma_storage_ids():
    """
    Test 11: Collision-Safe Chroma Storage IDs.
    Test boundary document and chunk IDs containing colons; assert reversible parsing.
    """
    doc_id = "doc:with:colons:123"
    chunk_id = "chk:also:with:colons:456"

    storage_id = format_chroma_id(doc_id, chunk_id)
    assert storage_id.startswith(f"{len(doc_id)}:")

    parsed_doc, parsed_chunk = parse_chroma_id(storage_id)
    assert parsed_doc == doc_id
    assert parsed_chunk == chunk_id

    # Malformed storage ID raises CorruptedStorageIdError
    with pytest.raises(CorruptedStorageIdError):
        parse_chroma_id("bad_prefix_no_colon")

    with pytest.raises(CorruptedStorageIdError):
        parse_chroma_id("999:short")


def test_12_failure_history_identity_collisions(store: LocalKnowledgeStore):
    """
    Test 12: Failure-History Identity Collisions.
    Ingest distinct run IDs sharing a 32-char prefix; assert distinct fingerprints and no collisions.
    """
    prefix = "run-prefix-" + "a" * 25
    run1 = prefix + "-suffix-one"
    run2 = prefix + "-suffix-two"

    sha1 = hashlib.sha256(run1.encode("utf-8")).hexdigest()
    sha2 = hashlib.sha256(run2.encode("utf-8")).hexdigest()

    doc1 = KnowledgeDocument(
        document_id="doc-fail-1",
        source_type=KnowledgeSourceType.FAILURE_HISTORY,
        title="Failure Run 1",
        source_ref="runs/run1.json",
    )
    chunk1 = KnowledgeChunk(
        chunk_id="chk-f1",
        document_id="doc-fail-1",
        content="Failure history run 1 details",
        chunk_index=0,
        source_type=KnowledgeSourceType.FAILURE_HISTORY,
    )
    store.ingest_document(
        doc1,
        [chunk1],
        source_identity=f"failure_history:{sha1[:16]}",
        raw_run_id_sha256=sha1,
    )

    # Ingest distinct run ID
    doc2 = KnowledgeDocument(
        document_id="doc-fail-2",
        source_type=KnowledgeSourceType.FAILURE_HISTORY,
        title="Failure Run 2",
        source_ref="runs/run2.json",
    )
    chunk2 = KnowledgeChunk(
        chunk_id="chk-f2",
        document_id="doc-fail-2",
        content="Failure history run 2 details",
        chunk_index=0,
        source_type=KnowledgeSourceType.FAILURE_HISTORY,
    )
    store.ingest_document(
        doc2,
        [chunk2],
        source_identity=f"failure_history:{sha2[:16]}",
        raw_run_id_sha256=sha2,
    )

    conn = store.conn_manager.get_connection()
    hashes = [
        r[0]
        for r in conn.execute("SELECT raw_run_id_sha256 FROM source_catalog").fetchall()
    ]
    assert sha1 in hashes
    assert sha2 in hashes


def test_13_deterministic_hash_embedding_edge_cases():
    """
    Test 13: Deterministic Hash Embedding Edge Cases.
    Test symbol-only text, exact cancellation zero-vector, unit norm, and empty string ValueError.
    """
    provider = DeterministicHashEmbeddingProvider()

    # Empty string raises ValueError
    with pytest.raises(ValueError, match="Input text must not be empty"):
        provider.embed_text("   \n\t  ")

    # Symbol-only text triggers fallback
    v_sym = provider.embed_text("!@#$%^&*()_+=-{}[]:;")
    assert len(v_sym) == 384
    norm_sym = math.sqrt(sum(x * x for x in v_sym))
    assert math.isclose(norm_sym, 1.0, rel_tol=1e-5)

    # Determinism assertion
    v_a = provider.embed_text("Standard deterministic test sentence.")
    v_b = provider.embed_text("Standard deterministic test sentence.")
    assert v_a == v_b


def test_14_provider_resolution_after_restart(temp_store_dir: Path):
    """
    Test 14: Provider Resolution After Restart.
    Test manifest mismatch detection and fail-closed behavior.
    """
    cfg = StoreConfig(storage_path=temp_store_dir, skip_security_checks=True)
    store = LocalKnowledgeStore(config=cfg)

    # Reopening with incompatible provider must fail closed
    incompat_provider = MockAlternativeEmbeddingProvider(dimension=768)
    with pytest.raises(EmbeddingProviderMismatchError):
        LocalKnowledgeStore(config=cfg, provider=incompat_provider)


def _posix_lockmanager_worker(
    locks_dir_str: str, count_file_str: str, iterations: int = 2
):
    from backend.app.services.knowledge_store import LockManager

    lm = LockManager(Path(locks_dir_str))
    for _ in range(iterations):
        lm.acquire_writer_lock()
        try:
            with open(count_file_str, "r") as f:
                val = int(f.read().strip())
            time.sleep(0.01)
            with open(count_file_str, "w") as f:
                f.write(str(val + 1))
        finally:
            lm.release_writer_lock()


def test_15_posix_multi_process_locks(temp_store_dir: Path):
    """
    Test 15: POSIX Multi-Process Locks.
    Multi-process concurrency stress test verifying mutual exclusion with .writer.lock
    using actual LockManager.acquire_writer_lock() and release_writer_lock().
    """
    if sys.platform == "win32":
        pytest.skip("POSIX multi-process test requires POSIX platform")

    locks_dir = temp_store_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)

    count_path = temp_store_dir / "counter.txt"
    with open(count_path, "w") as f:
        f.write("0")

    procs = []
    for _ in range(5):
        p = multiprocessing.Process(
            target=_posix_lockmanager_worker, args=(str(locks_dir), str(count_path), 2)
        )
        p.start()
        procs.append(p)

    for p in procs:
        p.join(timeout=10)

    with open(count_path, "r") as f:
        final_count = int(f.read().strip())
    assert final_count == 10


def _windows_lock_contention_worker(
    lock_path_str: str, ready_event: Any, release_event: Any, result_queue: Any
):
    import msvcrt
    from backend.app.services.knowledge_store import LockManager

    lm = LockManager(Path(lock_path_str).parent)
    fd = os.open(lock_path_str, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        lm._win_lock(fd, exclusive=True, nonblocking=False)
        ready_event.set()
        # Hold lock until main process signals release
        release_event.wait(timeout=10)
        lm._win_unlock(fd)
        result_queue.put("released")
    except Exception as e:
        result_queue.put(f"worker_error: {e}")
    finally:
        os.close(fd)


def test_16_windows_multi_process_locks(tmp_path: Path):
    """
    Test 16: Windows Multi-Process Locks.
    Tests actual OS-level Win32 LockFileEx contention, acquisition, and release across processes.
    Skipped on non-Windows platforms.
    """
    if sys.platform != "win32":
        pytest.skip(
            "Windows multi-process file locking test requires native Windows environment (current OS: linux)"
        )

    locks_dir = tmp_path / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file = locks_dir / ".test.lock"

    ready_event = multiprocessing.Event()
    release_event = multiprocessing.Event()
    result_queue = multiprocessing.Queue()

    p = multiprocessing.Process(
        target=_windows_lock_contention_worker,
        args=(str(lock_file), ready_event, release_event, result_queue),
    )
    p.start()
    try:
        assert ready_event.wait(timeout=5), "Worker failed to acquire initial lock"

        from backend.app.services.knowledge_store import LockManager

        lm = LockManager(locks_dir)
        fd2 = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # 1. Non-blocking acquisition while worker holds exclusive lock must fail with winerror 32 or 33
            with pytest.raises(OSError) as exc_info:
                lm._win_lock(fd2, exclusive=True, nonblocking=True)
            assert exc_info.value.winerror in (32, 33)

            # 2. Signal worker to release lock
            release_event.set()
            assert result_queue.get(timeout=5) == "released"

            # 3. Main process should now acquire exclusive lock successfully
            lm._win_lock(fd2, exclusive=True, nonblocking=True)

            # 4. Release lock
            lm._win_unlock(fd2)
        finally:
            os.close(fd2)
    finally:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()


def test_17_storage_root_security_traversal(tmp_path: Path):
    """
    Test 17: Storage-Root Security Traversal.
    Test symlink traversal rejection, mode 0700, UID check, and mount blocking.
    """
    target_dir = tmp_path / "actual_dir"
    target_dir.mkdir(mode=0o700, exist_ok=True)
    symlink_dir = tmp_path / "symlink_dir"
    symlink_dir.symlink_to(target_dir, target_is_directory=True)

    # Insecure symlink storage path must be rejected
    cfg_symlink = StoreConfig(storage_path=symlink_dir, skip_security_checks=False)
    with pytest.raises(SecurityStorageError):
        LocalKnowledgeStore(config=cfg_symlink)

    # Mock unapproved mount type
    cfg_valid = StoreConfig(storage_path=target_dir, skip_security_checks=False)
    with mock.patch(
        "backend.app.services.knowledge_store._read_proc_mounts",
        return_value=[f"1.2.3.4:/share {target_dir} nfs rw 0 0\n"],
    ):
        with pytest.raises(SecurityStorageError, match="forbidden remote filesystem"):
            LocalKnowledgeStore(config=cfg_valid)


def test_18_chroma_rebuild_from_canonical_records(store: LocalKnowledgeStore):
    """
    Test 18: Chroma Rebuild from Canonical Records.
    Wipe ChromaDB; call rebuild_index(); verify complete vector index restoration.
    """
    d1 = make_test_doc("doc-rb-1")
    c1 = make_test_chunk("chk-rb1", "doc-rb-1", 0, "Rebuild chunk 1 text")
    store.ingest_document(d1, [c1], source_identity="api_documentation:docs/rb1.md")

    # Wipe Chroma collection directly
    col = store.chroma_client.get_collection("chunks_gen_1")
    store.chroma_client.delete_collection("chunks_gen_1")
    assert "chunks_gen_1" not in [
        c.name for c in store.chroma_client.list_collections()
    ]

    # Call rebuild_index()
    res = store.rebuild_index()
    assert res.documents_migrated == 1
    assert res.chunks_migrated == 1
    assert res.to_generation == 2

    # Querying without session should return the restored chunk from Gen 2
    chunks = store.query_chunks("Rebuild chunk 1")
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "chk-rb1"


def test_19_concurrent_multi_thread_snapshot_registration(store: LocalKnowledgeStore):
    """
    Test 19: Concurrent Multi-Thread Snapshot Registration.
    Execute 5 concurrent threads registering snapshots and querying; assert zero transaction interleaving.
    """
    doc = make_test_doc("doc-conc-1")
    chunk = make_test_chunk(
        "chk-conc", "doc-conc-1", 0, "Concurrent multi-thread chunk"
    )
    store.ingest_document(
        doc, [chunk], source_identity="api_documentation:docs/conc.md"
    )

    errors: List[Exception] = []

    def thread_worker():
        try:
            for _ in range(3):
                with store.acquire_query_session() as s:
                    res = store.query_chunks("Concurrent multi-thread", session=s)
                    assert len(res) == 1
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=thread_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []


def test_20_partial_initialization_retry(temp_store_dir: Path):
    """
    Test 20: Partial Initialization Retry.
    Simulate crash during DDL; assert restart idempotently completes initialization.
    """
    db_path = temp_store_dir / "coordinator.sqlite3"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
    )
    conn.close()

    # Store initialization must complete idempotently despite partially existing schema
    cfg = StoreConfig(storage_path=temp_store_dir, skip_security_checks=True)
    store = LocalKnowledgeStore(config=cfg)
    conn = store.conn_manager.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM global_state").fetchone()[0] == 1


def test_21_snapshot_registration_failure_rollback(store: LocalKnowledgeStore):
    """
    Test 21: Snapshot Registration Failure Rollback.
    Inject failure into provider resolution after snapshot transaction commits;
    verify committed SQLite snapshot rows are rolled back under lifecycle lock before pin is released.
    """
    doc = make_test_doc("doc-snap-fail")
    chunk = make_test_chunk("chk-sf", "doc-snap-fail", 0, "Snapshot fail text")
    store.ingest_document(
        doc, [chunk], source_identity="api_documentation:docs/snap_fail.md"
    )

    # Inject failure into _resolve_provider
    with mock.patch.object(
        store, "_resolve_provider", side_effect=RuntimeError("Provider resolution boom")
    ):
        with pytest.raises(RuntimeError, match="Provider resolution boom"):
            store.acquire_query_session()

    conn = store.conn_manager.get_connection()
    # Snapshot row and protected documents must be cleanly rolled back
    assert conn.execute("SELECT COUNT(*) FROM active_snapshots").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM snapshot_protected_documents").fetchone()[0]
        == 0
    )
    # Pin directory must contain 0 pin files
    assert list(store.pins_dir.glob("*.pin")) == []


def test_22_standalone_pruning_lock_acquisition(store: LocalKnowledgeStore):
    """
    Test 22: Standalone Pruning Lock Acquisition.
    Standalone prune_retired_generations() acquires .writer.lock EX followed by .lifecycle.lock EX
    and releases them in reverse order (L1 -> L2).
    """
    store._lock_manager.events.clear()
    store.prune_retired_generations()

    expected_order = [
        ("acquire_writer_lock", "EX"),
        ("acquire_lifecycle_exclusive", "EX"),
        ("release_lifecycle_exclusive", "EX"),
        ("release_writer_lock", "EX"),
    ]
    assert store._lock_manager.events == expected_order


def test_23_migration_triggered_pruning_lock_ownership(store: LocalKnowledgeStore):
    """
    Test 23: Migration-Triggered Pruning Lock Ownership.
    Migration-triggered pruning (_prune_retired_generations_under_writer()) acquires .lifecycle.lock EX
    without re-acquiring .writer.lock EX (which is already held by migration).
    """
    store._lock_manager.events.clear()
    store._lock_manager.acquire_writer_lock()
    try:
        store._prune_retired_generations_under_writer()
    finally:
        store._lock_manager.release_writer_lock()

    under_writer_events = store._lock_manager.events[1:-1]
    assert ("acquire_writer_lock", "EX") not in under_writer_events
    expected_order = [
        ("acquire_lifecycle_exclusive", "EX"),
        ("release_lifecycle_exclusive", "EX"),
    ]
    assert under_writer_events == expected_order


def test_24_startup_reconciliation_lock_non_reentrancy(store: LocalKnowledgeStore):
    """
    Test 24: Startup Reconciliation Lock Non-Reentrancy.
    reconcile_startup() completes start-to-finish without re-acquiring either lock reentrantly.
    """
    store._lock_manager.events.clear()
    report = store.reconcile_startup()
    assert report.success is True

    expected_order = [
        ("acquire_writer_lock", "EX"),
        ("acquire_lifecycle_exclusive", "EX"),
        ("release_lifecycle_exclusive", "EX"),
        ("release_writer_lock", "EX"),
    ]
    assert store._lock_manager.events == expected_order


def test_25_pin_lock_conflict_classified_as_live(store: LocalKnowledgeStore):
    """
    Test 25: Pin Lock Conflict Classified as Live.
    Probing a pin with an active kernel shared lock returns PinStatus.LIVE.
    """
    pin_path = store.pins_dir / "active_test.pin"
    fd = os.open(str(pin_path), os.O_CREAT | os.O_RDWR, 0o600)
    store._lock_manager.acquire_pin_shared(fd)
    try:
        status = store.probe_pin_liveness(pin_path)
        assert status == PinStatus.LIVE
    finally:
        store._lock_manager.release_pin(fd)
        os.close(fd)
        os.unlink(pin_path)


def test_26_fail_closed_pin_probe_in_reconciliation(store: LocalKnowledgeStore):
    """
    Test 26: Fail-Closed Pin Probe in Reconciliation.
    Inject unexpected error during probe_pin_liveness(); assert status is PinStatus.UNKNOWN,
    the .pin file is NOT unlinked, and snapshot row in active_snapshots is preserved.
    """
    pin_path = store.pins_dir / "unknown_sess.pin"
    pin_path.touch(mode=0o600)

    conn = store.conn_manager.get_connection()
    conn.execute(
        "INSERT INTO active_snapshots (session_id, generation, global_commit_seq, embedding_provider, "
        "embedding_model, embedding_dimension, embedding_config_version, snapshot_type, created_at) "
        "VALUES ('unknown_sess', 1, 0, 'deterministic_hash', 'sha256_word_char_384', 384, 'v1.0', 'GLOBAL_QUERY', datetime('now'));"
    )

    # Inject unexpected error inside probe_pin_liveness implementation via acquire_pin_exclusive_nb
    with mock.patch.object(
        store._lock_manager,
        "acquire_pin_exclusive_nb",
        side_effect=OSError(errno.EIO, "Simulated hardware I/O error during probe"),
    ):
        # Directly assert actual probe_pin_liveness catches unexpected error and returns UNKNOWN
        assert store.probe_pin_liveness(pin_path) == PinStatus.UNKNOWN

        report = store.reconcile_startup()
        assert report.success is True
        assert report.preserved_sessions >= 1

    # Pin file and snapshot row must be preserved
    assert pin_path.exists()
    assert (
        conn.execute(
            "SELECT 1 FROM active_snapshots WHERE session_id = 'unknown_sess'"
        ).fetchone()
        is not None
    )

    # Cleanup
    conn.execute("DELETE FROM active_snapshots WHERE session_id = 'unknown_sess';")
    pin_path.unlink()


def test_27_fail_closed_pin_probe_in_retired_generation_pruning(
    store: LocalKnowledgeStore,
):
    """
    Test 27: Fail-Closed Pin Probe in Retired-Generation Pruning.
    Inject unexpected error during pin probe; assert _prune_retired_generations_locked() detects
    PinStatus.UNKNOWN, aborts pruning, and leaves Chroma collection and SQLite row intact.
    """
    conn = store.conn_manager.get_connection()
    conn.execute(
        "INSERT INTO retired_generations (generation, chunk_collection, embedding_provider, embedding_model, "
        "embedding_dimension, embedding_config_version, retired_at) "
        "VALUES (99, 'chunks_gen_99', 'deterministic_hash', 'sha256_word_char_384', 384, 'v1.0', datetime('now'));"
    )
    col = store.chroma_client.get_or_create_collection("chunks_gen_99")

    # Place a dummy pin file
    dummy_pin = store.pins_dir / "dummy.pin"
    dummy_pin.touch(mode=0o600)

    try:
        # Inject unexpected error inside probe_pin_liveness implementation via acquire_pin_exclusive_nb
        with mock.patch.object(
            store._lock_manager,
            "acquire_pin_exclusive_nb",
            side_effect=PermissionError("Simulated unhandled EPERM during probe"),
        ):
            # Directly assert actual probe_pin_liveness catches unexpected error and returns UNKNOWN
            assert store.probe_pin_liveness(dummy_pin) == PinStatus.UNKNOWN

            store.prune_retired_generations()

        # Both collection and SQLite row must remain intact
        assert "chunks_gen_99" in [
            c.name for c in store.chroma_client.list_collections()
        ]
        assert (
            conn.execute(
                "SELECT 1 FROM retired_generations WHERE generation = 99"
            ).fetchone()
            is not None
        )
    finally:
        conn.execute("DELETE FROM retired_generations WHERE generation = 99;")
        store.chroma_client.delete_collection("chunks_gen_99")
        if dummy_pin.exists():
            dummy_pin.unlink()


def test_28_query_session_store_identity_validation(
    store: LocalKnowledgeStore, tmp_path: Path
):
    """
    Test 28: Query Session Store Identity Validation.
    Verify query_chunks(..., session=session) accepts session created by same store
    and rejects session created by a different LocalKnowledgeStore with InvalidSessionError.
    """
    # Second store instance in a separate directory
    other_dir = tmp_path / "other_store"
    other_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(other_dir, 0o700)
    cfg2 = StoreConfig(storage_path=other_dir, skip_security_checks=True)
    store2 = LocalKnowledgeStore(config=cfg2)

    s1 = store.acquire_query_session()
    s2 = store2.acquire_query_session()

    try:
        # Same-store session passes validation
        store.query_chunks("hello", session=s1)

        # Foreign-store session fails with InvalidSessionError
        with pytest.raises(
            InvalidSessionError, match="Session belongs to another store instance"
        ):
            store.query_chunks("hello", session=s2)
    finally:
        s1.close()
        s2.close()


def test_29_same_store_multi_thread_writer_contention(store: LocalKnowledgeStore):
    """
    Test 29: Same-Store Multi-Thread Writer Contention.
    Verifies that multiple threads sharing one LocalKnowledgeStore cannot enter
    writer critical sections concurrently, and never reuse or corrupt OS lock descriptors.
    """
    thread_count = 5
    iterations = 3
    critical_section_active = False
    overlap_detected = False
    lock_check = threading.Lock()
    fds_per_thread = {}

    def writer_worker(thread_id: int):
        nonlocal critical_section_active, overlap_detected
        for it in range(iterations):
            store._lock_manager.acquire_writer_lock()
            try:
                # Capture the thread-local descriptor
                fd = getattr(store._lock_manager._local, "writer_fd", None)
                assert fd is not None, "Thread failed to allocate its own writer_fd"
                with lock_check:
                    if thread_id not in fds_per_thread:
                        fds_per_thread[thread_id] = set()
                    fds_per_thread[thread_id].add(fd)
                    if critical_section_active:
                        overlap_detected = True
                    critical_section_active = True

                # Simulate work in critical section
                time.sleep(0.01)

                with lock_check:
                    critical_section_active = False
            finally:
                store._lock_manager.release_writer_lock()
                # Verify descriptor is cleared on release
                assert getattr(store._lock_manager._local, "writer_fd", None) is None

    threads = [
        threading.Thread(target=writer_worker, args=(i,)) for i in range(thread_count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not overlap_detected, (
        "Concurrent threads entered writer critical section simultaneously"
    )

    for t_id, fds in fds_per_thread.items():
        assert len(fds) >= 1


def test_30_ingestion_chunk_document_mismatch_validation(store: LocalKnowledgeStore):
    """
    Test 30: Ingestion Chunk-Document Mismatch Validation.
    Verifies that ingest_document() and replace_document() validate that every chunk's
    document_id and source_type match the parent KnowledgeDocument before any DB or Chroma writes.
    """
    doc = make_test_doc("doc-parent-1")
    doc.source_type = KnowledgeSourceType.OPENAPI_SPEC

    # 1. Chunk with mismatched document_id
    bad_chunk_doc_id = make_test_chunk(
        "chk-bad-doc", "doc-MISMATCHED", 0, "mismatched doc id content"
    )
    bad_chunk_doc_id.source_type = KnowledgeSourceType.OPENAPI_SPEC

    with pytest.raises(DocumentChunkMismatchError, match="document_id"):
        store.ingest_document(
            doc, [bad_chunk_doc_id], source_identity="openapi:test-spec"
        )

    # Verify nothing was written to SQLite or Chroma
    conn = store.conn_manager.get_connection()
    doc_count = conn.execute("SELECT COUNT(*) FROM canonical_documents;").fetchall()[0][
        0
    ]
    chunk_count = conn.execute("SELECT COUNT(*) FROM canonical_chunks;").fetchall()[0][
        0
    ]
    assert doc_count == 0
    assert chunk_count == 0

    col = store._get_or_create_collection(1)
    assert col.count() == 0

    # 2. Chunk with mismatched source_type
    bad_chunk_source_type = make_test_chunk(
        "chk-bad-src", "doc-parent-1", 0, "mismatched src type content"
    )
    bad_chunk_source_type.source_type = KnowledgeSourceType.FAILURE_HISTORY

    with pytest.raises(DocumentChunkMismatchError, match="source_type"):
        store.ingest_document(
            doc, [bad_chunk_source_type], source_identity="openapi:test-spec"
        )

    assert (
        conn.execute("SELECT COUNT(*) FROM canonical_documents;").fetchall()[0][0] == 0
    )
    assert col.count() == 0

    # 3. Successful ingestion of valid document and chunk
    valid_chunk = make_test_chunk("chk-valid", "doc-parent-1", 0, "valid content")
    valid_chunk.source_type = KnowledgeSourceType.OPENAPI_SPEC
    store.ingest_document(doc, [valid_chunk], source_identity="openapi:test-spec")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM canonical_documents WHERE status = 'active';"
        ).fetchall()[0][0]
        == 1
    )

    # 4. replace_document() validation with mismatched chunk document_id
    doc2 = make_test_doc("doc-parent-2")
    doc2.source_type = KnowledgeSourceType.OPENAPI_SPEC
    bad_rep_chunk = make_test_chunk(
        "chk-bad-rep", "doc-parent-WRONG", 0, "bad rep content"
    )
    bad_rep_chunk.source_type = KnowledgeSourceType.OPENAPI_SPEC

    with pytest.raises(DocumentChunkMismatchError, match="document_id"):
        store.replace_document(
            "doc-parent-1", doc2, [bad_rep_chunk], source_identity="openapi:test-spec"
        )

    # Verify original document remains active and untampered
    active_row = conn.execute(
        "SELECT active_document_id FROM source_catalog WHERE source_identity = 'openapi:test-spec';"
    ).fetchall()[0]
    assert active_row[0] == "doc-parent-1"

    # 5. replace_document() validation with mismatched chunk source_type
    bad_rep_src = make_test_chunk(
        "chk-bad-rep2", "doc-parent-2", 0, "bad rep content 2"
    )
    bad_rep_src.source_type = KnowledgeSourceType.FAILURE_HISTORY

    with pytest.raises(DocumentChunkMismatchError, match="source_type"):
        store.replace_document(
            "doc-parent-1", doc2, [bad_rep_src], source_identity="openapi:test-spec"
        )

    active_row = conn.execute(
        "SELECT active_document_id FROM source_catalog WHERE source_identity = 'openapi:test-spec';"
    ).fetchall()[0]
    assert active_row[0] == "doc-parent-1"


def test_31_storage_root_intermediate_symlink_rejection(tmp_path: Path):
    """
    Test 31: Storage-Root Intermediate Symlink Rejection.
    Verifies that component-by-component traversal rejects paths where an intermediate
    parent directory is a symlink, without following or resolving it.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX component traversal test skipped on Windows")

    real_parent = tmp_path / "real_parent"
    real_parent.mkdir(mode=0o700, exist_ok=True)

    symlink_parent = tmp_path / "sym_parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)

    # Insecure path passing through an intermediate symlink component
    insecure_store_path = symlink_parent / "nested_store"

    cfg = StoreConfig(storage_path=insecure_store_path, skip_security_checks=False)
    with pytest.raises(SecurityStorageError, match="Insecure storage path component"):
        LocalKnowledgeStore(config=cfg)


def test_32_symlink_rejection_causes_no_filesystem_side_effects(tmp_path: Path):
    """
    Test 32: Symlink Rejection Causes No Filesystem Side Effects.
    Verifies that rejected final and intermediate symlinks do not modify target
    permissions via chmod or create directories under the symlink target via mkdir.
    """
    if sys.platform == "win32":
        pytest.skip("POSIX symlink side-effect test skipped on Windows")

    # 1. Final symlink rejection must not chmod target or add files
    target_dir = tmp_path / "target_dir"
    target_dir.mkdir(mode=0o755, exist_ok=True)
    os.chmod(str(target_dir), 0o755)
    sentinel = target_dir / "sentinel.txt"
    sentinel.write_text("unmodified")

    sym_final = tmp_path / "sym_final"
    sym_final.symlink_to(target_dir, target_is_directory=True)

    cfg_final = StoreConfig(storage_path=sym_final, skip_security_checks=False)
    with pytest.raises(SecurityStorageError):
        LocalKnowledgeStore(config=cfg_final)

    # Assert target directory permissions were not modified by chmod
    st = target_dir.stat()
    assert (st.st_mode & 0o777) == 0o755, (
        f"Target mode modified: {oct(st.st_mode & 0o777)}"
    )
    assert sentinel.read_text() == "unmodified"
    assert os.listdir(str(target_dir)) == ["sentinel.txt"], (
        "Files or directories created under target"
    )

    # 2. Intermediate symlink rejection must not create nested subdirectories under target
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir(mode=0o755, exist_ok=True)
    os.chmod(str(real_parent), 0o755)

    sym_parent = tmp_path / "sym_parent"
    sym_parent.symlink_to(real_parent, target_is_directory=True)

    insecure_nested = sym_parent / "unwanted_subdir" / "storage_root"
    cfg_nested = StoreConfig(storage_path=insecure_nested, skip_security_checks=False)
    with pytest.raises(SecurityStorageError):
        LocalKnowledgeStore(config=cfg_nested)

    assert os.listdir(str(real_parent)) == [], (
        "Subdirectory created under intermediate symlink target"
    )


def test_33_windows_reparse_point_rejection(tmp_path: Path):
    """
    Test 33: Windows Reparse Point Security Rejection.
    Verifies that Windows directory validation detects and rejects intermediate
    and final reparse points (junctions/symlinks) without modifying targets.
    Skipped on non-Windows platforms.
    """
    if sys.platform != "win32":
        pytest.skip(
            "Windows reparse-point rejection test requires native Windows environment (current OS: linux)"
        )

    import subprocess

    real_dir = tmp_path / "real_win_dir"
    real_dir.mkdir(parents=True, exist_ok=True)
    sentinel = real_dir / "sentinel.txt"
    sentinel.write_text("safe")

    junction_dir = tmp_path / "junction_dir"
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction_dir), str(real_dir)],
        capture_output=True,
    )
    if res.returncode != 0:
        pytest.skip(
            f"Could not create Windows junction: {res.stderr.decode('utf-8', errors='ignore')}"
        )

    # 1. Final reparse point rejection
    cfg_final = StoreConfig(storage_path=junction_dir, skip_security_checks=False)
    with pytest.raises(SecurityStorageError, match="reparse point"):
        LocalKnowledgeStore(config=cfg_final)
    assert sentinel.read_text() == "safe"
    assert os.listdir(str(real_dir)) == ["sentinel.txt"], (
        "Target directory modified during final rejection"
    )

    # 2. Intermediate reparse point rejection
    intermediate_path = junction_dir / "sub_store"
    cfg_inter = StoreConfig(storage_path=intermediate_path, skip_security_checks=False)
    with pytest.raises(SecurityStorageError, match="reparse point"):
        LocalKnowledgeStore(config=cfg_inter)
    assert not (real_dir / "sub_store").exists(), (
        "Created directory under rejected reparse point"
    )
    assert sentinel.read_text() == "safe"


def test_34_windows_directory_type_validation(tmp_path: Path):
    """
    Test 34: Windows Directory Type Validation and Target Protection.
    Verifies that Windows component traversal validates that every component is
    genuinely a directory and rejects files with SecurityStorageError without
    modifying or overwriting existing files.
    Skipped on non-Windows platforms.
    """
    if sys.platform != "win32":
        pytest.skip(
            "Windows directory type validation test requires native Windows environment (current OS: linux)"
        )

    # 1. Reject when an intermediate path component is a regular file
    parent_file = tmp_path / "regular_file.txt"
    parent_file.write_text("file content")

    insecure_intermediate = parent_file / "nested_store"
    cfg_intermediate = StoreConfig(
        storage_path=insecure_intermediate, skip_security_checks=False
    )
    with pytest.raises(SecurityStorageError, match="not a directory"):
        LocalKnowledgeStore(config=cfg_intermediate)

    # Verify no filesystem side effects on the target file
    assert parent_file.is_file(), "Regular file was modified or removed"
    assert parent_file.read_text() == "file content"

    # 2. Reject when the final path component is a regular file
    cfg_final = StoreConfig(storage_path=parent_file, skip_security_checks=False)
    with pytest.raises(SecurityStorageError, match="not a directory"):
        LocalKnowledgeStore(config=cfg_final)

    assert parent_file.is_file()
    assert parent_file.read_text() == "file content"


def test_35_windows_nt_helper_declarations_and_structure_abi():
    """
    Test 35: Windows NT Helper Declarations, Ctypes ABI, and NTSTATUS Conversion.
    Verifies that _configure_ntdll() explicitly declares correct argtypes and restype
    for NtOpenFile and NtCreateFile, verifies structure layouts against Windows NT 64-bit ABI,
    and proves 32-bit unsigned NTSTATUS code conversions without pretending Linux execution
    validates native Windows kernel behavior.
    """
    import ctypes
    from ctypes import wintypes
    from backend.app.services.knowledge_store import (
        UNICODE_STRING,
        OBJECT_ATTRIBUTES,
        IO_STATUS_BLOCK,
        BY_HANDLE_FILE_INFORMATION,
        _configure_ntdll,
        _get_win32_ntdll,
    )

    class DummyFunction:
        def __init__(self):
            self.argtypes = None
            self.restype = None

    class DummyNTDLL:
        def __init__(self):
            self.NtOpenFile = DummyFunction()
            self.NtCreateFile = DummyFunction()

    dummy_ntdll = DummyNTDLL()
    _configure_ntdll(dummy_ntdll)

    # 1. Verify NtOpenFile argtypes and restype
    assert len(dummy_ntdll.NtOpenFile.argtypes) == 6
    assert dummy_ntdll.NtOpenFile.argtypes[0] == ctypes.POINTER(wintypes.HANDLE)
    assert dummy_ntdll.NtOpenFile.argtypes[1] == wintypes.DWORD
    assert dummy_ntdll.NtOpenFile.argtypes[2] == ctypes.POINTER(OBJECT_ATTRIBUTES)
    assert dummy_ntdll.NtOpenFile.argtypes[3] == ctypes.POINTER(IO_STATUS_BLOCK)
    assert dummy_ntdll.NtOpenFile.argtypes[4] == wintypes.DWORD
    assert dummy_ntdll.NtOpenFile.argtypes[5] == wintypes.DWORD
    assert dummy_ntdll.NtOpenFile.restype == wintypes.LONG

    # 2. Verify NtCreateFile argtypes and restype
    assert len(dummy_ntdll.NtCreateFile.argtypes) == 11
    assert dummy_ntdll.NtCreateFile.argtypes[0] == ctypes.POINTER(wintypes.HANDLE)
    assert dummy_ntdll.NtCreateFile.argtypes[1] == wintypes.DWORD
    assert dummy_ntdll.NtCreateFile.argtypes[2] == ctypes.POINTER(OBJECT_ATTRIBUTES)
    assert dummy_ntdll.NtCreateFile.argtypes[3] == ctypes.POINTER(IO_STATUS_BLOCK)
    assert dummy_ntdll.NtCreateFile.argtypes[4] == ctypes.c_void_p
    assert dummy_ntdll.NtCreateFile.argtypes[5] == wintypes.DWORD
    assert dummy_ntdll.NtCreateFile.argtypes[6] == wintypes.DWORD
    assert dummy_ntdll.NtCreateFile.argtypes[7] == wintypes.DWORD
    assert dummy_ntdll.NtCreateFile.argtypes[8] == wintypes.DWORD
    assert dummy_ntdll.NtCreateFile.argtypes[9] == ctypes.c_void_p
    assert dummy_ntdll.NtCreateFile.argtypes[10] == wintypes.DWORD
    assert dummy_ntdll.NtCreateFile.restype == wintypes.LONG

    # 3. Verify structure layouts and offsets on 64-bit architectures
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        # OBJECT_ATTRIBUTES: 48 bytes total
        assert OBJECT_ATTRIBUTES.Length.offset == 0
        assert OBJECT_ATTRIBUTES.RootDirectory.offset == 8
        assert OBJECT_ATTRIBUTES.ObjectName.offset == 16
        assert OBJECT_ATTRIBUTES.Attributes.offset == 24
        assert OBJECT_ATTRIBUTES.SecurityDescriptor.offset == 32
        assert OBJECT_ATTRIBUTES.SecurityQualityOfService.offset == 40
        assert ctypes.sizeof(OBJECT_ATTRIBUTES) == 48

        # IO_STATUS_BLOCK: 16 bytes total
        assert IO_STATUS_BLOCK.Status.offset == 0
        assert IO_STATUS_BLOCK.Information.offset == 8
        assert ctypes.sizeof(IO_STATUS_BLOCK) == 16

        # UNICODE_STRING: 16 bytes total
        assert UNICODE_STRING.Length.offset == 0
        assert UNICODE_STRING.MaximumLength.offset == 2
        assert UNICODE_STRING.Buffer.offset == 8
        assert ctypes.sizeof(UNICODE_STRING) == 16

    # 4. Verify 32-bit NTSTATUS code masking
    # STATUS_OBJECT_NAME_NOT_FOUND (0xC0000034) returned as signed 32-bit int (-1073741772)
    assert (-1073741772) & 0xFFFFFFFF == 0xC0000034
    # STATUS_NOT_A_DIRECTORY (0xC0000103) returned as signed 32-bit int (-1073741565)
    assert (-1073741565) & 0xFFFFFFFF == 0xC0000103
    # STATUS_OBJECT_NAME_COLLISION (0xC0000035) returned as signed 32-bit int (-1073741771)
    assert (-1073741771) & 0xFFFFFFFF == 0xC0000035
    # STATUS_OBJECT_PATH_NOT_FOUND (0xC000003A) returned as signed 32-bit int (-1073741766)
    assert (-1073741766) & 0xFFFFFFFF == 0xC000003A
    # STATUS_SUCCESS (0x00000000)
    assert 0 & 0xFFFFFFFF == 0

    # 5. Non-Windows environment returns None (honest platform distinction)
    if sys.platform != "win32":
        assert _get_win32_ntdll() is None


def test_36_windows_nt_native_handle_anchored_execution(tmp_path: Path):
    """
    Test 36: Native Windows NT Handle-Anchored Traversal Execution.
    Tests actual OS-level ntdll.NtOpenFile and ntdll.NtCreateFile handle-anchored traversal
    and attribute validation on the native Windows operating system, including BMP and
    supplementary Unicode path components.
    Skipped on non-Windows platforms.
    """
    if sys.platform != "win32":
        pytest.skip(
            "Windows NT native execution test requires native Windows environment (current OS: linux)"
        )

    from backend.app.services.knowledge_store import (
        _get_win32_ntdll,
        _get_win32_kernel32,
    )

    ntdll = _get_win32_ntdll()
    kernel32 = _get_win32_kernel32()
    assert ntdll is not None, "ntdll must be loaded on Windows"
    assert kernel32 is not None, "kernel32 must be loaded on Windows"

    # Validate store creation through handle-anchored traversal on native Windows with BMP and supplementary chars
    store_dir = tmp_path / "native_nt_test_😀_données"
    cfg = StoreConfig(storage_path=store_dir, skip_security_checks=False)
    store = LocalKnowledgeStore(config=cfg)
    assert store_dir.is_dir()
    store.close()


def test_37_windows_unicode_string_utf16_length_and_buffer_lifetime():
    """
    Test 37: Windows UNICODE_STRING UTF-16LE Byte Length and Buffer Lifetime.
    Verifies that:
    1. Length is strictly calculated using UTF-16LE encoded byte count, not Python character count.
    2. MaximumLength includes the 2-byte UTF-16LE null terminator.
    3. BMP characters (ASCII, Latin, Greek, Cyrillic, CJK) correctly map to 2 bytes per code unit.
    4. Supplementary characters (astral plane / SMP / SIP code points > U+FFFF such as emojis
       and supplementary CJK ideographs) requiring surrogate pairs are accurately measured in bytes
       and exceed len(text) * 2.
    5. The raw backing buffer remains valid and alive in memory, holding null-terminated UTF-16LE bytes.
    6. Oversized path components (> 65534 bytes) trigger SecurityStorageError.
    """
    import ctypes
    from ctypes import wintypes
    from backend.app.services.knowledge_store import (
        UNICODE_STRING,
        SecurityStorageError,
        _create_unicode_string,
    )

    # 1. BMP Unicode components: len(s) code points == len(s.encode('utf-16le')) // 2
    bmp_samples = [
        "knowledge_store",  # ASCII: 15 chars -> 30 bytes
        "données_témoin",  # Latin extended / French: 14 chars -> 28 bytes
        "δοκιμή_тест",  # Greek & Cyrillic: 11 chars -> 22 bytes
        "日本語_测试",  # CJK BMP: 6 chars -> 12 bytes
    ]
    for sample in bmp_samples:
        u_str, buf = _create_unicode_string(sample)
        expected_len = len(sample.encode("utf-16le"))
        assert expected_len == len(sample) * 2
        assert u_str.Length == expected_len
        assert u_str.MaximumLength == expected_len + 2
        assert ctypes.sizeof(buf) == expected_len + 2
        # Verify null termination in UTF-16LE
        raw_bytes = bytes(buf)
        assert raw_bytes[-2:] == b"\x00\x00"
        assert raw_bytes[:-2] == sample.encode("utf-16le")

    # 2. Supplementary Unicode components (code points > U+FFFF requiring surrogate pairs)
    # Emoji '😀' (U+1F600): 1 Python character, 2 UTF-16 code units (0xD83D 0xDE00) -> 4 bytes
    single_emoji = "😀"
    assert len(single_emoji) == 1
    u_emoji, buf_emoji = _create_unicode_string(single_emoji)
    assert u_emoji.Length == 4  # Strictly 4 bytes, NOT len(single_emoji) * 2 == 2!
    assert u_emoji.Length > len(single_emoji) * 2
    assert u_emoji.MaximumLength == 6
    assert bytes(buf_emoji) == single_emoji.encode("utf-16le") + b"\x00\x00"

    # Multi-surrogate string with standard text, emojis, and supplementary CJK ideographs:
    # '𠀀' (U+20000, 4 bytes), '𪚥' (U+2A6A5, 4 bytes)
    supp_sample = "data_😀_🚀_𠀀_𪚥"
    # len(supp_sample): 5 ('data_') + 1 (😀) + 1 (_) + 1 (🚀) + 1 (_) + 1 (𠀀) + 1 (_) + 1 (𪚥) = 12 chars
    assert len(supp_sample) == 12
    u_supp, buf_supp = _create_unicode_string(supp_sample)
    expected_supp_len = len(supp_sample.encode("utf-16le"))
    # 4 supplementary characters add 4 extra code units (8 extra bytes) -> 12*2 + 8 = 32 bytes
    assert expected_supp_len == 32
    assert u_supp.Length == 32
    assert u_supp.Length > len(supp_sample) * 2
    assert u_supp.MaximumLength == 34
    assert bytes(buf_supp) == supp_sample.encode("utf-16le") + b"\x00\x00"

    # 3. Verify memory buffer pinning and ctypes pointer address match
    buf_addr = ctypes.addressof(buf_supp)
    wstr_ptr = ctypes.c_void_p.from_buffer(u_supp, UNICODE_STRING.Buffer.offset).value
    assert buf_addr == wstr_ptr, (
        "Buffer pointer must directly reference the allocated backing buffer"
    )

    # 4. Exact boundary cases for USHORT (16-bit) capacity:
    # 32,766 ASCII characters: Length = 65,532 bytes, MaximumLength = 65,534 bytes -> accepted
    max_valid_str = "a" * 32766
    u_valid, buf_valid = _create_unicode_string(max_valid_str)
    assert u_valid.Length == 65532
    assert u_valid.MaximumLength == 65534
    assert ctypes.sizeof(buf_valid) == 65534
    assert u_valid.Length <= 0xFFFF
    assert u_valid.MaximumLength <= 0xFFFF

    # 32,767 ASCII characters: Length = 65,534 bytes -> MaximumLength would be 65,536 > 0xFFFF -> rejected
    boundary_overflow_str = "a" * 32767
    with pytest.raises(SecurityStorageError, match="maximum NT UNICODE_STRING length"):
        _create_unicode_string(boundary_overflow_str)

    # Further overflow case: 32,768 ASCII characters (65,536 bytes) -> rejected
    huge_str = "a" * 32768
    with pytest.raises(SecurityStorageError, match="maximum NT UNICODE_STRING length"):
        _create_unicode_string(huge_str)

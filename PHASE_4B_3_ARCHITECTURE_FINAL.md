# Phase 4B.3 — Local Knowledge Storage and Indexing
## Final Consolidated Architecture Specification (Revision 17)

---

## 1. Executive Architecture Summary

This document specifies the complete, implementation-ready architecture for **Phase 4B.3: Local Knowledge Storage and Indexing**. It bridges the ingestion pipeline completed in Phase 4B.2 (`read_secure_file`, `chunk_document`, `compute_source_slug`, `compute_source_fingerprint`) and the retrieval pipeline in Phase 4B.4.

### Immutable Architecture Baseline
1. **SQLite (`coordinator.sqlite3`) as Canonical System of Record:**
   Canonical documents, chunks, lifecycle states, commit sequences, generation mappings, and active query snapshots are stored in an application-managed SQLite database (`coordinator.sqlite3`).
2. **Derived, Reconstructible ChromaDB Vector Index:**
   ChromaDB (`PersistentClient`) serves strictly as an approximate nearest-neighbor vector search accelerator. Candidate IDs and similarity rankings from Chroma are verified against the reader's captured snapshot and hydrated from `coordinator.sqlite3`. Chroma content and metadata never override canonical SQLite records.
3. **Strict Global Lock Hierarchy ($\mathcal{L}_1 \implies \mathcal{L}_2$):**
   Mutations acquire `.writer.lock` ($\mathcal{L}_1$) exclusively. Pointers, snapshots, and coordinator state transitions acquire `.lifecycle.lock` ($\mathcal{L}_2$) exclusively. Readers acquire $\mathcal{L}_2$ in shared mode during snapshot registration and hold OS kernel-locked pin descriptors for query execution. Deadlocks are mathematically impossible.
4. **Authoritative Global Commit Sequence:**
   A durable singleton record (`global_state.global_commit_seq`) monotonically increments inside the exact SQLite transaction that mutates the active document set or activates a new index generation.
5. **Snapshot-Isolated Retrieval Bound to Generation Providers:**
   Every retrieval query resolves an immutable tuple $(G, S, \mathcal{D}_{active}, \mathcal{P}_G)$ representing active generation $G$, global commit sequence $S$, active document IDs $\mathcal{D}_{active}$, and the exact `EmbeddingProvider` $\mathcal{P}_G$ bound to generation $G$. Vector queries apply a mandatory metadata filter `{"document_id": {"$in": active_docs}}`. Empty active sets return `[]` without calling Chroma.
6. **Relational Foreign-Key Ingestion Order:**
   First-time ingestion inserts a placeholder row in `source_catalog` with `active_document_id = NULL` before inserting dependent documents and chunks, satisfying SQLite foreign keys without exposing incomplete documents to queries.
7. **Cross-Generation Document Cleanup:**
   Document indexing is tracked relationally in `document_generation_index`. Cleanup registers durable deletion intents across all generations containing that document. Canonical records are purged only after all ChromaDB collections confirm complete chunk removal.
8. **Phase 4B.1 Schema Purity:**
   `KnowledgeDocument` and `KnowledgeChunk` schemas are 100% preserved without modification. `source_identity` is passed explicitly via application context and stored in canonical tables.
9. **Component-by-Component Filesystem Security:**
   Traversal uses file-descriptor pinning (`O_DIRECTORY | O_NOFOLLOW` on POSIX; handle-based reparse-point and local volume validation on Windows). Remote/network mounts are rejected.

---

## 2. Complete SQLite Schema

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Global state singleton: authoritative sequence counter
CREATE TABLE IF NOT EXISTS global_state (
    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'state'),
    global_commit_seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Global storage manifest: generation and embedding metadata
CREATE TABLE IF NOT EXISTS storage_manifest (
    manifest_key TEXT PRIMARY KEY CHECK (manifest_key = 'manifest'),
    active_generation INTEGER NOT NULL CHECK (active_generation >= 1),
    active_chunk_collection TEXT NOT NULL,
    staging_generation INTEGER CHECK (staging_generation IS NULL OR staging_generation > active_generation),
    staging_chunk_collection TEXT,
    staging_embedding_provider TEXT,
    staging_embedding_model TEXT,
    staging_embedding_dimension INTEGER CHECK (staging_embedding_dimension IS NULL OR staging_embedding_dimension > 0),
    staging_embedding_config_version TEXT,
    migration_state TEXT NOT NULL CHECK (migration_state IN ('IDLE', 'REBUILD_STAGING', 'MIGRATION_STAGING')),
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    embedding_config_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Relational retired generations tracking: binds provider configuration to retired generations
CREATE TABLE IF NOT EXISTS retired_generations (
    generation INTEGER PRIMARY KEY CHECK (generation >= 1),
    chunk_collection TEXT NOT NULL,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0),
    embedding_config_version TEXT NOT NULL,
    retired_at TEXT NOT NULL
);

-- Stable source catalog
CREATE TABLE IF NOT EXISTS source_catalog (
    source_identity TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    active_document_id TEXT, -- NULL during placeholder/staging state
    raw_run_id_sha256 TEXT,  -- SHA-256 of un-truncated run ID for failure history
    commit_seq INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Canonical document records
CREATE TABLE IF NOT EXISTS canonical_documents (
    document_id TEXT PRIMARY KEY,
    source_identity TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    source_ref TEXT,
    http_method TEXT,
    path_template TEXT,
    operation_id TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content_fingerprint TEXT NOT NULL,
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'retired', 'tombstoned')),
    FOREIGN KEY (source_identity) REFERENCES source_catalog(source_identity) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_docs_source ON canonical_documents(source_identity, status);

-- Canonical chunk records (Authoritative Source of Truth)
CREATE TABLE IF NOT EXISTS canonical_chunks (
    document_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    source_identity TEXT NOT NULL,
    source_type TEXT NOT NULL,
    content TEXT NOT NULL,
    http_method TEXT,
    path_template TEXT,
    operation_id TEXT,
    failure_category TEXT,
    source_ref TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (document_id, chunk_id),
    FOREIGN KEY (document_id) REFERENCES canonical_documents(document_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON canonical_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON canonical_chunks(source_identity);

-- Tracks which generations contain vectors for each document
CREATE TABLE IF NOT EXISTS document_generation_index (
    document_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (document_id, generation),
    FOREIGN KEY (document_id) REFERENCES canonical_documents(document_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_doc_gen ON document_generation_index(generation);

-- Active Reader / Query Snapshots: binds exact generation provider settings to session
CREATE TABLE IF NOT EXISTS active_snapshots (
    session_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    global_commit_seq INTEGER NOT NULL,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    embedding_config_version TEXT NOT NULL,
    snapshot_type TEXT NOT NULL CHECK (snapshot_type IN ('DOCUMENT', 'GLOBAL_QUERY')),
    target_document_id TEXT,
    created_at TEXT NOT NULL
);

-- Protected documents per active snapshot
CREATE TABLE IF NOT EXISTS snapshot_protected_documents (
    session_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    PRIMARY KEY (session_id, document_id),
    FOREIGN KEY (session_id) REFERENCES active_snapshots(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_snap_doc ON snapshot_protected_documents(document_id);

-- Cross-generation durable deletion registry
CREATE TABLE IF NOT EXISTS cleanup_registry (
    document_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    cleanup_state TEXT NOT NULL CHECK (cleanup_state IN ('RETIRED', 'TOMBSTONED', 'PURGING')),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (document_id, generation),
    FOREIGN KEY (document_id) REFERENCES canonical_documents(document_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cleanup_doc ON cleanup_registry(document_id);
CREATE INDEX IF NOT EXISTS idx_cleanup_state ON cleanup_registry(cleanup_state);
```

---

## 3. Initialization and Connection Protocol

### 3.1 Strict Connection Configuration Order
The store connection must be configured in a strict, non-transactional order before executing DDL or queries:

```python
def configure_sqlite_connection(db_path: Path) -> sqlite3.Connection:
    # 0. Runtime version assertion
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise UnsupportedRuntimeError(
            f"SQLite 3.35.0+ required for UPSERT and RETURNING; got {sqlite3.sqlite_version}"
        )

    # 1. Open database connection with isolation_level=None for explicit transaction control
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,  # Explicit transaction management
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row

    # 2. Enable and verify foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    fk_status = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    if fk_status != 1:
        raise StorageConfigurationError("Failed to enable foreign_keys in SQLite")

    # 3. Configure WAL mode OUTSIDE an active transaction
    wal_status = conn.execute("PRAGMA journal_mode = WAL;").fetchone()[0]
    if wal_status.lower() != "wal":
        raise StorageConfigurationError(f"Failed to enable WAL mode; got {wal_status}")

    # 4. Configure synchronous = FULL (2) for physical durability
    conn.execute("PRAGMA synchronous = FULL;")
    sync_status = conn.execute("PRAGMA synchronous;").fetchone()[0]
    if sync_status != 2:
        raise StorageConfigurationError(f"Failed to set PRAGMA synchronous = FULL; got {sync_status}")

    # 5. Configure busy timeout
    conn.execute("PRAGMA busy_timeout = 30000;")

    return conn
```

### 3.2 Truly Atomic Database Initialization
To eliminate Python `sqlite3.executescript()` implicit commit hazards, individual DDL statements are executed within an explicit `BEGIN IMMEDIATE ... COMMIT` transaction:

```python
INDIVIDUAL_DDL_STATEMENTS = [
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS global_state (singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'state'), global_commit_seq INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS storage_manifest (manifest_key TEXT PRIMARY KEY CHECK (manifest_key = 'manifest'), active_generation INTEGER NOT NULL CHECK (active_generation >= 1), active_chunk_collection TEXT NOT NULL, staging_generation INTEGER CHECK (staging_generation IS NULL OR staging_generation > active_generation), staging_chunk_collection TEXT, staging_embedding_provider TEXT, staging_embedding_model TEXT, staging_embedding_dimension INTEGER CHECK (staging_embedding_dimension IS NULL OR staging_embedding_dimension > 0), staging_embedding_config_version TEXT, migration_state TEXT NOT NULL CHECK (migration_state IN ('IDLE', 'REBUILD_STAGING', 'MIGRATION_STAGING')), embedding_provider TEXT NOT NULL, embedding_model TEXT NOT NULL, embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0), embedding_config_version TEXT NOT NULL, updated_at TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS retired_generations (generation INTEGER PRIMARY KEY CHECK (generation >= 1), chunk_collection TEXT NOT NULL, embedding_provider TEXT NOT NULL, embedding_model TEXT NOT NULL, embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension > 0), embedding_config_version TEXT NOT NULL, retired_at TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS source_catalog (source_identity TEXT PRIMARY KEY, source_type TEXT NOT NULL, active_document_id TEXT, raw_run_id_sha256 TEXT, commit_seq INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS canonical_documents (document_id TEXT PRIMARY KEY, source_identity TEXT NOT NULL, source_type TEXT NOT NULL, title TEXT NOT NULL, source_ref TEXT, http_method TEXT, path_template TEXT, operation_id TEXT, created_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', content_fingerprint TEXT NOT NULL, chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0), status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'retired', 'tombstoned')), FOREIGN KEY (source_identity) REFERENCES source_catalog(source_identity) ON DELETE RESTRICT);",
    "CREATE INDEX IF NOT EXISTS idx_docs_source ON canonical_documents(source_identity, status);",
    "CREATE TABLE IF NOT EXISTS canonical_chunks (document_id TEXT NOT NULL, chunk_id TEXT NOT NULL, chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0), source_identity TEXT NOT NULL, source_type TEXT NOT NULL, content TEXT NOT NULL, http_method TEXT, path_template TEXT, operation_id TEXT, failure_category TEXT, source_ref TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (document_id, chunk_id), FOREIGN KEY (document_id) REFERENCES canonical_documents(document_id) ON DELETE CASCADE);",
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc ON canonical_chunks(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_chunks_source ON canonical_chunks(source_identity);",
    "CREATE TABLE IF NOT EXISTS document_generation_index (document_id TEXT NOT NULL, generation INTEGER NOT NULL, indexed_at TEXT NOT NULL, PRIMARY KEY (document_id, generation), FOREIGN KEY (document_id) REFERENCES canonical_documents(document_id) ON DELETE CASCADE);",
    "CREATE INDEX IF NOT EXISTS idx_doc_gen ON document_generation_index(generation);",
    "CREATE TABLE IF NOT EXISTS active_snapshots (session_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, global_commit_seq INTEGER NOT NULL, embedding_provider TEXT NOT NULL, embedding_model TEXT NOT NULL, embedding_dimension INTEGER NOT NULL, embedding_config_version TEXT NOT NULL, snapshot_type TEXT NOT NULL CHECK (snapshot_type IN ('DOCUMENT', 'GLOBAL_QUERY')), target_document_id TEXT, created_at TEXT NOT NULL);",
    "CREATE TABLE IF NOT EXISTS snapshot_protected_documents (session_id TEXT NOT NULL, document_id TEXT NOT NULL, PRIMARY KEY (session_id, document_id), FOREIGN KEY (session_id) REFERENCES active_snapshots(session_id) ON DELETE CASCADE);",
    "CREATE INDEX IF NOT EXISTS idx_snap_doc ON snapshot_protected_documents(document_id);",
    "CREATE TABLE IF NOT EXISTS cleanup_registry (document_id TEXT NOT NULL, generation INTEGER NOT NULL, cleanup_state TEXT NOT NULL CHECK (cleanup_state IN ('RETIRED', 'TOMBSTONED', 'PURGING')), registered_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (document_id, generation), FOREIGN KEY (document_id) REFERENCES canonical_documents(document_id) ON DELETE CASCADE);",
    "CREATE INDEX IF NOT EXISTS idx_cleanup_doc ON cleanup_registry(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_cleanup_state ON cleanup_registry(cleanup_state);"
]

def initialize_database(conn: sqlite3.Connection, provider: Any) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        # 1. Execute DDL statements individually within explicit transaction
        for ddl in INDIVIDUAL_DDL_STATEMENTS:
            conn.execute(ddl)

        # 2. Record schema version 1
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (1, :now) ON CONFLICT(version) DO NOTHING;",
            {"now": now}
        )

        # 3. Initialize singleton row in global_state
        conn.execute(
            "INSERT INTO global_state (singleton_key, global_commit_seq, updated_at) "
            "VALUES ('state', 0, :now) ON CONFLICT(singleton_key) DO NOTHING;",
            {"now": now}
        )

        # 4. Initialize storage_manifest
        conn.execute(
            "INSERT INTO storage_manifest ("
            "  manifest_key, active_generation, active_chunk_collection, "
            "  staging_generation, staging_chunk_collection, "
            "  staging_embedding_provider, staging_embedding_model, "
            "  staging_embedding_dimension, staging_embedding_config_version, "
            "  migration_state, embedding_provider, "
            "  embedding_model, embedding_dimension, embedding_config_version, updated_at"
            ") VALUES ("
            "  'manifest', 1, 'chunks_gen_1', NULL, NULL, NULL, NULL, NULL, NULL, "
            "  'IDLE', :provider_id, :model_name, :dimension, :config_version, :now"
            ") ON CONFLICT(manifest_key) DO NOTHING;",
            {
                "provider_id": provider.provider_id,
                "model_name": provider.model_name,
                "dimension": provider.dimension,
                "config_version": provider.config_version,
                "now": now
            }
        )

        # 5. Verify singletons exist
        state_count = conn.execute("SELECT COUNT(*) FROM global_state WHERE singleton_key = 'state';").fetchone()[0]
        manifest_count = conn.execute("SELECT COUNT(*) FROM storage_manifest WHERE manifest_key = 'manifest';").fetchone()[0]
        if state_count != 1 or manifest_count != 1:
            raise CorruptedGlobalStateError("Required singletons not initialized")

        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
```

**Crash Behavior at Every Boundary:**
- Crash before `COMMIT`: SQLite rolls back the entire transaction upon recovery. The database remains empty or at its prior valid schema.
- Re-running `initialize_database` is completely idempotent; all statements use `IF NOT EXISTS` or `ON CONFLICT DO NOTHING`.

---

## 4. Lock Hierarchy and Connection Ownership

### 4.1 Global Lock Hierarchy
$$\mathcal{L}_1 \ (\texttt{.writer.lock}) \implies \mathcal{L}_2 \ (\texttt{.lifecycle.lock})$$

- $\mathcal{L}_1$ (`.writer.lock`): Acquired **exclusively** for all mutations (ingestion, replacement, deletion, migration, pruning, reconciliation, reset).
- $\mathcal{L}_2$ (`.lifecycle.lock`):
  - Acquired **shared** by readers during snapshot registration.
  - Acquired **exclusive** by writers briefly during state commit, cleanup transitions, and generation pruning.
- Lock acquisition order is strictly $\mathcal{L}_1 \implies \mathcal{L}_2$. Readers only acquire $\mathcal{L}_2$ (shared). Deadlocks are mathematically impossible.
- **Lock Ownership Contexts for Pruning:**
  1. *Standalone Public Pruning (`prune_retired_generations()`):* Acquires $\mathcal{L}_1$ (`.writer.lock EX`), then acquires $\mathcal{L}_2$ (`.lifecycle.lock EX`), executes `_prune_retired_generations_locked(conn)`, and releases them in reverse order ($\mathcal{L}_2$ then $\mathcal{L}_1$).
  2. *Migration-Triggered Pruning (`_prune_retired_generations_under_writer()`):* Called at the end of generation migration (`_execute_generation_transition()`). Because the migration workflow already owns $\mathcal{L}_1$ exclusively, it MUST NOT reacquire $\mathcal{L}_1$. It acquires $\mathcal{L}_2$ (`.lifecycle.lock EX`), delegates to `_prune_retired_generations_locked(conn)`, and releases $\mathcal{L}_2$.
  3. *Startup Reconciliation (`reconcile_startup()`):* Already owns both $\mathcal{L}_1$ and $\mathcal{L}_2$ exclusively across the entire recovery sequence. It calls `_prune_retired_generations_locked(conn)` directly with zero lock reacquisition.

### 4.2 Safe Thread-Local Connection Ownership
To prevent transaction interleaving between concurrent threads:
- The store maintains a `threading.local()` connection registry:
  ```python
  class ConnectionManager:
      def __init__(self, db_path: Path):
          self.db_path = db_path
          self._local = threading.local()

      def get_connection(self) -> sqlite3.Connection:
          if not hasattr(self._local, "conn") or self._local.conn is None:
              self._local.conn = configure_sqlite_connection(self.db_path)
          return self._local.conn

      def close_thread_connection(self) -> None:
          if hasattr(self._local, "conn") and self._local.conn is not None:
              self._local.conn.close()
              self._local.conn = None
  ```
- Each thread operates on its own dedicated SQLite connection handle. Transaction boundaries (`BEGIN IMMEDIATE`, `COMMIT`, `ROLLBACK`) are strictly isolated to the calling thread and cannot interleave.
- **Worker and Background Task Contract:** Background workers, cleanup workers (`run_cleanup_pass`), pruning tasks (`prune_retired_generations`), and startup reconciliation routines (`reconcile_startup`) follow this exact connection ownership contract by obtaining their SQLite handle via `conn = self.conn_manager.get_connection()`. A shared `self.conn` handle is never used anywhere in the store.

### 4.3 Windows Locking Adapter Contract (`LockFileEx` via `ctypes`)
- Uses Win32 `LockFileEx` / `UnlockFileEx` on a 1-byte range at offset 0.
- Layout:
  ```python
  class OVERLAPPED(ctypes.Structure):
      _fields_ = [
          ("Internal", ctypes.c_size_t),
          ("InternalHigh", ctypes.c_size_t),
          ("Offset", wintypes.DWORD),
          ("OffsetHigh", wintypes.DWORD),
          ("hEvent", wintypes.HANDLE)
      ]
  ```
- Non-blocking Probe: Calls `_LockFileEx` with flags `LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY`.
  - If returns `TRUE`: Lock was available $\implies$ dead reader (`PinStatus.STALE`). **Releases probe lock immediately** using `_UnlockFileEx` before returning `True`.
  - If returns `FALSE`: Checks `kernel32.GetLastError() in (ERROR_LOCK_VIOLATION, ERROR_SHARING_VIOLATION)` (33, 32). Returns `False` (live reader, `PinStatus.LIVE`).
  - Any other error (descriptor failure, permission error, adapter failure) raises an exception, classifying the probe as `PinStatus.UNKNOWN` (fail-closed; never treated as dead).
- Closing file handle automatically releases all held locks.

---

## 5. First Ingestion and Replacement Protocols

### 5.1 First-Time Ingestion Protocol
**Step 1: Staging Phase (under `.writer.lock EX`):**
```python
conn = self.conn_manager.get_connection()
conn.execute("BEGIN IMMEDIATE;")
try:
    # 1. Upsert placeholder in source_catalog with active_document_id = NULL
    conn.execute(
        "INSERT INTO source_catalog (source_identity, source_type, active_document_id, raw_run_id_sha256, commit_seq, created_at, updated_at) "
        "VALUES (:source_id, :source_type, NULL, :raw_run_sha, 0, :now, :now) "
        "ON CONFLICT(source_identity) DO NOTHING;",
        {"source_id": source_id, "source_type": doc.source_type.value, "raw_run_sha": raw_run_sha, "now": now}
    )
    # Validate source catalog entry matches expectations
    catalog_row = conn.execute(
        "SELECT source_type, raw_run_id_sha256, active_document_id FROM source_catalog WHERE source_identity = :source_id;",
        {"source_id": source_id}
    ).fetchone()
    if catalog_row["source_type"] != doc.source_type.value:
        raise SourceTypeMismatchError(f"Source exists with type {catalog_row['source_type']}, got {doc.source_type.value}")
    if catalog_row["raw_run_id_sha256"] != raw_run_sha:
        raise IdentityCollisionError(f"Source collision: raw run hash mismatch for {source_id}")

    # 2. Insert canonical document with status 'pending'
    cursor = conn.execute(
        "INSERT INTO canonical_documents (document_id, source_identity, source_type, title, source_ref, http_method, "
        "path_template, operation_id, created_at, metadata_json, content_fingerprint, chunk_count, status) "
        "VALUES (:doc_id, :source_id, :source_type, :title, :source_ref, :http_method, :path_template, :operation_id, "
        ":created_at, :metadata_json, :fingerprint, :chunk_count, 'pending');",
        doc_params
    )
    if cursor.rowcount != 1:
        raise StorageTransactionError("Failed to insert pending canonical document")

    # 3. Batch insert canonical chunks
    conn.executemany("INSERT INTO canonical_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);", chunk_tuples)

    # 4. Record generation mapping
    conn.execute(
        "INSERT INTO document_generation_index (document_id, generation, indexed_at) VALUES (:doc_id, :gen, :now);",
        {"doc_id": doc.document_id, "gen": active_gen, "now": now}
    )
    conn.execute("COMMIT;")
except Exception:
    conn.execute("ROLLBACK;")
    raise
```

*ChromaDB Indexing:* Chunks embedded and inserted into ChromaDB `chunks_gen_<G>` using storage keys `format_chroma_id(doc_id, chunk_id)`.

**Step 2: Commit Boundary (under `.lifecycle.lock EX`):**
```python
conn.execute("BEGIN IMMEDIATE;")
try:
    # 1. Increment global sequence
    cursor = conn.execute(
        "UPDATE global_state SET global_commit_seq = global_commit_seq + 1, updated_at = :now WHERE singleton_key = 'state';",
        {"now": now}
    )
    if cursor.rowcount != 1:
        raise CorruptedGlobalStateError("global_state singleton missing during commit")

    # 2. Activate in source_catalog conditionally (only if active_document_id IS NULL)
    cursor = conn.execute(
        "UPDATE source_catalog SET active_document_id = :doc_id, "
        "commit_seq = (SELECT global_commit_seq FROM global_state WHERE singleton_key = 'state'), updated_at = :now "
        "WHERE source_identity = :source_id AND active_document_id IS NULL;",
        {"doc_id": doc.document_id, "now": now, "source_id": source_id}
    )
    if cursor.rowcount != 1:
        raise ConcurrencyConflictError("Source active_document_id changed concurrently during initial ingestion")

    # 3. Transition document status to active conditionally
    cursor = conn.execute(
        "UPDATE canonical_documents SET status = 'active' WHERE document_id = :doc_id AND status = 'pending';",
        {"doc_id": doc.document_id}
    )
    if cursor.rowcount != 1:
        raise ConcurrencyConflictError("Document status modified concurrently during initial ingestion")

    conn.execute("COMMIT;")
except Exception:
    conn.execute("ROLLBACK;")
    raise
```

### 5.2 Document Replacement Protocol ($D_{old} \to D_{new}$)
**Step 1: Staging Phase (under `.writer.lock EX`):**
Inserts $D_{new}$ into `canonical_documents` as `'pending'`, inserts chunks into `canonical_chunks`, records `document_generation_index (D_new, active_gen)`. Inserts into ChromaDB `chunks_gen_<G>`.

**Step 2: Commit Boundary (under `.lifecycle.lock EX`):**
```python
conn.execute("BEGIN IMMEDIATE;")
try:
    # 1. Increment global sequence
    cursor = conn.execute(
        "UPDATE global_state SET global_commit_seq = global_commit_seq + 1, updated_at = :now WHERE singleton_key = 'state';",
        {"now": now}
    )
    if cursor.rowcount != 1:
        raise CorruptedGlobalStateError("global_state singleton missing during replacement commit")

    # 2. Atomically switch active pointer conditionally on D_old still being active
    cursor = conn.execute(
        "UPDATE source_catalog SET active_document_id = :d_new, "
        "commit_seq = (SELECT global_commit_seq FROM global_state WHERE singleton_key = 'state'), updated_at = :now "
        "WHERE source_identity = :source_id AND active_document_id = :d_old;",
        {"d_new": d_new, "d_old": d_old, "source_id": source_id, "now": now}
    )
    if cursor.rowcount != 1:
        raise ConcurrencyConflictError(f"Document replacement race: {d_old} is no longer active for source {source_id}")

    # 3. Transition D_new from 'pending' to 'active'
    cursor = conn.execute(
        "UPDATE canonical_documents SET status = 'active' WHERE document_id = :d_new AND status = 'pending';",
        {"d_new": d_new}
    )
    if cursor.rowcount != 1:
        raise StorageTransactionError("Failed to activate new document version")

    # 4. Transition D_old from 'active' to 'retired'
    cursor = conn.execute(
        "UPDATE canonical_documents SET status = 'retired' WHERE document_id = :d_old AND status = 'active';",
        {"d_old": d_old}
    )
    if cursor.rowcount != 1:
        raise StorageTransactionError("Failed to retire old document version")

    # 5. Register D_old for cross-generation cleanup
    conn.execute(
        "INSERT INTO cleanup_registry (document_id, generation, cleanup_state, registered_at, updated_at) "
        "SELECT :d_old, generation, 'RETIRED', :now, :now "
        "FROM document_generation_index WHERE document_id = :d_old;",
        {"d_old": d_old, "now": now}
    )
    conn.execute("COMMIT;")
except Exception:
    conn.execute("ROLLBACK;")
    raise
```

---

## 6. Deletion and Cleanup Protocols

### 6.1 `delete_document` Protocol
Under `.writer.lock EX` and `.lifecycle.lock EX`:
```python
conn.execute("BEGIN IMMEDIATE;")
try:
    doc_row = conn.execute(
        "SELECT source_identity, status FROM canonical_documents WHERE document_id = :doc_id;",
        {"doc_id": doc_id}
    ).fetchone()
    if not doc_row or doc_row["status"] in ("retired", "tombstoned"):
        conn.execute("COMMIT;")
        return False # Idempotent: already inactive or absent

    source_id = doc_row["source_identity"]

    # 1. Increment global sequence
    cursor = conn.execute(
        "UPDATE global_state SET global_commit_seq = global_commit_seq + 1, updated_at = :now WHERE singleton_key = 'state';",
        {"now": now}
    )
    if cursor.rowcount != 1:
        raise CorruptedGlobalStateError("global_state singleton missing during deletion")

    # 2. Clear active pointer conditionally on doc_id being the active pointer
    cursor = conn.execute(
        "UPDATE source_catalog SET active_document_id = NULL, "
        "commit_seq = (SELECT global_commit_seq FROM global_state WHERE singleton_key = 'state'), updated_at = :now "
        "WHERE source_identity = :source_id AND active_document_id = :doc_id;",
        {"source_id": source_id, "doc_id": doc_id, "now": now}
    )
    if cursor.rowcount != 1:
        raise ConcurrencyConflictError("Document is no longer the active pointer for source")

    # 3. Transition document to retired
    cursor = conn.execute(
        "UPDATE canonical_documents SET status = 'retired' WHERE document_id = :doc_id AND status = 'active';",
        {"doc_id": doc_id}
    )
    if cursor.rowcount != 1:
        raise StorageTransactionError("Failed to mark document retired")

    # 4. Register cross-generation cleanup
    conn.execute(
        "INSERT INTO cleanup_registry (document_id, generation, cleanup_state, registered_at, updated_at) "
        "SELECT :doc_id, generation, 'RETIRED', :now, :now "
        "FROM document_generation_index WHERE document_id = :doc_id;",
        {"doc_id": doc_id, "now": now}
    )
    conn.execute("COMMIT;")
    return True
except Exception:
    conn.execute("ROLLBACK;")
    raise
```

### 6.2 `delete_source` Protocol
Under `.writer.lock EX` and `.lifecycle.lock EX`:
Queries `source_catalog` for `source_identity`. If `active_document_id IS NULL`, returns `False`. If `active_document_id` is $D$, invokes the exact conditional deletion transaction from Section 6.1. The `source_catalog` entry is retained with `active_document_id = NULL` as a durable historical anchor.

### 6.3 Cleanup State Machine and Worker
The cleanup worker operates under `.writer.lock EX` and `.lifecycle.lock EX`. Because OS-level kernel file locks (`flock` on POSIX and `LockFileEx` on Windows) are not reentrant within a process or handle, the architecture strictly separates the public entrypoint from the internal locked implementation to eliminate nested lock acquisition:
- **`run_cleanup_pass(self)` (Public):** Acquires `.writer.lock EX` and `.lifecycle.lock EX` exactly once, obtains the thread-local connection handle, and delegates to the internal locked routine.
- **`_run_cleanup_pass_locked(self, conn)` (Internal):** Assumes the caller already holds `.writer.lock EX` and `.lifecycle.lock EX`. Never acquires or releases locks, preventing reentrant deadlock during startup reconciliation. Every coupled transition verifies `rowcount == 1` or rolls back.

```python
def _run_cleanup_pass_locked(self, conn: sqlite3.Connection) -> None:
    """Internal cleanup pass executed under writer lock and lifecycle exclusive lock.

    Assumes caller already holds .writer.lock EX and .lifecycle.lock EX.
    Does NOT attempt to acquire or release locks, preventing nested lock deadlocks on non-reentrant OS file locks.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Transition 1: RETIRED -> TOMBSTONED
    candidates = conn.execute(
        "SELECT document_id, generation FROM cleanup_registry WHERE cleanup_state = 'RETIRED';"
    ).fetchall()
    for row in candidates:
        doc_id, gen = row["document_id"], row["generation"]
        # Check if protected by active snapshot
        is_protected = conn.execute(
            "SELECT 1 FROM snapshot_protected_documents WHERE document_id = :doc_id;",
            {"doc_id": doc_id}
        ).fetchone()
        if not is_protected:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                c1 = conn.execute(
                    "UPDATE cleanup_registry SET cleanup_state = 'TOMBSTONED', updated_at = :now "
                    "WHERE document_id = :doc_id AND generation = :gen AND cleanup_state = 'RETIRED';",
                    {"doc_id": doc_id, "gen": gen, "now": now}
                )
                if c1.rowcount != 1:
                    conn.execute("ROLLBACK;")
                    continue

                # Coupled update: canonical_documents transitions to tombstoned
                c2 = conn.execute(
                    "UPDATE canonical_documents SET status = 'tombstoned' "
                    "WHERE document_id = :doc_id AND status IN ('retired', 'tombstoned');",
                    {"doc_id": doc_id}
                )
                if c2.rowcount != 1:
                    conn.execute("ROLLBACK;")
                    continue

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    # Transition 2: TOMBSTONED -> PURGING -> COMPLETED (Chroma deletion)
    tombstoned = conn.execute(
        "SELECT document_id, generation, cleanup_state FROM cleanup_registry WHERE cleanup_state IN ('TOMBSTONED', 'PURGING');"
    ).fetchall()
    for row in tombstoned:
        doc_id, gen, state = row["document_id"], row["generation"], row["cleanup_state"]

        if state == "TOMBSTONED":
            conn.execute("BEGIN IMMEDIATE;")
            try:
                c_purg = conn.execute(
                    "UPDATE cleanup_registry SET cleanup_state = 'PURGING', updated_at = :now "
                    "WHERE document_id = :doc_id AND generation = :gen AND cleanup_state = 'TOMBSTONED';",
                    {"doc_id": doc_id, "gen": gen, "now": now}
                )
                if c_purg.rowcount != 1:
                    conn.execute("ROLLBACK;")
                    continue
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        elif state == "PURGING":
            # Idempotent retry: row was already PURGING from prior interrupted run
            pass

        # ChromaDB Deletion
        collection_name = f"chunks_gen_{gen}"
        collection_exists = collection_name in [c.name for c in self.chroma_client.list_collections()]
        if collection_exists:
            col = self.chroma_client.get_collection(collection_name)
            col.delete(where={"document_id": doc_id})
            # Verify 0 chunks remain using ChromaDB 0.5.5 supported get() API
            remaining = col.get(where={"document_id": doc_id}, limit=1)
            if len(remaining["ids"]) > 0:
                continue # Retry next pass

        # Mark COMPLETED for this generation: atomic removal of cleanup_registry and document_generation_index
        conn.execute("BEGIN IMMEDIATE;")
        try:
            cursor = conn.execute(
                "DELETE FROM cleanup_registry WHERE document_id = :doc_id AND generation = :gen AND cleanup_state = 'PURGING';",
                {"doc_id": doc_id, "gen": gen}
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK;")
                continue

            # Explicitly delete generation index entry for this generation
            conn.execute(
                "DELETE FROM document_generation_index WHERE document_id = :doc_id AND generation = :gen;",
                {"doc_id": doc_id, "gen": gen}
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    # Transition 3: Final Canonical Purge
    purgeable_docs = conn.execute(
        "SELECT document_id FROM canonical_documents "
        "WHERE status = 'tombstoned' "
        "  AND NOT EXISTS (SELECT 1 FROM cleanup_registry WHERE document_id = canonical_documents.document_id) "
        "  AND NOT EXISTS (SELECT 1 FROM snapshot_protected_documents WHERE document_id = canonical_documents.document_id);"
    ).fetchall()
    for row in purgeable_docs:
        p_id = row["document_id"]
        conn.execute("BEGIN IMMEDIATE;")
        try:
            # Explicitly verify zero orphaned cleanup records exist for this document
            has_cleanup = conn.execute(
                "SELECT 1 FROM cleanup_registry WHERE document_id = :doc_id;", {"doc_id": p_id}
            ).fetchone()
            if has_cleanup:
                conn.execute("ROLLBACK;")
                continue

            # Explicitly delete any remaining generation index rows and canonical chunks
            conn.execute("DELETE FROM document_generation_index WHERE document_id = :doc_id;", {"doc_id": p_id})
            conn.execute("DELETE FROM canonical_chunks WHERE document_id = :doc_id;", {"doc_id": p_id})

            cursor = conn.execute(
                "DELETE FROM canonical_documents "
                "WHERE document_id = :doc_id AND status = 'tombstoned' "
                "  AND NOT EXISTS (SELECT 1 FROM snapshot_protected_documents WHERE document_id = :doc_id);",
                {"doc_id": p_id}
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK;")
                continue

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise


def run_cleanup_pass(self) -> None:
    """Public cleanup entrypoint: acquires writer lock and lifecycle lock exactly once."""
    self._lock_manager.acquire_writer_lock()
    try:
        self._lock_manager.acquire_lifecycle_exclusive()
        try:
            conn = self.conn_manager.get_connection()
            self._run_cleanup_pass_locked(conn)
        finally:
            self._lock_manager.release_lifecycle_exclusive()
    finally:
        self._lock_manager.release_writer_lock()
```

---

## 7. Reader Snapshot and Query Lifecycle

### 7.1 Snapshot Registration Protocol
Binds the exact generation provider settings to the session:

```python
def acquire_query_session(self) -> QueryReaderSession:
    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    pin_path = self.config.storage_path / "pins" / f"{session_id}.pin"
    pin_fd = -1
    snapshot_committed = False

    try:
        # Step 1: Acquire lifecycle lock shared
        self._lock_manager.acquire_lifecycle_shared()
        try:
            # Step 2: Create pin file securely
            pin_fd = os.open(pin_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            # Step 3: Acquire OS kernel shared lock on descriptor
            self._lock_manager.acquire_pin_shared(pin_fd)

            # Step 4: Begin SQLite transaction and capture state consistently
            conn = self.conn_manager.get_connection()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                manifest_row = conn.execute(
                    "SELECT active_generation, embedding_provider, embedding_model, embedding_dimension, embedding_config_version "
                    "FROM storage_manifest WHERE manifest_key = 'manifest';"
                ).fetchone()
                active_gen = manifest_row["active_generation"]
                p_id = manifest_row["embedding_provider"]
                m_name = manifest_row["embedding_model"]
                dim = manifest_row["embedding_dimension"]
                cfg_v = manifest_row["embedding_config_version"]

                seq = conn.execute(
                    "SELECT global_commit_seq FROM global_state WHERE singleton_key = 'state';"
                ).fetchone()[0]

                active_docs = [
                    r[0] for r in conn.execute(
                        "SELECT active_document_id FROM source_catalog WHERE active_document_id IS NOT NULL;"
                    ).fetchall()
                ]

                # Step 5: Register snapshot in SQLite atomically
                conn.execute(
                    "INSERT INTO active_snapshots ("
                    "  session_id, generation, global_commit_seq, "
                    "  embedding_provider, embedding_model, embedding_dimension, embedding_config_version, "
                    "  snapshot_type, target_document_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, 'GLOBAL_QUERY', NULL, ?);",
                    (session_id, active_gen, seq, p_id, m_name, dim, cfg_v, datetime.now(timezone.utc).isoformat())
                )
                if active_docs:
                    conn.executemany(
                        "INSERT INTO snapshot_protected_documents (session_id, document_id) VALUES (?, ?);",
                        [(session_id, d) for d in active_docs]
                    )
                conn.execute("COMMIT;")
                snapshot_committed = True
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        finally:
            # Step 6: Release lifecycle shared lock
            self._lock_manager.release_lifecycle_shared()

        # Step 7: Resolve exact provider instance bound to this generation
        session_provider = self._resolve_provider(p_id, m_name, dim, cfg_v)

        return QueryReaderSession(
            store=self,
            session_id=session_id,
            generation=active_gen,
            global_commit_seq=seq,
            captured_active_doc_ids=frozenset(active_docs),
            embedding_provider=session_provider,
            pin_fd=pin_fd,
            pin_path=pin_path
        )
    except Exception:
        # Step 8: Failure recovery path
        # If SQLite snapshot was committed, rollback the database rows under lifecycle lock FIRST
        if snapshot_committed:
            try:
                self._lock_manager.acquire_lifecycle_shared()
                try:
                    conn = self.conn_manager.get_connection()
                    conn.execute("BEGIN IMMEDIATE;")
                    conn.execute("DELETE FROM snapshot_protected_documents WHERE session_id = ?;", (session_id,))
                    conn.execute("DELETE FROM active_snapshots WHERE session_id = ?;", (session_id,))
                    conn.execute("COMMIT;")
                    snapshot_committed = False
                except Exception:
                    conn.execute("ROLLBACK;")
                    # If SQLite cleanup fails here, leave pin_fd and pin_path intact on disk!
                    # Do NOT unlink pin if snapshot remains in DB:
                    # Startup reconciliation will detect the orphaned pin and clean up both together.
                    raise
                finally:
                    self._lock_manager.release_lifecycle_shared()
            except Exception:
                raise

        # Only release and unlink pin descriptor if database snapshot does not exist
        if not snapshot_committed and pin_fd != -1:
            try:
                self._lock_manager.release_pin(pin_fd)
            except Exception:
                pass
            try:
                os.close(pin_fd)
            except Exception:
                pass
            try:
                os.unlink(pin_path)
            except Exception:
                pass
        raise

### 7.2 Session Release Protocol
```python
class QueryReaderSession:
    """Represents an active, snapshot-pinned reader session."""
    def __init__(
        self,
        store: Any,
        session_id: str,
        generation: int,
        global_commit_seq: int,
        captured_active_doc_ids: FrozenSet[str],
        embedding_provider: Any,
        pin_fd: int,
        pin_path: Path
    ):
        self.store = store
        self.session_id = session_id
        self.generation = generation
        self.global_commit_seq = global_commit_seq
        self.captured_active_doc_ids = captured_active_doc_ids
        self.embedding_provider = embedding_provider
        self.pin_fd = pin_fd
        self.pin_path = pin_path
        self._db_cleaned = False
        self._pin_released = False

    @property
    def is_active(self) -> bool:
        return not (self._db_cleaned and self._pin_released)

    def close(self) -> None:
        self.store.release_query_session(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def release_query_session(self, session: QueryReaderSession) -> None:
    # Phase 1: Database cleanup (retriable if failed)
    if not session._db_cleaned:
        self._lock_manager.acquire_lifecycle_shared()
        try:
            conn = self.conn_manager.get_connection()
            conn.execute("BEGIN IMMEDIATE;")
            # Explicitly delete protected documents and active snapshot
            conn.execute("DELETE FROM snapshot_protected_documents WHERE session_id = ?;", (session.session_id,))
            cursor = conn.execute("DELETE FROM active_snapshots WHERE session_id = ?;", (session.session_id,))
            conn.execute("COMMIT;")
            session._db_cleaned = True
        except Exception:
            conn.execute("ROLLBACK;")
            # CRITICAL: Do NOT release or unlink kernel pin if database cleanup fails!
            # The pin MUST remain active so that:
            # 1. Background pruning cannot drop the retired generation while this snapshot might linger.
            # 2. The caller can safely retry session.close().
            # 3. If the process crashes, startup reconciliation will clean up both pin and snapshot together.
            raise
        finally:
            self._lock_manager.release_lifecycle_shared()

    # Phase 2: OS Kernel Pin Release (only executes once Phase 1 has committed successfully)
    if not session._pin_released:
        try:
            self._lock_manager.release_pin(session.pin_fd)
        finally:
            os.close(session.pin_fd)
            try:
                os.unlink(session.pin_path)
            except FileNotFoundError:
                pass
            session._pin_released = True
```

---

## 8. Chroma Indexing and Canonical Hydration

### 8.1 Chroma Internal Metadata Contract and Injective ID Encoding

#### A. Internal Metadata Contract (Allowlist)
ChromaDB serves solely as an approximate nearest-neighbor vector search accelerator. To satisfy ChromaDB's where-clause filtering constraints without data corruption, metadata attached to Chroma vector records is strictly restricted to an allowlist of primitive types (`str` and `int`). Complex objects, nested dictionaries, lists, or `None` values are strictly forbidden in Chroma metadata.

| Field Name | Type | Purpose | Example Value |
|---|---|---|---|
| `document_id` | `str` | Canonical document ID. Used for `$in` active document filtering and joins. | `"doc-doc-guide_md_a1b2c3d4-112233445566"` |
| `chunk_id` | `str` | Canonical chunk ID. Used for unique storage key identification. | `"chk-doc-guide_md_a1b2-0000-deadbeef"` |
| `chunk_index` | `int` | Zero-based sequential position of chunk within the document. | `0` |
| `source_identity` | `str` | Stable source identity string formatted as `f"{source_type}:{slug}"`. | `"api_documentation:docs/guide.md"` |
| `source_type` | `str` | String representation of `KnowledgeSourceType` value. Used for source-type filtering. | `"api_documentation"` |
| `generation` | `int` | Vector index generation number. Tracks vector provenance. | `1` |

#### B. Separation from Phase 4B.1 User Metadata
- **Chroma Metadata:** Contains strictly the 6 internal indexing fields above. It never stores Phase 4B.1 user metadata, payload text, or document attributes.
- **Phase 4B.1 User Metadata (`DocumentMetadata`, `ChunkMetadata`):** Stored with 100% fidelity in SQLite tables (`canonical_documents.metadata_json` and `canonical_chunks.metadata_json`) as JSON strings, preserving timestamps, nested structures, UUIDs, and custom fields.
- **Hydration Invariant:** ChromaDB vector queries return candidate IDs only. The retrieval pipeline (Section 8.2) verifies candidate IDs against the reader snapshot and hydrates the actual text, HTTP attributes, and rich Phase 4B.1 metadata directly from SQLite `canonical_chunks`. Chroma metadata is never returned to callers.

#### C. Injective Collision-Safe Chroma Storage ID Encoding
$$\text{storage\_id} = f"{len(document\_id)}:{document\_id}:{chunk\_id}"$$

```python
def format_chroma_id(document_id: str, chunk_id: str) -> str:
    return f"{len(document_id)}:{document_id}:{chunk_id}"

def parse_chroma_id(storage_id: str) -> Tuple[str, str]:
    prefix, sep, rest = storage_id.partition(":")
    if not sep or not prefix.isdigit():
        raise CorruptedStorageIdError(f"Invalid storage_id format: {storage_id}")
    doc_len = int(prefix)
    if len(rest) < doc_len + 1 or rest[doc_len] != ":":
        raise CorruptedStorageIdError(f"Malformed storage_id length prefix: {storage_id}")
    doc_id = rest[:doc_len]
    chunk_id = rest[doc_len + 1:]
    return doc_id, chunk_id
```

### 8.2 Query and Canonical Hydration Pipeline
Always uses the session's bound embedding provider and hydrates text/metadata from SQLite:

```python
def query_chunks(
    self,
    query_text: str,
    top_k: int = 5,
    source_type: Optional[KnowledgeSourceType] = None,
    session: Optional[QueryReaderSession] = None
) -> List[KnowledgeChunk]:
    if session is not None:
        if session.store is not self:
            raise InvalidSessionError("Session belongs to another store instance")
        if not session.is_active:
            raise ClosedSessionError("Query session is closed")

    managed_session = None
    if session is None:
        managed_session = self.acquire_query_session()
        s = managed_session
    else:
        s = session

    try:
        # Fast path: Empty active document set
        if not s.captured_active_doc_ids:
            return []

        # 1. Embed query using session's bound provider (NOT store's active provider)
        query_vector = s.embedding_provider.embed_text(query_text)

        # 2. Vector search in ChromaDB pinned generation
        collection_name = f"chunks_gen_{s.generation}"
        col = self.chroma_client.get_collection(collection_name)
        filter_dict = {"document_id": {"$in": list(s.captured_active_doc_ids)}}
        if source_type is not None:
            filter_dict = {"$and": [filter_dict, {"source_type": source_type.value}]}

        results = col.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            where=filter_dict
        )

        candidate_ids = results["ids"][0] if results and results["ids"] else []
        if not candidate_ids:
            return []

        # 3. Parse and filter candidate IDs
        valid_pairs = []
        ordered_keys = []
        for sid in candidate_ids:
            doc_id, chunk_id = parse_chroma_id(sid)
            if doc_id in s.captured_active_doc_ids:
                valid_pairs.append((doc_id, chunk_id))
                ordered_keys.append((doc_id, chunk_id))

        if not valid_pairs:
            return []

        # 4. Canonical SQLite Hydration
        conn = self.conn_manager.get_connection()
        placeholders = ", ".join(["(?, ?)"] * len(valid_pairs))
        flat_params = [item for pair in valid_pairs for item in pair]
        sql = (
            "SELECT document_id, chunk_id, chunk_index, source_identity, source_type, content, "
            "http_method, path_template, operation_id, failure_category, source_ref, metadata_json "
            f"FROM canonical_chunks WHERE (document_id, chunk_id) IN (VALUES {placeholders});"
        )
        rows = conn.execute(sql, flat_params).fetchall()
        row_map = {(r["document_id"], r["chunk_id"]): r for r in rows}

        # 5. Build hydrated KnowledgeChunk instances preserving Chroma similarity ranking
        hydrated_chunks = []
        for pair in ordered_keys:
            if pair not in row_map:
                raise CorruptedIndexIntegrityError(f"Chroma returned chunk missing from canonical storage: {pair}")
            r = row_map[pair]
            chunk = KnowledgeChunk(
                chunk_id=r["chunk_id"],
                document_id=r["document_id"],
                content=r["content"],
                chunk_index=r["chunk_index"],
                source_type=KnowledgeSourceType(r["source_type"]),
                http_method=r["http_method"],
                path_template=r["path_template"],
                operation_id=r["operation_id"],
                failure_category=r["failure_category"],
                source_ref=r["source_ref"],
                metadata=json.loads(r["metadata_json"])
            )
            hydrated_chunks.append(chunk)

        return hydrated_chunks
    finally:
        if managed_session is not None:
            self.release_query_session(managed_session)
```

---

## 9. Rebuild and Migration Protocols

### 9.1 Ordered 18-Step Protocol

```python
def _execute_generation_transition(self, is_migration: bool, target_provider: Optional[Any] = None) -> MigrationResult:
    # Step 1: Acquire writer lock exclusive
    self._lock_manager.acquire_writer_lock()
    try:
        conn = self.conn_manager.get_connection()
        # Step 2: Validate manifest and resolve providers
        manifest = conn.execute("SELECT * FROM storage_manifest WHERE manifest_key = 'manifest';").fetchone()
        if manifest["migration_state"] != "IDLE":
            raise InvalidManifestStateError(f"Cannot start generation transition in state {manifest['migration_state']}")

        active_gen = manifest["active_generation"]
        if is_migration:
            intent_state = "MIGRATION_STAGING"
            p_id = target_provider.provider_id
            m_name = target_provider.model_name
            dim = target_provider.dimension
            cfg_v = target_provider.config_version
            provider = target_provider
        else:
            intent_state = "REBUILD_STAGING"
            p_id = manifest["embedding_provider"]
            m_name = manifest["embedding_model"]
            dim = manifest["embedding_dimension"]
            cfg_v = manifest["embedding_config_version"]
            provider = self._resolve_provider(p_id, m_name, dim, cfg_v)

        # Step 3: Calculate staging generation
        stg_gen = active_gen + 1
        stg_col_name = f"chunks_gen_{stg_gen}"
        now = datetime.now(timezone.utc).isoformat()

        # Step 4: Persist staging intent in SQLite first
        conn.execute("BEGIN IMMEDIATE;")
        try:
            cursor = conn.execute(
                "UPDATE storage_manifest SET "
                "staging_generation = :stg_gen, staging_chunk_collection = :stg_col, "
                "staging_embedding_provider = :p_id, staging_embedding_model = :m_name, "
                "staging_embedding_dimension = :dim, staging_embedding_config_version = :cfg_v, "
                "migration_state = :intent_state, updated_at = :now "
                "WHERE manifest_key = 'manifest' AND migration_state = 'IDLE';",
                {"stg_gen": stg_gen, "stg_col": stg_col_name, "p_id": p_id, "m_name": m_name,
                 "dim": dim, "cfg_v": cfg_v, "intent_state": intent_state, "now": now}
            )
            if cursor.rowcount != 1:
                raise StorageTransactionError("Failed to persist staging intent in storage_manifest")
            # Step 5: Verify staging-intent transaction committed
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

        # Step 6: Create Chroma staging collection
        if stg_col_name in [c.name for c in self.chroma_client.list_collections()]:
            self.chroma_client.delete_collection(stg_col_name)
        stg_col = self.chroma_client.create_collection(
            name=stg_col_name,
            metadata={"generation": stg_gen, "provider": p_id, "model": m_name, "dimension": dim}
        )

        # Step 7: Populate from canonical SQLite documents and chunks
        active_docs = conn.execute(
            "SELECT active_document_id FROM source_catalog WHERE active_document_id IS NOT NULL;"
        ).fetchall()
        expected_ids = set()

        for doc_row in active_docs:
            d_id = doc_row["active_document_id"]
            chunks = conn.execute(
                "SELECT chunk_id, chunk_index, content, source_type, source_identity FROM canonical_chunks WHERE document_id = ?;",
                (d_id,)
            ).fetchall()

            storage_ids = [format_chroma_id(d_id, c["chunk_id"]) for c in chunks]
            texts = [c["content"] for c in chunks]
            metas = [{
                "document_id": d_id,
                "chunk_id": c["chunk_id"],
                "chunk_index": c["chunk_index"],
                "source_identity": c["source_identity"],
                "source_type": c["source_type"],
                "generation": stg_gen
            } for c in chunks]

            embeddings = provider.embed_batch(texts)
            stg_col.add(ids=storage_ids, embeddings=embeddings, metadatas=metas)
            expected_ids.update(storage_ids)

            # Step 8: Record generation mappings safely
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                "INSERT INTO document_generation_index (document_id, generation, indexed_at) "
                "VALUES (?, ?, ?) ON CONFLICT(document_id, generation) DO NOTHING;",
                (d_id, stg_gen, now)
            )
            conn.execute("COMMIT;")

        # Step 9: Verify exact expected-versus-actual document/chunk ID sets
        actual_get = stg_col.get(include=["metadatas"])
        actual_ids = set(actual_get["ids"])
        if actual_ids != expected_ids:
            raise MigrationVerificationError(f"ID mismatch: expected {len(expected_ids)}, got {len(actual_ids)}")

        # Step 10: Verify vector dimensions and finite values
        sample = stg_col.get(limit=5, include=["embeddings"])
        if sample["embeddings"]:
            for vec in sample["embeddings"]:
                if len(vec) != dim or any(not math.isfinite(x) for x in vec):
                    raise MigrationVerificationError("Invalid vector dimension or non-finite values detected")

        # Step 11: Atomically activate new generation in SQLite
        self._lock_manager.acquire_lifecycle_exclusive()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            # Step 12: Increment global commit sequence
            c1 = conn.execute(
                "UPDATE global_state SET global_commit_seq = global_commit_seq + 1, updated_at = :now WHERE singleton_key = 'state';",
                {"now": now}
            )
            if c1.rowcount != 1:
                raise CorruptedGlobalStateError("global_state singleton missing during activation")

            # Step 13: Register previous generation in retired_generations (with its provider metadata)
            conn.execute(
                "INSERT INTO retired_generations (generation, chunk_collection, embedding_provider, embedding_model, embedding_dimension, embedding_config_version, retired_at) "
                "VALUES (:gen, :col, :p_id, :m_name, :dim, :cfg_v, :now);",
                {"gen": active_gen, "col": f"chunks_gen_{active_gen}", "p_id": manifest["embedding_provider"],
                 "m_name": manifest["embedding_model"], "dim": manifest["embedding_dimension"],
                 "cfg_v": manifest["embedding_config_version"], "now": now}
            )

            # Step 14: Clear staging fields and update active generation in storage_manifest
            c2 = conn.execute(
                "UPDATE storage_manifest SET "
                "active_generation = :stg_gen, active_chunk_collection = :stg_col, "
                "staging_generation = NULL, staging_chunk_collection = NULL, "
                "staging_embedding_provider = NULL, staging_embedding_model = NULL, "
                "staging_embedding_dimension = NULL, staging_embedding_config_version = NULL, "
                "migration_state = 'IDLE', embedding_provider = :p_id, "
                "embedding_model = :m_name, embedding_dimension = :dim, "
                "embedding_config_version = :cfg_v, updated_at = :now "
                "WHERE manifest_key = 'manifest';",
                {"stg_gen": stg_gen, "stg_col": stg_col_name, "p_id": p_id, "m_name": m_name,
                 "dim": dim, "cfg_v": cfg_v, "now": now}
            )
            if c2.rowcount != 1:
                raise StorageTransactionError("Failed to update active storage_manifest on activation")

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            self._lock_manager.release_lifecycle_exclusive()

        # Step 15: New readers immediately use new generation.
        # Step 16: Old generation retained while snapshots reference it.
        # Step 17: Deferred drop of old Chroma collection under already-held writer lock.
        self._prune_retired_generations_under_writer()

        return MigrationResult(
            from_generation=active_gen,
            to_generation=stg_gen,
            documents_migrated=len(active_docs),
            chunks_migrated=len(expected_ids),
            new_provider_id=p_id
        )
    finally:
        self._lock_manager.release_writer_lock()
```

### 9.2 Executable Startup Reconciliation Procedure
Executed deterministically on `LocalKnowledgeStore` initialization or recovery before admitting any readers or writers.

#### A. Lock Hierarchy and Connection Ownership
1. Acquire `.writer.lock` exclusively ($\mathcal{L}_1$).
2. Acquire `.lifecycle.lock` exclusively ($\mathcal{L}_2$).
3. Obtain connection via `conn = self.conn_manager.get_connection()`.
4. Ensure PRAGMAs: `foreign_keys = ON`, `journal_mode = WAL`, `synchronous = FULL`.
5. Non-reentrant lock safety: `reconcile_startup()` delegates sub-tasks to internal locked routines (`_run_cleanup_pass_locked`, `_prune_retired_generations_locked`) without releasing or re-acquiring `.lifecycle.lock`, preventing self-deadlock.

#### B. Executable Algorithm Pseudocode
```python
def reconcile_startup(self) -> ReconciliationReport:
    now = datetime.now(timezone.utc).isoformat()
    self._lock_manager.acquire_writer_lock()
    try:
        self._lock_manager.acquire_lifecycle_exclusive()
        try:
            conn = self.conn_manager.get_connection()

            # -------------------------------------------------------------
            # Phase 1: Recover Stale Reader Pins and Orphaned Snapshots (Fail-Closed)
            # -------------------------------------------------------------
            pins_dir = self.config.storage_path / "pins"
            stale_sessions = set()
            preserved_sessions = set()

            if pins_dir.exists():
                for pin_file in list(pins_dir.glob("*.pin")):
                    session_id = pin_file.stem
                    pin_status = self.probe_pin_liveness(pin_file)

                    if pin_status == PinStatus.STALE:
                        stale_sessions.add(session_id)
                        conn.execute("BEGIN IMMEDIATE;")
                        try:
                            conn.execute("DELETE FROM snapshot_protected_documents WHERE session_id = :sid;", {"sid": session_id})
                            conn.execute("DELETE FROM active_snapshots WHERE session_id = :sid;", {"sid": session_id})
                            conn.execute("COMMIT;")
                        except Exception:
                            conn.execute("ROLLBACK;")
                            raise
                        try:
                            os.unlink(pin_file)
                        except FileNotFoundError:
                            pass
                    else:
                        # Fail-closed: both PinStatus.LIVE and PinStatus.UNKNOWN preserve pin and snapshot
                        preserved_sessions.add(session_id)

            # Clean any snapshots in DB with no corresponding pin file on disk,
            # excluding any sessions whose liveness could not be established (preserved_sessions)
            conn.execute("BEGIN IMMEDIATE;")
            try:
                db_snapshots = [r[0] for r in conn.execute("SELECT session_id FROM active_snapshots;").fetchall()]
                for sid in db_snapshots:
                    if sid in preserved_sessions:
                        continue
                    pin_f = pins_dir / f"{sid}.pin"
                    if not pin_f.exists():
                        conn.execute("DELETE FROM snapshot_protected_documents WHERE session_id = :sid;", {"sid": sid})
                        conn.execute("DELETE FROM active_snapshots WHERE session_id = :sid;", {"sid": sid})
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            # -------------------------------------------------------------
            # Phase 2: Roll Back Incomplete Rebuild / Migration Staging
            # -------------------------------------------------------------
            manifest = conn.execute(
                "SELECT migration_state, staging_generation, staging_chunk_collection "
                "FROM storage_manifest WHERE manifest_key = 'manifest';"
            ).fetchone()
            if manifest and manifest["migration_state"] in ("REBUILD_STAGING", "MIGRATION_STAGING"):
                stg_gen = manifest["staging_generation"]
                stg_col = manifest["staging_chunk_collection"]

                # Drop Chroma staging collection if exists
                if stg_col and stg_col in [c.name for c in self.chroma_client.list_collections()]:
                    self.chroma_client.delete_collection(stg_col)

                conn.execute("BEGIN IMMEDIATE;")
                try:
                    if stg_gen is not None:
                        conn.execute("DELETE FROM document_generation_index WHERE generation = :gen;", {"gen": stg_gen})

                    c_stg = conn.execute(
                        "UPDATE storage_manifest SET "
                        "staging_generation = NULL, staging_chunk_collection = NULL, "
                        "staging_embedding_provider = NULL, staging_embedding_model = NULL, "
                        "staging_embedding_dimension = NULL, staging_embedding_config_version = NULL, "
                        "migration_state = 'IDLE', updated_at = :now "
                        "WHERE manifest_key = 'manifest' AND migration_state IN ('REBUILD_STAGING', 'MIGRATION_STAGING');",
                        {"now": now}
                    )
                    if c_stg.rowcount != 1:
                        conn.execute("ROLLBACK;")
                        raise StorageTransactionError("Failed to reset interrupted staging manifest state")
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

            # -------------------------------------------------------------
            # Phase 3: Roll Back Pending Ingestion and Replacement Records
            # -------------------------------------------------------------
            pending_docs = conn.execute(
                "SELECT document_id, source_identity FROM canonical_documents WHERE status = 'pending';"
            ).fetchall()
            active_gen = conn.execute("SELECT active_generation FROM storage_manifest WHERE manifest_key = 'manifest';").fetchone()[0]
            active_col_name = f"chunks_gen_{active_gen}"

            for p_doc in pending_docs:
                d_id = p_doc["document_id"]
                s_id = p_doc["source_identity"]

                # Purge pending chunks from active Chroma collection
                if active_col_name in [c.name for c in self.chroma_client.list_collections()]:
                    col = self.chroma_client.get_collection(active_col_name)
                    col.delete(where={"document_id": d_id})

                conn.execute("BEGIN IMMEDIATE;")
                try:
                    conn.execute("DELETE FROM document_generation_index WHERE document_id = :doc_id;", {"doc_id": d_id})
                    conn.execute("DELETE FROM canonical_chunks WHERE document_id = :doc_id;", {"doc_id": d_id})
                    c_del = conn.execute(
                        "DELETE FROM canonical_documents WHERE document_id = :doc_id AND status = 'pending';",
                        {"doc_id": d_id}
                    )
                    if c_del.rowcount != 1:
                        conn.execute("ROLLBACK;")
                        continue

                    # If source_catalog was a first-time placeholder and has no other documents, drop placeholder
                    conn.execute(
                        "DELETE FROM source_catalog "
                        "WHERE source_identity = :s_id AND active_document_id IS NULL "
                        "  AND NOT EXISTS (SELECT 1 FROM canonical_documents WHERE source_identity = :s_id);",
                        {"s_id": s_id}
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

            # -------------------------------------------------------------
            # Phase 4: Resume Interrupted Cleanup States
            # -------------------------------------------------------------
            # 4a. Resume PURGING (Chroma deletion completed or retried)
            purging_rows = conn.execute(
                "SELECT document_id, generation FROM cleanup_registry WHERE cleanup_state = 'PURGING';"
            ).fetchall()
            for row in purging_rows:
                d_id, gen = row["document_id"], row["generation"]
                col_name = f"chunks_gen_{gen}"
                if col_name in [c.name for c in self.chroma_client.list_collections()]:
                    c_col = self.chroma_client.get_collection(col_name)
                    c_col.delete(where={"document_id": d_id})
                    remaining = c_col.get(where={"document_id": d_id}, limit=1)
                    if len(remaining["ids"]) > 0:
                        continue # Skip to next pass

                conn.execute("BEGIN IMMEDIATE;")
                try:
                    cursor = conn.execute(
                        "DELETE FROM cleanup_registry WHERE document_id = :doc_id AND generation = :gen AND cleanup_state = 'PURGING';",
                        {"doc_id": d_id, "gen": gen}
                    )
                    if cursor.rowcount == 1:
                        conn.execute("DELETE FROM document_generation_index WHERE document_id = :doc_id AND generation = :gen;", {"doc_id": d_id, "gen": gen})
                        conn.execute("COMMIT;")
                    else:
                        conn.execute("ROLLBACK;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

            # 4b. Execute internal cleanup pass without re-acquiring lifecycle lock
            self._run_cleanup_pass_locked(conn)

            # -------------------------------------------------------------
            # Phase 5: Prune Retired Generations without re-acquiring lifecycle lock
            # -------------------------------------------------------------
            self._prune_retired_generations_locked(conn)

            return ReconciliationReport(success=True, timestamp=now)
        finally:
            self._lock_manager.release_lifecycle_exclusive()
    finally:
        self._lock_manager.release_writer_lock()
```

#### C. Crash Resilience of the Reconciliation Procedure Itself
Every mutation in `reconcile_startup()` is partitioned into atomic SQLite transactions verified by strict conditional row counts (`rowcount == 1`). If the host system or process crashes at any boundary during reconciliation:
1. All incomplete SQLite transactions roll back cleanly via SQLite WAL journal recovery.
2. Chroma collection and chunk deletions are idempotent; re-invoking `delete(where=...)` on an already-deleted chunk or collection is a safe no-op.
3. Upon restart, `reconcile_startup()` executes from Phase 1 and deterministically reaches the exact identical recovered state.

---

### 9.3 Retired-Generation Pruning
Safely reclaims disk space by deleting retired Chroma collections and SQLite catalog rows once all readers have exited.

#### A. Reader Protection and Lock Invariants
- **No New Readers:** New reader sessions only ever bind to `storage_manifest.active_generation`. A generation present in `retired_generations` is strictly $< \text{active\_generation}$ and cannot be acquired by any new reader session.
- **Drain Existing Readers:** The generation is only pruned once all existing reader sessions referencing it have completely terminated and released both their SQLite snapshot and OS kernel file lock.
- **Fail-Closed Pin Probing:** Any pin probe error where liveness cannot be definitively established (e.g., permissions, I/O, descriptor limits) is classified as `PinStatus.UNKNOWN`, which safely aborts and defers pruning.
- **Strict Lock Ownership Hierarchy:**
  1. *Standalone Public Pruning (`prune_retired_generations`):* Acquires $\mathcal{L}_1$ (`.writer.lock EX`), then acquires $\mathcal{L}_2$ (`.lifecycle.lock EX`), and releases them in reverse order ($\mathcal{L}_2$ then $\mathcal{L}_1$).
  2. *Migration-Triggered Pruning (`_prune_retired_generations_under_writer`):* Already owns $\mathcal{L}_1$ exclusively. Acquires $\mathcal{L}_2$ (`.lifecycle.lock EX`), delegates to `_prune_retired_generations_locked(conn)`, and releases $\mathcal{L}_2$. Never reacquires $\mathcal{L}_1$.
  3. *Startup Reconciliation (`reconcile_startup`):* Already owns both $\mathcal{L}_1$ and $\mathcal{L}_2$ exclusively. Calls `_prune_retired_generations_locked(conn)` directly with zero lock reacquisition.

#### B. Pin Liveness and Pruning Algorithms
```python
class PinStatus(str, Enum):
    STALE = "stale"      # Successfully acquired non-blocking exclusive lock -> reader is dead
    LIVE = "live"        # Specifically recognized lock conflict -> reader is actively holding lock
    UNKNOWN = "unknown"  # Unexpected open or probe error -> fail closed, assume potentially live


def probe_pin_liveness(self, pin_path: Path) -> PinStatus:
    """Probes a reader pin file to determine if a live process holds an OS kernel shared lock.

    Fail-closed contract:
    - Returns PinStatus.STALE only if non-blocking exclusive lock is acquired and released.
    - Returns PinStatus.LIVE on specifically recognized kernel lock contention.
    - Returns PinStatus.UNKNOWN on any unexpected error (permission, I/O, descriptor exhaustion, or adapter failure).
    """
    pin_fd = -1
    try:
        try:
            pin_fd = os.open(pin_path, os.O_RDWR)
        except OSError as e:
            if e.errno == errno.ENOENT:
                return PinStatus.STALE # File unlinked concurrently
            return PinStatus.UNKNOWN # Permission or I/O failure -> fail closed

        try:
            # Non-blocking probe for exclusive lock
            self._lock_manager.acquire_pin_exclusive_nb(pin_fd)
            # Successfully acquired -> no active reader holds shared lock
            self._lock_manager.release_pin(pin_fd)
            return PinStatus.STALE
        except (BlockingIOError, OSError) as e:
            # Recognized lock contention:
            # POSIX: EAGAIN, EWOULDBLOCK, EACCES
            # Windows: ERROR_LOCK_VIOLATION (33), ERROR_SHARING_VIOLATION (32)
            is_conflict = False
            if isinstance(e, BlockingIOError):
                is_conflict = True
            elif hasattr(e, "errno") and e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                is_conflict = True
            elif hasattr(e, "winerror") and e.winerror in (32, 33):
                is_conflict = True

            if is_conflict:
                return PinStatus.LIVE
            return PinStatus.UNKNOWN
        except Exception:
            return PinStatus.UNKNOWN
    finally:
        if pin_fd != -1:
            try:
                os.close(pin_fd)
            except OSError:
                pass


def _prune_retired_generations_locked(self, conn: sqlite3.Connection) -> None:
    """Internal pruning routine executed under writer lock and lifecycle exclusive lock.

    Assumes caller already holds .writer.lock EX and .lifecycle.lock EX.
    Does NOT attempt to acquire or release locks, preventing nested lock deadlocks on non-reentrant OS file locks.
    """
    retired = conn.execute(
        "SELECT generation, chunk_collection FROM retired_generations ORDER BY generation ASC;"
    ).fetchall()

    for row in retired:
        gen = row["generation"]
        col_name = row["chunk_collection"]

        # 1. Check SQLite active snapshots referencing this generation
        snap_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM active_snapshots WHERE generation = :gen;",
            {"gen": gen}
        ).fetchone()["cnt"]
        if snap_count > 0:
            continue # Reader snapshot actively references this generation

        # 2. Check filesystem kernel locks on active reader pins (fail-closed)
        can_prune = True
        pins_dir = self.config.storage_path / "pins"
        if pins_dir.exists():
            for pin_file in pins_dir.glob("*.pin"):
                pin_status = self.probe_pin_liveness(pin_file)
                if pin_status != PinStatus.STALE:
                    # Both LIVE and UNKNOWN prevent pruning! Fail closed.
                    can_prune = False
                    break

        if not can_prune:
            continue # Active reader or unknown pin status: defer pruning safely

        # 3. Chroma Collection Deletion FIRST
        try:
            if col_name in [c.name for c in self.chroma_client.list_collections()]:
                self.chroma_client.delete_collection(col_name)
        except Exception:
            # If Chroma collection deletion fails, do NOT delete the SQLite row.
            # Retains row so pruning will be retried on the next pass.
            continue

        # 4. Remove SQLite retired_generations record conditionally
        conn.execute("BEGIN IMMEDIATE;")
        try:
            cursor = conn.execute(
                "DELETE FROM retired_generations WHERE generation = :gen;",
                {"gen": gen}
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK;")
                continue
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise


def _prune_retired_generations_under_writer(self) -> None:
    """Internal migration pruning entrypoint: assumes .writer.lock EX is already held.

    Acquires .lifecycle.lock EX, executes pruning, and releases .lifecycle.lock EX.
    Does NOT attempt to reacquire .writer.lock EX, avoiding self-deadlock during migration.
    """
    self._lock_manager.acquire_lifecycle_exclusive()
    try:
        conn = self.conn_manager.get_connection()
        self._prune_retired_generations_locked(conn)
    finally:
        self._lock_manager.release_lifecycle_exclusive()


def prune_retired_generations(self) -> None:
    """Public standalone pruning entrypoint: acquires writer lock EX, then lifecycle lock EX."""
    self._lock_manager.acquire_writer_lock()
    try:
        self._prune_retired_generations_under_writer()
    finally:
        self._lock_manager.release_writer_lock()
```

#### C. Crash Recovery and State Consistency during Pruning
- **Crash before Step 3 (Chroma deletion):** Both Chroma collection and SQLite `retired_generations` row remain intact. Next pruning or reconciliation pass re-evaluates pins and retries pruning cleanly.
- **Crash after Step 3 (Chroma deletion succeeds), before Step 4 (SQLite commit):** Chroma collection has been deleted, but the SQLite row remains in `retired_generations`. On the next pruning or reconciliation pass, Step 3 detects that `col_name` does not exist in Chroma collections (`col_name in list_collections()` is False) and safely advances to Step 4 to delete the SQLite row with `cursor.rowcount == 1`. Zero zombie state is left behind.

---

## 10. Provider and Embedding Configuration

### 10.1 Deterministic Hash Embedding Baseline
- **Specification:** `provider_id = "deterministic_hash"`, `model_name = "sha256_word_char_384"`, `config_version = "v1.0"`.
- **Dimension:** 384 dimensions.
- **Normalization:** Unicode NFC normalized. Empty/whitespace-only input raises `ValueError("Input text must not be empty or whitespace-only")`.
- **Word Tokenization:** `re.findall(r"\b[\w\-]{2,}\b", text.lower(), re.UNICODE)`.
- **Symbol Fallback:** If zero word tokens extracted, extracts all non-whitespace characters $C = [c_1, \dots, c_m]$. Generates unigrams $c_i$ and bigrams $c_i c_{i+1}$ (for $i=1 \dots m-1$).
- **Projection & Weighting:**
  $h = \text{int}(\text{hashlib.sha256}(t.\text{encode}("utf-8")).\text{hexdigest}(), 16)$.
  $\text{dim} = h \pmod{384}$, $\text{sign} = +1.0 \text{ if } (h \gg 16) \pmod 2 == 0 \text{ else } -1.0$.
  $w(t) = 1.0 + \ln(count(t))$.
  $v[\text{dim}] += \text{sign} \cdot w(t)$.
- **Zero-Cancellation Fallback:**
  If $\sqrt{\sum v[i]^2} < 10^{-12}$:
  $h_{seed} = \text{int}(\text{hashlib.sha256}(text.\text{encode}("utf-8")).\text{hexdigest}(), 16)$.
  $v[h_{seed} \pmod{384}] = 1.0$.
- **Unit Normalization:** $v \leftarrow v / \|v\|_2$. Finite checks verify no NaN/Inf. Output is 100% deterministic.

### 10.2 Provider Resolution on Restart
1. Inspects `storage_manifest`: reads `embedding_provider`, `embedding_model`, `embedding_dimension`, `embedding_config_version`.
2. Resolves provider from `ProviderRegistry`.
3. Verifies resolved provider attributes match `storage_manifest` exactly.
4. If an optional provider was passed to the store constructor, verifies it matches the manifest. If mismatched, raises `EmbeddingProviderMismatchError`.
5. Rebuild preserves active manifest provider; migration sets new target provider in staging fields.

---

## 11. Filesystem Security

### 11.1 POSIX Security Implementation
- **Traversal:** Traversals pin intermediate directories using `os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)`.
- **Database & Sidecars:** Storage root opened with `O_DIRECTORY | os.O_NOFOLLOW`. Validates `st_uid == os.getuid()` and mode `0700`. Sidecars (`-wal`, `-shm`) reside inside verified storage directory.
- **Lock Files:** Created using `openat(dir_fd, filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)`.
- **Mount Verification:** Rejects remote mounts (`nfs`, `cifs`, `smbfs`, `9p`, `fuse.sshfs`).
- **Threat Model & Honest Limitation Statement:**
  - *Guaranteed:* Complete protection against unprivileged local users, shared-directory `/tmp` pre-creation attacks, path traversal, and symlink substitution by untrusted processes.
  - *Limitation:* C libraries opening path strings (SQLite and ChromaDB) have an unavoidable kernel-level TOCTOU window if an attacker has identical UID permissions (`os.getuid()`) to mutate the directory tree during execution. Mode `0700` prevents other local users from exploiting this.

### 11.2 Windows Security Implementation
- **Handle Validation:** Directory opened via `CreateFileW(..., FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)`.
- **Reparse Check:** Calls `GetFileInformationByHandleEx` to verify `(FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) == 0`.
- **Drive Type:** Validates `GetDriveTypeW(volume_path) != DRIVE_REMOTE`.
- **DACL Verification:** Calls `GetSecurityInfo` to confirm owner SID matches current process token SID (`TokenUser`) and DACL excludes unprivileged write access.
- *Limitation:* Chroma C++ client opens persistent paths via Windows path APIs. Security relies on volume-level NTFS DACL isolation.

---

## 12. Complete Crash-Recovery Matrix

| # | Crash Boundary | Durable SQLite State | Chroma State | Lock State | Executable Recovery Action | Safe Retry | Queryable Data |
|---|---|---|---|---|---|---|---|
| **1** | Database initialization | Incomplete schema or missing singletons. | No collections. | `.writer.lock` held. | Reconciler re-runs initialization transaction inside `BEGIN IMMEDIATE`. | Yes | None |
| **2** | First ingestion staging | `source_catalog.active_document_id = NULL`, document `'pending'`. | Chunks in `chunks_gen_<G>`. | `.writer.lock` held. | Reconciler deletes pending chunks from Chroma, deletes pending document and chunks from SQLite, drops placeholder catalog entry. | Yes | None |
| **3** | Replacement staging | $D_{new}$ `'pending'`, $D_{old}$ `'active'`. | $D_{new}$ chunks in Chroma. | `.writer.lock` held. | Deletes $D_{new}$ chunks from Chroma and SQLite. | Yes | $D_{old}$ remains queryable |
| **4** | Chroma partial insertion | Document `'pending'`, partial chunks in Chroma. | Partial chunks. | `.writer.lock` held. | Rollback deletes staged chunks from Chroma by ID, deletes pending SQLite records. | Yes | Previous active version |
| **5** | Pointer activation | Atomic commit completed: $D_{new}$ `'active'`, $D_{old}$ in `cleanup_registry`. | Both versions in Chroma. | `.lifecycle.lock` released. | State is 100% valid. Subsequent cleanup pass purges $D_{old}$. | Yes | $D_{new}$ queryable |
| **6** | Reader pin creation | Pin file on disk (unlocked). | Chroma intact. | No kernel lock. | Reconciler non-blocking probe succeeds $\implies$ unlinks orphaned pin file. | Yes | Active generation |
| **7** | Snapshot registration | Pin file locked, SQLite insert failed/rolled back. | Chroma intact. | Kernel lock held. | Reader release unlinks pin. If reader crashed, reconciler cleans orphan pin. | Yes | Active generation |
| **8** | Reader query | Row in `active_snapshots`, pin file locked. | Chroma intact. | Kernel lock held. | None. Query proceeds safely. | Yes | Pinned snapshot |
| **9** | Reader session release | DB row deleted, pin file exists. | Chroma intact. | Descriptor closed. | Reconciler lock probe succeeds $\implies$ unlinks pin file. | Yes | Active generation |
| **10** | Reader process crash | Row in `active_snapshots`, pin file on disk. | Chroma intact. | Kernel released lock upon crash. | Reconciler lock probe succeeds $\implies$ reader dead. Deletes snapshot row and unlinks pin file. | Yes | Active generation |
| **11** | Reconciliation worker | `.writer.lock` held. | Chroma intact. | Lock released on crash. | Next reconciliation restarts safely from beginning. Idempotent. | Yes | Active generation |
| **12** | Deletion at RETIRED | `cleanup_state = 'RETIRED'`. | Chunks in Chroma. | None. | Reconciler evaluates snapshots. If 0 references, advances to `TOMBSTONED`. | Yes | Active generation |
| **13** | Deletion at TOMBSTONED | `cleanup_state = 'TOMBSTONED'`. | Chunks in Chroma. | None. | Reconciler advances to `PURGING`, deletes from Chroma. | Yes | Active generation |
| **14** | Deletion at PURGING | `cleanup_state = 'PURGING'`. | Partial chunks deleted. | None. | Reconciler re-issues Chroma delete, confirms 0 chunks, deletes cleanup row. | Yes | Active generation |
| **15** | Final canonical deletion | Cleanup row deleted, canonical doc `'tombstoned'`. | Chunks 0 in Chroma. | None. | Reconciler deletes canonical document and chunks from SQLite. | Yes | Active generation |
| **16** | Rebuild staging | `migration_state = 'REBUILD_STAGING'`, staging mappings exist. | Staging collection partial. | `.writer.lock` held. | Reconciler drops staging collection, deletes staging rows from `document_generation_index`, resets manifest to `'IDLE'`. | Yes | Active generation untouched |
| **17** | Migration staging | `migration_state = 'MIGRATION_STAGING'`, staging mappings exist. | Staging collection partial. | `.writer.lock` held. | Reconciler drops staging collection, deletes staging rows from `document_generation_index`, resets manifest to `'IDLE'`. | Yes | Active generation untouched |
| **18** | Generation activation | Atomic commit completed: $G_{new}$ active, $G_{old}$ in `retired_generations`. | Both collections exist. | `.lifecycle.lock` released. | State is 100% valid. $G_{new}$ queryable. $G_{old}$ retained until reader snapshots drain. | Yes | $G_{new}$ queryable |
| **19** | Old-generation deletion | $G_{old}$ in `retired_generations`, Chroma delete interrupted. | Partial collection. | None. | Reconciler checks snapshots; if 0, re-issues collection drop and deletes retired row. | Yes | Active generation |
| **20** | Chroma corruption / loss | SQLite 100% intact. Chroma segment files corrupted. | Corrupted. | None. | Administrator/reconciler calls `rebuild_index()`. Reconstructs complete index from `canonical_chunks`. | Yes | Full recovery from SQLite |

---

## 13. Test Strategy and Implementation Gates

### 13.1 Test Categories
1. **Foreign-Key Safe First Ingestion:** Ingest first document; verify placeholder catalog creation and FK constraint validity.
2. **Conditional Update Row-Count Invariants:** Assert `cursor.rowcount == 1` checks and rollback on unexpected rows across all transitions.
3. **Replacement and Deletion Pointer Atomicity:** Verify atomic pointer switches and retired cleanup registrations.
4. **Race-Free Pin Publication:** Concurrently run reader registration and reconciliation; verify live readers are never deleted.
5. **Snapshot Isolation & Generation Provider Binding:** Register reader on Gen 1; migrate to Gen 2 with different provider; verify Gen 1 query uses Gen 1 provider and collection.
6. **Empty Active-Document Query:** Query store with no active documents; assert immediate empty list return without ChromaDB call.
7. **Crash Recovery from Every Cleanup State:** Simulate crashes in `RETIRED`, `TOMBSTONED`, and `PURGING`; assert clean recovery.
8. **Failed Staging Generation Rollback:** Inject failure during migration staging; verify `document_generation_index` rows rolled back and retry succeeds with same generation.
9. **SQLite Canonical Hydration:** Query chunks; verify text and metadata are hydrated from SQLite and ranked by Chroma similarity.
10. **Phase 4B.1 Metadata Validation:** Test strict allowlist, boolean/int distinctions, and sensitive credential filtering.
11. **Collision-Safe Chroma Storage IDs:** Test boundary document and chunk IDs containing colons; assert reversible parsing.
12. **Failure-History Identity Collisions:** Ingest distinct run IDs sharing a 32-char prefix; assert distinct fingerprints and no collisions.
13. **Deterministic Hash Embedding Edge Cases:** Test symbol-only text, exact cancellation zero-vector, and unit norm.
14. **Provider Resolution After Restart:** Test manifest mismatch detection and fail-closed behavior.
15. **POSIX Multi-Process Locks:** Multi-process concurrency stress test verifying mutual exclusion.
16. **Windows Multi-Process Locks:** Multi-process concurrency stress test verifying Win32 `LockFileEx` mutual exclusion on Windows runner.
17. **Storage-Root Security Traversal:** Test symlink traversal rejection, mode `0700`, UID check, and mount blocking.
18. **Chroma Rebuild from Canonical Records:** Wipe ChromaDB; call `rebuild_index()`; verify complete vector index restoration.
19. **Concurrent Multi-Thread Snapshot Registration:** Execute 5 concurrent threads registering snapshots and querying; assert zero SQLite transaction interleaving errors.
20. **Partial Initialization Retry:** Simulate crash during DDL; assert restart idempotently completes initialization.
21. **Snapshot Registration Failure Rollback:** Inject failure into provider resolution after snapshot transaction commits; verify committed SQLite snapshot and protected document rows are rolled back under lifecycle lock before pin is released.
22. **Standalone Pruning Lock Acquisition:** Standalone `prune_retired_generations()` acquires `.writer.lock EX` followed by `.lifecycle.lock EX` and releases them in reverse order ($\mathcal{L}_1 \implies \mathcal{L}_2$).
23. **Migration-Triggered Pruning Lock Ownership:** Migration-triggered pruning (`_prune_retired_generations_under_writer()`) acquires `.lifecycle.lock EX` without re-acquiring `.writer.lock EX` (which is already held by migration).
24. **Startup Reconciliation Lock Non-Reentrancy:** `reconcile_startup()` completes start-to-finish without re-acquiring either `.writer.lock` or `.lifecycle.lock`, delegating to `_run_cleanup_pass_locked()` and `_prune_retired_generations_locked()`.
25. **Pin Lock Conflict Classified as Live:** Probing a pin with an active kernel shared lock (yielding `BlockingIOError` / `EAGAIN` / `EWOULDBLOCK` on POSIX or `ERROR_LOCK_VIOLATION` on Windows) returns `PinStatus.LIVE`, preventing deletion.
26. **Fail-Closed Pin Probe in Reconciliation:** Inject unexpected error (e.g. `PermissionError` on `os.open`, descriptor failure, or unhandled lock adapter error) during `probe_pin_liveness()`; assert status is `PinStatus.UNKNOWN`, the `.pin` file is NOT unlinked, and the corresponding snapshot row in `active_snapshots` is preserved.
27. **Fail-Closed Pin Probe in Retired-Generation Pruning:** Inject unexpected error during pin probe; assert `_prune_retired_generations_locked()` detects `PinStatus.UNKNOWN`, aborts pruning, and leaves both Chroma collection and SQLite `retired_generations` row intact.
28. **Query Session Store Identity Validation:** Verify that `query_chunks(..., session=session)` accepts an active session created by the same store (`session.store is self`) and rejects a session created by a different `LocalKnowledgeStore` instance (`session.store is not self`) with `InvalidSessionError`.

### 13.2 Explicit Implementation Gates
- **Gate 1 (Windows Concurrency):** Live multi-process test of Win32 `LockFileEx` via `ctypes` on a Windows runner.
- **Gate 2 (ChromaDB 0.5.5 Wheel & SQLite Runtime):** Live package installation verifying ChromaDB 0.5.5 links with system SQLite `3.37.2` (`sqlite3.sqlite_version_info >= (3, 35, 0)`).
- **Gate 3 (ONNX Model Checksums & Tensors):** Live verification of pre-downloaded `all-MiniLM-L6-v2` ONNX artifact hashes and tensor names.

---

## 14. Finding-by-Finding Resolution Mapping

### 14.1 Revision 17 Final Resolution Mapping

| Item # | Finding Summary | Resolution Mechanism | Architecture Section |
|---|---|---|---|
| **Fix 1** | Retired-generation pruning lock ownership | Standalone `prune_retired_generations()` acquires `.writer.lock EX` then `.lifecycle.lock EX`; migration uses `_prune_retired_generations_under_writer()` without reacquiring writer lock; reconciliation calls `_prune_retired_generations_locked(conn)`. | Section 4.1, 9.1, 9.2, 9.3, 13.1 |
| **Fix 2** | Fail-closed pin-probe error handling | Defined `PinStatus` and `probe_pin_liveness()`; recognized lock conflicts yield `PinStatus.LIVE`; unexpected errors yield `PinStatus.UNKNOWN`; both fail closed, preserving pins and snapshot rows and deferring pruning. | Section 4.3, 9.2, 9.3, 13.1 |
| **Fix 3** | Startup reconciliation deadlock | Refactored cleanup and pruning into internal routines (`_run_cleanup_pass_locked`, `_prune_retired_generations_locked`) assuming locks already held; public methods acquire lifecycle lock exactly once; zero nested lock deadlocks on non-reentrant OS locks. | Section 6.3, 9.2, 9.3, 13.1 |
| **Fix 4** | QueryReaderSession store identity | Replaced undefined `session.store_id != self.store_id` check with object identity validation `session.store is not self`; eliminates `AttributeError` and rejects foreign sessions. | Section 8.2, 13.1 |
| **Blocker 1** | Truly atomic cleanup transitions & row counts | Checked `rowcount == 1` for both cleanup registry and canonical document transitions; rolled back on mismatch; checked `rowcount == 1` for `TOMBSTONED -> PURGING`; verified zero orphans in final purge. | Section 6.3 |
| **Blocker 2** | Repaired snapshot failure paths | Explicitly rolls back committed SQLite snapshot under lifecycle lock before releasing pin on provider resolution error; never unlinks pin if SQLite delete fails during session release; retriable session close. | Section 7.1, 7.2 |
| **Blocker 3** | Executable startup reconciliation procedure | Fully specified 5-phase algorithm (`reconcile_startup`) covering lock hierarchy ($\mathcal{L}_1 \implies \mathcal{L}_2$), pin probes, staging generation rollback, pending document rollback, cleanup resumption, and crash resilience. | Section 9.2, 12 |
| **Blocker 4** | Complete retired-generation pruning | Fully specified `prune_retired_generations()` / `_prune_retired_generations_locked()` checking SQLite snapshots and kernel locks on pins, deleting Chroma collection first, removing SQLite catalog row conditionally (`rowcount == 1`), and handling crash recovery. | Section 9.3 |
| **Blocker 5** | Consistent Chroma metadata & connection contracts | Unified exact 6-field Chroma internal metadata allowlist across sections; clearly separated from Phase 4B.1 user metadata; replaced all `self.conn` with thread-local `conn_manager.get_connection()`. | Section 4.2, 8.1, 9.1 |

### 14.2 Consolidated Architecture Baseline Mappings (C1–C15)

| Finding ID | Title | Summary of Architectural Solution | Primary Section |
|---|---|---|---|
| **C1** | Complete generation rebuild and migration protocol | Two-phase rebuild and migration staging collections (`staging_generation`); atomic activation pointer update; reader pin validation prevents premature collection drop. | Section 9.1, 9.2, 9.3 |
| **C2** | Atomic cleanup state machine | Fully coupled transitions (`RETIRED -> TOMBSTONED -> PURGING -> DELETED`); verified `rowcount == 1` at each step; transactional rollback if desynchronized. | Section 6.3 |
| **C3** | Pending ingestion and failed-operation recovery | Placeholder `source_catalog` row resolves FK ordering; pending documents rolled back on startup if uncommitted; zero incomplete query exposure. | Section 5.1, 5.2, 9.2, 12 |
| **C4** | Explicit Chroma internal metadata contract | Strict 6-field allowlist (`document_id` [str], `chunk_id` [str], `chunk_index` [int], `source_identity` [str], `source_type` [str], `generation` [int]); primitive types only; user metadata stored in SQLite. | Section 8.1, 9.1 |
| **C5** | SQLite initialization and transaction correctness | Strict PRAGMA ordering; explicit `BEGIN IMMEDIATE ... COMMIT` block executing statement-by-statement DDL; idempotent retry capability. | Section 2, 3.1, 3.2 |
| **C6** | Filesystem and storage-root security | Descriptor-pinned path traversal (`O_DIRECTORY \| O_NOFOLLOW`); local mount validation; explicit documentation of pathname TOCTOU threat model. | Section 11.1, 11.2 |
| **C7** | Complete Windows locking contract | Win32 `LockFileEx` via `ctypes` implementing shared/exclusive byte-range locks for `.writer.lock` and `.lifecycle.lock`; pin handle registry. | Section 4.1, 4.3 |
| **C8** | Snapshot transaction and reader lifecycle | Atomically captured `(G, S, active_docs, provider_id)` tuple; reader pin file created before dropping lifecycle lock; non-blocking retrieval; robust failure paths. | Section 7.1, 7.2 |
| **C9** | SQLite canonical hydration and ranking integrity | Chroma used solely for vector candidate ranking; candidate IDs validated against snapshot active set; canonical content hydrated directly from SQLite. | Section 8.2 |
| **C10** | Phase 4B.1 metadata compatibility | Complete fidelity to `DocumentMetadata` and `ChunkMetadata`; zero schema modifications; JSON serialization preserving ISO timestamps and UUIDs. | Section 8.1, 15 |
| **C11** | Failure-history identity and source identity reuse | Preserved source identity format `f"{source_type}:{slug}"`; failure history matches Phase 4B.2 specification. | Section 5.1, 8.1 |
| **C12** | Deterministic hashing provider correctness | Word-level tokenization with symbol fallback; unit normalization; zero-cancellation seed fallback; zero external runtime dependencies. | Section 10.1 |
| **C13** | Embedding provider and migration consistency | Strict manifest binding; immutable generation-provider pairing; dynamic resolution via `ProviderRegistry`; reader session provider lock. | Section 9.1, 10.2 |
| **C14** | Retired-generation representation and lifecycle | Relationally tracked `retired_generations` table storing provider metadata and drop eligibility; safely pruned only when zero reader pins exist. | Section 2, 9.1, 9.3 |
| **C15** | Identifier uniqueness and canonical identity | Structured deterministic IDs `doc-{slug}-{sha256[:12]}-{uuid4[:12]}`; chunk keys `chk-{doc_id[:16]}-{index:04d}-{sha256[:8]}`; composite keys in index table. | Section 2, 5.1, 8.1 |

---

## 15. Scope and Compatibility Report

### Approved Artifacts Preserved Intact
- `backend/app/schemas/knowledge.py`: 100% untouched.
- `tests/test_knowledge_schemas.py`: 100% untouched.
- `backend/app/services/document_ingestion.py`: 100% untouched.
- `tests/test_document_ingestion.py`: 100% untouched.
- All Phase 1, Phase 2, Phase 3, and Phase 4A implementation files and tests: 100% untouched.

### Execution Boundaries
- **Architecture and documentation only:** Zero implementation files created or edited.
- **Zero dependency modifications:** `chromadb` is not installed; environment remains clean.
- **Git status:** Clean at commit `8ec2551` on branch `feature/phase-4b-rag`.
- **Implementation Status:** Awaiting explicit independent review and approval before implementation.

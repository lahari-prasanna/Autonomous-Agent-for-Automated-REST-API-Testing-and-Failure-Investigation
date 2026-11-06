"""
Local Knowledge Storage and Indexing Service (Phase 4B.3).
Authoritative implementation based on Phase 4B.3 Architecture Revision 17.

Provides:
- coordinator.sqlite3 as canonical source of truth (WAL mode, synchronous=FULL, strict foreign keys).
- ChromaDB (0.5.5) PersistentClient as derived, reconstructible vector search index.
- Strict global lock hierarchy (writer.lock EX -> lifecycle.lock EX).
- Reader snapshot isolation pinned by OS kernel file locks.
- Fail-closed 3-state pin probing (STALE, LIVE, UNKNOWN).
- Deterministic 384-dimensional SHA-256 word/symbol hash embedding provider.
- Injective, collision-safe Chroma storage ID encoding.
- Generation-scoped Chroma collections and atomic migration protocols.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from enum import Enum
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple, Union
import unicodedata
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import chromadb
from chromadb.config import Settings as ChromaSettings

# Silence posthog telemetry incompatibilities in chromadb 0.5.5
try:
    import chromadb.telemetry.product.posthog
    chromadb.telemetry.product.posthog.Posthog.capture = lambda *args, **kwargs: None
except Exception:
    pass

from backend.app.schemas.failure import FailureCategory
from backend.app.schemas.knowledge import (
    ALLOWED_METADATA_KEYS,
    MAX_ID_LEN,
    VALID_HTTP_METHODS,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSourceType,
    _validate_http_method,
    _validate_metadata_dict,
    _validate_operation_id,
    _validate_path_template,
)

import ctypes
from ctypes import wintypes

if sys.platform != "win32":
    import fcntl


# ---------------------------------------------------------------------------
# Exceptions Hierarchy
# ---------------------------------------------------------------------------

class KnowledgeStoreError(Exception):
    """Base exception for all local knowledge storage operations."""
    pass


class UnsupportedRuntimeError(KnowledgeStoreError):
    """Raised when runtime environment (e.g. SQLite version) is unsupported."""
    pass


class StorageConfigurationError(KnowledgeStoreError):
    """Raised when database or connection PRAGMA configuration fails."""
    pass


class CorruptedGlobalStateError(KnowledgeStoreError):
    """Raised when global_state or storage_manifest singletons are corrupted or missing."""
    pass


class CorruptedIndexIntegrityError(KnowledgeStoreError):
    """Raised when Chroma index contains chunks missing from canonical SQLite storage."""
    pass


class CorruptedStorageIdError(KnowledgeStoreError):
    """Raised when Chroma storage ID encoding is malformed or invalid."""
    pass


class StorageTransactionError(KnowledgeStoreError):
    """Raised when a transactional storage operation fails or updates unexpected rowcount."""
    pass


class ConcurrencyConflictError(KnowledgeStoreError):
    """Raised when an atomic conditional update detects concurrent state modification."""
    pass


class SourceTypeMismatchError(KnowledgeStoreError):
    """Raised when attempting to ingest a document whose source_type does not match catalog."""
    pass


class IdentityCollisionError(KnowledgeStoreError):
    """Raised when an unrecoverable source identity collision occurs."""
    pass


class InvalidManifestStateError(KnowledgeStoreError):
    """Raised when an index rebuild or migration is attempted in an invalid state."""
    pass


class MigrationVerificationError(KnowledgeStoreError):
    """Raised when index rebuild or migration validation detects count, ID, or vector discrepancies."""
    pass


class EmbeddingProviderMismatchError(KnowledgeStoreError):
    """Raised when active storage manifest provider does not match runtime configuration."""
    pass


class InvalidSessionError(KnowledgeStoreError):
    """Raised when a query session does not belong to the queried store instance."""
    pass


class ClosedSessionError(KnowledgeStoreError):
    """Raised when a query is executed against an inactive or closed session."""
    pass


class SecurityStorageError(KnowledgeStoreError):
    """Raised when a filesystem traversal, unapproved mount, or insecure directory is detected."""
    pass


class DocumentChunkMismatchError(KnowledgeStoreError, ValueError):
    """Raised when a chunk's document_id or source_type does not match the parent KnowledgeDocument."""
    pass


# ---------------------------------------------------------------------------
# Enums, Result Models & Configuration
# ---------------------------------------------------------------------------

class PinStatus(str, Enum):
    """3-state fail-closed reader pin liveness status."""
    STALE = "stale"      # Acquired non-blocking exclusive lock -> reader is dead
    LIVE = "live"        # Recognized lock conflict -> reader is actively holding lock
    UNKNOWN = "unknown"  # Unexpected open or probe error -> fail closed, assume potentially live


class StoreConfig(BaseModel):
    """Configuration options for LocalKnowledgeStore."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    storage_path: Path
    busy_timeout_ms: int = 30000
    skip_security_checks: bool = False  # Allows testing environments with mock mounts/permissions


class MigrationResult(BaseModel):
    """Outcome report for index rebuild or embedding migration."""
    model_config = ConfigDict(populate_by_name=True)

    from_generation: int
    to_generation: int
    documents_migrated: int
    chunks_migrated: int
    new_provider_id: str


class ReconciliationReport(BaseModel):
    """Outcome report for startup recovery and reconciliation."""
    model_config = ConfigDict(populate_by_name=True)

    success: bool
    timestamp: str
    stale_sessions_recovered: int = 0
    preserved_sessions: int = 0
    pending_docs_rolled_back: int = 0


# ---------------------------------------------------------------------------
# Retrieval Models & Helpers (Phase 4B.4)
# ---------------------------------------------------------------------------

def compute_relevance_score(distance: float) -> float:
    """
    Computes a normalized relevance score in [0.0, 1.0] from cosine distance.
    Cosine distance d is in [0.0, 2.0]:
      d = 0.0 -> score = 1.0 (identical)
      d = 1.0 -> score = 0.5 (orthogonal)
      d = 2.0 -> score = 0.0 (opposite)
    Formula: max(0.0, min(1.0, 1.0 - (distance / 2.0)))
    """
    raw_score = 1.0 - (float(distance) / 2.0)
    clamped = max(0.0, min(1.0, raw_score))
    return round(clamped, 6)


class RetrievalResult(BaseModel):
    """
    Represents a single hydrated retrieval candidate with its relevance score (Phase 4B.4).
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk: KnowledgeChunk = Field(..., description="Canonical chunk hydrated from SQLite")
    score: float = Field(..., ge=0.0, le=1.0, description="Normalized relevance score in [0.0, 1.0]")
    distance: float = Field(..., ge=0.0, description="Raw cosine distance from ChromaDB")


class RetrievalFilter(BaseModel):
    """
    Defines metadata filter criteria for canonical SQLite chunk retrieval (Phase 4B.4).
    Enforces strict field allowlists, alias resolution, and type validation.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source_type: Optional[KnowledgeSourceType] = None
    http_method: Optional[str] = None
    path_template: Optional[str] = Field(default=None, alias="path")
    operation_id: Optional[str] = None
    failure_category: Optional[Union[FailureCategory, str]] = None
    metadata: Dict[str, Union[str, int, float, bool, None]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _preprocess_filter_dict(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            known_fields = {"source_type", "http_method", "path_template", "path", "operation_id", "failure_category", "metadata"}
            meta = dict(data.get("metadata") or {})
            for k in list(data.keys()):
                if k not in known_fields:
                    if k in ALLOWED_METADATA_KEYS:
                        meta[k] = data.pop(k)
                    else:
                        raise ValueError(f"Unrecognized or forbidden filter field: '{k}'")
            if meta:
                data["metadata"] = meta
        return data

    @field_validator("source_type", mode="before")
    @classmethod
    def _val_source_type(cls, v: Any) -> Optional[KnowledgeSourceType]:
        if v is None:
            return None
        if isinstance(v, KnowledgeSourceType):
            return v
        if isinstance(v, str):
            try:
                return KnowledgeSourceType(v.strip().lower())
            except ValueError:
                raise ValueError(f"Invalid source_type: '{v}'. Must be one of {[e.value for e in KnowledgeSourceType]}.")
        raise ValueError(f"source_type must be a KnowledgeSourceType or string, got {type(v).__name__}.")

    @field_validator("http_method")
    @classmethod
    def _val_http_method(cls, v: Optional[Any]) -> Optional[str]:
        return _validate_http_method(v)

    @field_validator("path_template")
    @classmethod
    def _val_path_template(cls, v: Optional[Any]) -> Optional[str]:
        return _validate_path_template(v)

    @field_validator("operation_id")
    @classmethod
    def _val_operation_id(cls, v: Optional[Any]) -> Optional[str]:
        return _validate_operation_id(v)

    @field_validator("failure_category")
    @classmethod
    def _val_failure_category(cls, v: Optional[Any]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, FailureCategory):
            return v.value
        if isinstance(v, str):
            cleaned = v.strip()
            if not cleaned:
                return None
            if len(cleaned) > MAX_ID_LEN:
                raise ValueError(f"failure_category exceeds maximum length of {MAX_ID_LEN} characters.")
            return cleaned
        raise ValueError(f"failure_category must be a FailureCategory or string, got {type(v).__name__}.")

    @field_validator("metadata", mode="before")
    @classmethod
    def _val_metadata(cls, v: Any) -> Dict[str, Union[str, int, float, bool, None]]:
        return _validate_metadata_dict(v)


# ---------------------------------------------------------------------------
# Chroma Storage ID Injective Formatting
# ---------------------------------------------------------------------------

def format_chroma_id(document_id: str, chunk_id: str) -> str:
    """Encodes document_id and chunk_id into an injective, collision-safe storage ID."""
    return f"{len(document_id)}:{document_id}:{chunk_id}"


def parse_chroma_id(storage_id: str) -> Tuple[str, str]:
    """Decodes an injective storage ID back into (document_id, chunk_id)."""
    prefix, sep, rest = storage_id.partition(":")
    if not sep or not prefix.isdigit():
        raise CorruptedStorageIdError(f"Invalid storage_id format: {storage_id}")
    doc_len = int(prefix)
    if len(rest) < doc_len + 1 or rest[doc_len] != ":":
        raise CorruptedStorageIdError(f"Malformed storage_id length prefix: {storage_id}")
    doc_id = rest[:doc_len]
    chunk_id = rest[doc_len + 1:]
    return doc_id, chunk_id


# ---------------------------------------------------------------------------
# Embedding Provider Protocol & Deterministic Baseline
# ---------------------------------------------------------------------------

class EmbeddingProvider:
    """Base class / interface for RAG embedding providers."""
    @property
    def provider_id(self) -> str:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        raise NotImplementedError

    @property
    def dimension(self) -> int:
        raise NotImplementedError

    @property
    def config_version(self) -> str:
        raise NotImplementedError

    def embed_text(self, text: str) -> List[float]:
        raise NotImplementedError

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError


class DeterministicHashEmbeddingProvider(EmbeddingProvider):
    """
    384-dimensional deterministic hash embedding baseline (Section 10.1).
    Uses SHA-256 projections, word tokenization with symbol fallback,
    zero-cancellation handling, and unit normalization.
    """
    @property
    def provider_id(self) -> str:
        return "deterministic_hash"

    @property
    def model_name(self) -> str:
        return "sha256_word_char_384"

    @property
    def dimension(self) -> int:
        return 384

    @property
    def config_version(self) -> str:
        return "v1.0"

    def embed_text(self, text: str) -> List[float]:
        if not isinstance(text, str):
            raise ValueError(f"Input text must be a string, got {type(text).__name__}")
        normalized = unicodedata.normalize("NFC", text)
        if not normalized or not normalized.strip():
            raise ValueError("Input text must not be empty or whitespace-only")

        # 1. Word tokenization
        tokens = re.findall(r"\b[\w\-]{2,}\b", normalized.lower(), re.UNICODE)

        # 2. Symbol fallback if 0 word tokens extracted
        if not tokens:
            chars = [c for c in normalized if not c.isspace()]
            if not chars:
                raise ValueError("Input text must not be empty or whitespace-only")
            unigrams = chars
            bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
            tokens = unigrams + bigrams

        # 3. Projection & Weighting
        counts = Counter(tokens)
        vec = [0.0] * self.dimension

        for tok, count in counts.items():
            h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
            dim = h % self.dimension
            sign = 1.0 if ((h >> 16) % 2 == 0) else -1.0
            weight = 1.0 + math.log(count)
            vec[dim] += sign * weight

        # 4. Zero-Cancellation Fallback
        norm = math.sqrt(sum(x * x for x in vec))
        if norm < 1e-12:
            h_seed = int(hashlib.sha256(normalized.encode("utf-8")).hexdigest(), 16)
            vec = [0.0] * self.dimension
            vec[h_seed % self.dimension] = 1.0
            norm = 1.0

        # 5. Unit Normalization
        unit_vec = [x / norm for x in vec]

        # 6. Finite validation
        for val in unit_vec:
            if not math.isfinite(val):
                raise ValueError("Non-finite value generated in deterministic embedding")

        return unit_vec

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_text(t) for t in texts]


class ProviderRegistry:
    """Registry for managing and resolving RAG embedding providers."""
    def __init__(self):
        self._providers: Dict[Tuple[str, str, int, str], EmbeddingProvider] = {}
        # Default registration
        self.register(DeterministicHashEmbeddingProvider())

    def register(self, provider: EmbeddingProvider) -> None:
        key = (provider.provider_id, provider.model_name, provider.dimension, provider.config_version)
        self._providers[key] = provider

    def resolve(self, provider_id: str, model_name: str, dimension: int, config_version: str) -> EmbeddingProvider:
        key = (provider_id, model_name, dimension, config_version)
        if key in self._providers:
            return self._providers[key]
        if (
            provider_id == "deterministic_hash"
            and model_name == "sha256_word_char_384"
            and dimension == 384
            and config_version == "v1.0"
        ):
            p = DeterministicHashEmbeddingProvider()
            self._providers[key] = p
            return p
        raise EmbeddingProviderMismatchError(
            f"Embedding provider {provider_id}:{model_name} (dim={dimension}, cfg={config_version}) not found in registry"
        )


# Global default provider registry
GLOBAL_PROVIDER_REGISTRY = ProviderRegistry()


# ---------------------------------------------------------------------------
# SQLite Schema & Individual DDL Statements
# ---------------------------------------------------------------------------

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


def configure_sqlite_connection(db_path: Path, busy_timeout_ms: int = 30000) -> sqlite3.Connection:
    """Configures an individual SQLite connection according to Section 3.1 strict PRAGMA order."""
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise UnsupportedRuntimeError(
            f"SQLite 3.35.0+ required for UPSERT and RETURNING; got {sqlite3.sqlite_version}"
        )

    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000.0,
        isolation_level=None,  # Explicit transaction control
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row

    # Foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    fk_status = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    if fk_status != 1:
        raise StorageConfigurationError("Failed to enable foreign_keys in SQLite")

    # WAL mode
    wal_status = conn.execute("PRAGMA journal_mode = WAL;").fetchone()[0]
    if wal_status.lower() != "wal":
        raise StorageConfigurationError(f"Failed to enable WAL mode; got {wal_status}")

    # synchronous = FULL (2)
    conn.execute("PRAGMA synchronous = FULL;")
    sync_status = conn.execute("PRAGMA synchronous;").fetchone()[0]
    if sync_status != 2:
        raise StorageConfigurationError(f"Failed to set PRAGMA synchronous = FULL; got {sync_status}")

    # Busy timeout
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")

    return conn


def initialize_database(conn: sqlite3.Connection, provider: EmbeddingProvider) -> None:
    """Executes schema DDL statements and initializes singletons inside explicit transaction (Section 3.2)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE;")
    try:
        for ddl in INDIVIDUAL_DDL_STATEMENTS:
            conn.execute(ddl)

        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (1, :now) ON CONFLICT(version) DO NOTHING;",
            {"now": now}
        )

        conn.execute(
            "INSERT INTO global_state (singleton_key, global_commit_seq, updated_at) "
            "VALUES ('state', 0, :now) ON CONFLICT(singleton_key) DO NOTHING;",
            {"now": now}
        )

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

        state_count = conn.execute("SELECT COUNT(*) FROM global_state WHERE singleton_key = 'state';").fetchone()[0]
        manifest_count = conn.execute("SELECT COUNT(*) FROM storage_manifest WHERE manifest_key = 'manifest';").fetchone()[0]
        if state_count != 1 or manifest_count != 1:
            raise CorruptedGlobalStateError("Required singletons not initialized")

        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


class ConnectionManager:
    """Maintains thread-isolated SQLite connections with correct PRAGMAs (Section 4.2)."""
    def __init__(self, db_path: Path, busy_timeout_ms: int = 30000):
        self.db_path = db_path
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()

    def get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = configure_sqlite_connection(self.db_path, self.busy_timeout_ms)
        return self._local.conn

    def close_thread_connection(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None


# ---------------------------------------------------------------------------
# Cross-Platform File Locking & Reader Pin Manager
# ---------------------------------------------------------------------------

# Windows NT / Win32 ctypes structures for handle-anchored traversal and locking
class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR)
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p)
    ]


class IO_STATUS_BLOCK(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("Status", wintypes.LONG), ("Pointer", ctypes.c_void_p)]
    _anonymous_ = ("_u",)
    _fields_ = [
        ("_u", _U),
        ("Information", ctypes.c_size_t)
    ]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _create_unicode_string(text: str) -> Tuple[UNICODE_STRING, Any]:
    """
    Creates an NT UNICODE_STRING with exact UTF-16LE byte length.
    Returns (UNICODE_STRING, backing_buffer) where backing_buffer must be kept
    alive for the duration of any native Windows NT API call.
    - Length is calculated using actual UTF-16LE encoded byte length (handling surrogate pairs).
    - MaximumLength includes the 2-byte UTF-16LE null terminator.
    """
    encoded_bytes = text.encode("utf-16le")
    byte_len = len(encoded_bytes)
    if byte_len > 65532:
        raise SecurityStorageError(f"Path component exceeds maximum NT UNICODE_STRING length: '{text}'")
    max_len = byte_len + 2
    raw_buf = ctypes.create_string_buffer(encoded_bytes + b"\x00\x00", max_len)
    u_str = UNICODE_STRING(
        Length=byte_len,
        MaximumLength=max_len,
        Buffer=ctypes.cast(raw_buf, wintypes.LPWSTR)
    )
    return u_str, raw_buf


_WIN32_KERNEL32 = None
_WIN32_NTDLL = None


def _configure_kernel32(kernel32: Any) -> Any:
    """Configures explicit argtypes and restype on a kernel32 WinDLL instance."""
    # CreateFileW: pointer-sized HANDLE return (wintypes.HANDLE)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    # CloseHandle
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # GetFileInformationByHandle
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

    # GetDriveTypeW
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT

    # CreateDirectoryW
    kernel32.CreateDirectoryW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p
    ]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL

    # LockFileEx
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL

    # UnlockFileEx
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL

    return kernel32


def _get_win32_kernel32() -> Any:
    """Returns ctypes.WinDLL('kernel32') with explicit argtypes and restype declared."""
    global _WIN32_KERNEL32
    if sys.platform != "win32":
        return None
    if _WIN32_KERNEL32 is not None:
        return _WIN32_KERNEL32

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _WIN32_KERNEL32 = _configure_kernel32(kernel32)
    return _WIN32_KERNEL32


def _configure_ntdll(ntdll: Any) -> Any:
    """Configures explicit argtypes and restype on an ntdll WinDLL instance."""
    # NtOpenFile: relative/anchored handle opening via OBJECT_ATTRIBUTES.RootDirectory
    ntdll.NtOpenFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),      # FileHandle (PHANDLE)
        wintypes.DWORD,                       # DesiredAccess (ACCESS_MASK)
        ctypes.POINTER(OBJECT_ATTRIBUTES),    # ObjectAttributes (POBJECT_ATTRIBUTES)
        ctypes.POINTER(IO_STATUS_BLOCK),      # IoStatusBlock (PIO_STATUS_BLOCK)
        wintypes.DWORD,                       # ShareAccess (ULONG)
        wintypes.DWORD,                       # OpenOptions (ULONG)
    ]
    ntdll.NtOpenFile.restype = wintypes.LONG  # NTSTATUS (32-bit signed long)

    # NtCreateFile: relative/anchored directory creation via OBJECT_ATTRIBUTES.RootDirectory
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),      # FileHandle (PHANDLE)
        wintypes.DWORD,                       # DesiredAccess (ACCESS_MASK)
        ctypes.POINTER(OBJECT_ATTRIBUTES),    # ObjectAttributes (POBJECT_ATTRIBUTES)
        ctypes.POINTER(IO_STATUS_BLOCK),      # IoStatusBlock (PIO_STATUS_BLOCK)
        ctypes.c_void_p,                      # AllocationSize (PLARGE_INTEGER OPTIONAL)
        wintypes.DWORD,                       # FileAttributes (ULONG)
        wintypes.DWORD,                       # ShareAccess (ULONG)
        wintypes.DWORD,                       # CreateDisposition (ULONG)
        wintypes.DWORD,                       # CreateOptions (ULONG)
        ctypes.c_void_p,                      # EaBuffer (PVOID OPTIONAL)
        wintypes.DWORD,                       # EaLength (ULONG)
    ]
    ntdll.NtCreateFile.restype = wintypes.LONG  # NTSTATUS (32-bit signed long)

    return ntdll


def _get_win32_ntdll() -> Any:
    """Returns ctypes.WinDLL('ntdll') with explicit argtypes and restype declared."""
    global _WIN32_NTDLL
    if sys.platform != "win32":
        return None
    if _WIN32_NTDLL is not None:
        return _WIN32_NTDLL

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _WIN32_NTDLL = _configure_ntdll(ntdll)
    return _WIN32_NTDLL


class LockManager:
    """
    Manages OS kernel file locks (.writer.lock, .lifecycle.lock, and reader pins).
    Adheres strictly to the lock hierarchy: writer.lock EX -> lifecycle.lock EX / SH.
    Tracks acquisition calls for invariant validation and prevents reentrancy deadlocks.
    Multi-thread safe: each thread manages its own OS file descriptors to prevent
    concurrent descriptor reuse or premature descriptor closure.
    """
    def __init__(self, locks_dir: Path):
        self.locks_dir = locks_dir
        self.writer_lock_path = locks_dir / ".writer.lock"
        self.lifecycle_lock_path = locks_dir / ".lifecycle.lock"

        self._thread_writer_lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._local = threading.local()

        # Introspection / test spy audit log
        self.events: List[Tuple[str, str]] = []

    def _open_lock_fd(self, path: Path) -> int:
        return os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)

    def _get_lifecycle_fds(self) -> List[int]:
        if not hasattr(self._local, "lifecycle_fds"):
            self._local.lifecycle_fds = []
        return self._local.lifecycle_fds

    # --- Writer Lock (L1) ---
    def acquire_writer_lock(self) -> None:
        with self._events_lock:
            self.events.append(("acquire_writer_lock", "EX"))
        self._thread_writer_lock.acquire()
        fd = -1
        try:
            fd = self._open_lock_fd(self.writer_lock_path)
            if sys.platform != "win32":
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                self._win_lock(fd, exclusive=True, nonblocking=False)
            self._local.writer_fd = fd
        except Exception:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._thread_writer_lock.release()
            raise

    def release_writer_lock(self) -> None:
        with self._events_lock:
            self.events.append(("release_writer_lock", "EX"))
        fd = getattr(self._local, "writer_fd", None)
        try:
            if fd is not None:
                try:
                    if sys.platform != "win32":
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    else:
                        self._win_unlock(fd)
                finally:
                    os.close(fd)
        finally:
            self._local.writer_fd = None
            self._thread_writer_lock.release()

    # --- Lifecycle Lock (L2) ---
    def acquire_lifecycle_shared(self) -> None:
        with self._events_lock:
            self.events.append(("acquire_lifecycle_shared", "SH"))
        fd = self._open_lock_fd(self.lifecycle_lock_path)
        try:
            if sys.platform != "win32":
                fcntl.flock(fd, fcntl.LOCK_SH)
            else:
                self._win_lock(fd, exclusive=False, nonblocking=False)
            self._get_lifecycle_fds().append(fd)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release_lifecycle_shared(self) -> None:
        with self._events_lock:
            self.events.append(("release_lifecycle_shared", "SH"))
        fds = self._get_lifecycle_fds()
        if fds:
            fd = fds.pop()
            try:
                if sys.platform != "win32":
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    self._win_unlock(fd)
            finally:
                os.close(fd)

    def acquire_lifecycle_exclusive(self) -> None:
        with self._events_lock:
            self.events.append(("acquire_lifecycle_exclusive", "EX"))
        fd = self._open_lock_fd(self.lifecycle_lock_path)
        try:
            if sys.platform != "win32":
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                self._win_lock(fd, exclusive=True, nonblocking=False)
            self._get_lifecycle_fds().append(fd)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release_lifecycle_exclusive(self) -> None:
        with self._events_lock:
            self.events.append(("release_lifecycle_exclusive", "EX"))
        fds = self._get_lifecycle_fds()
        if fds:
            fd = fds.pop()
            try:
                if sys.platform != "win32":
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    self._win_unlock(fd)
            finally:
                os.close(fd)

    # --- Reader Pins ---
    def acquire_pin_shared(self, pin_fd: int) -> None:
        if sys.platform != "win32":
            fcntl.flock(pin_fd, fcntl.LOCK_SH)
        else:
            self._win_lock(pin_fd, exclusive=False, nonblocking=False)

    def acquire_pin_exclusive_nb(self, pin_fd: int) -> None:
        if sys.platform != "win32":
            fcntl.flock(pin_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            self._win_lock(pin_fd, exclusive=True, nonblocking=True)

    def release_pin(self, pin_fd: int) -> None:
        if sys.platform != "win32":
            fcntl.flock(pin_fd, fcntl.LOCK_UN)
        else:
            self._win_unlock(pin_fd)

    # --- Windows Helpers ---
    def _win_lock(self, fd: int, exclusive: bool, nonblocking: bool) -> None:
        import msvcrt
        import ctypes
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(fd)
        flags = 0
        if exclusive:
            flags |= 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
        if nonblocking:
            flags |= 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)
            ]

        ov = OVERLAPPED()
        kernel32 = _get_win32_kernel32()
        ret = kernel32.LockFileEx(
            wintypes.HANDLE(handle),
            wintypes.DWORD(flags),
            0,
            wintypes.DWORD(1),
            wintypes.DWORD(0),
            ctypes.byref(ov)
        )
        if not ret:
            err = ctypes.get_last_error()
            raise ctypes.WinError(err)

    def _win_unlock(self, fd: int) -> None:
        import msvcrt
        import ctypes
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(fd)

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)
            ]

        ov = OVERLAPPED()
        kernel32 = _get_win32_kernel32()
        ret = kernel32.UnlockFileEx(
            wintypes.HANDLE(handle),
            0,
            wintypes.DWORD(1),
            wintypes.DWORD(0),
            ctypes.byref(ov)
        )
        if not ret:
            err = ctypes.get_last_error()
            raise ctypes.WinError(err)


# ---------------------------------------------------------------------------
# Reader Query Session
# ---------------------------------------------------------------------------

class QueryReaderSession:
    """Represents an active, snapshot-pinned reader session (Section 7.2)."""
    def __init__(
        self,
        store: Any,
        session_id: str,
        generation: int,
        global_commit_seq: int,
        captured_active_doc_ids: FrozenSet[str],
        embedding_provider: EmbeddingProvider,
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

    def retrieve(
        self,
        query_text: str,
        top_k: int = 5,
        filter: Optional[Union[RetrievalFilter, Dict[str, Any]]] = None,
        source_type: Optional[Union[KnowledgeSourceType, str]] = None,
    ) -> List[RetrievalResult]:
        """Retrieves ranked results within this pinned reader session (Phase 4B.4)."""
        return self.store.retrieve(
            query_text=query_text,
            top_k=top_k,
            filter=filter,
            source_type=source_type,
            session=self,
        )

    def query_chunks(
        self,
        query_text: str,
        top_k: int = 5,
        source_type: Optional[Union[KnowledgeSourceType, str]] = None,
        filter: Optional[Union[RetrievalFilter, Dict[str, Any]]] = None,
        return_scores: bool = False,
    ) -> Union[List[KnowledgeChunk], List[RetrievalResult]]:
        """Queries chunks within this pinned reader session (Section 8.2)."""
        return self.store.query_chunks(
            query_text=query_text,
            top_k=top_k,
            source_type=source_type,
            filter=filter,
            session=self,
            return_scores=return_scores,
        )

    def __enter__(self) -> QueryReaderSession:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Security Validation Helpers
# ---------------------------------------------------------------------------

FORBIDDEN_FS_TYPES = frozenset({"nfs", "nfs4", "cifs", "smbfs", "9p", "fuse.sshfs", "sshfs"})


def _read_proc_mounts() -> List[str]:
    """Reads lines from /proc/mounts on Linux systems."""
    if sys.platform.startswith("linux") and os.path.exists("/proc/mounts"):
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as f:
                return f.readlines()
        except Exception:
            return []
    return []


def _validate_posix_security(storage_path: Path) -> None:
    """Component-by-component POSIX traversal with O_DIRECTORY | O_NOFOLLOW (Section 11.1)."""
    str_path = os.path.abspath(str(storage_path))

    # 1. Mount verification (Linux /proc/mounts check)
    mount_lines = _read_proc_mounts()
    if mount_lines:
        longest_match = ""
        matched_fstype = ""
        for line in mount_lines:
            parts = line.split()
            if len(parts) >= 3:
                mount_point = parts[1]
                fstype = parts[2].lower()
                if str_path == mount_point or str_path.startswith(mount_point.rstrip("/") + "/"):
                    if len(mount_point) > len(longest_match):
                        longest_match = mount_point
                        matched_fstype = fstype
        if matched_fstype in FORBIDDEN_FS_TYPES:
            raise SecurityStorageError(f"Storage path on forbidden remote filesystem type: {matched_fstype}")

    # 2. Component-by-component traversal with O_DIRECTORY | O_NOFOLLOW (no Path.resolve())
    p = Path(str_path)
    parts = p.parts
    if not parts or parts[0] != "/":
        raise SecurityStorageError(f"Storage path must be absolute: {storage_path}")

    curr_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        for part in parts[1:]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=curr_fd)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    # Component does not exist: create securely inside curr_fd with mode 0700
                    os.mkdir(part, mode=0o700, dir_fd=curr_fd)
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=curr_fd)
                else:
                    raise SecurityStorageError(f"Insecure storage path component '{part}' in '{storage_path}': {e}")
            os.close(curr_fd)
            curr_fd = next_fd

        # 3. Mode 0700 and process UID verification on the pinned descriptor
        st = os.fstat(curr_fd)
        if hasattr(os, "getuid"):
            if st.st_uid != os.getuid():
                raise SecurityStorageError(f"Storage directory owner UID ({st.st_uid}) does not match process UID ({os.getuid()})")
        if (st.st_mode & 0o077) != 0:
            os.fchmod(curr_fd, 0o700)
            st = os.fstat(curr_fd)
            if (st.st_mode & 0o077) != 0:
                raise SecurityStorageError("Storage directory permissions must be 0700")
    finally:
        os.close(curr_fd)


def _validate_windows_security(storage_path: Path) -> None:
    """
    Windows directory validation (Section 11.2):
    - Genuinely handle-anchored traversal: each child component is opened / created
      relative to the open HANDLE of its validated parent directory using ntdll.NtOpenFile
      and ntdll.NtCreateFile with OBJECT_ATTRIBUTES.RootDirectory.
    - Prevents intermediate directory replacement and reparse-point races above the anchor
      as far as user-mode Windows APIs allow.
    - Explicitly verifies that every component is a directory (FILE_ATTRIBUTE_DIRECTORY)
      and is not a reparse point (FILE_ATTRIBUTE_REPARSE_POINT).
    - Preserves pointer-sized HANDLE handling and explicit ctypes declarations.
    - Ensures every successfully opened handle is closed exactly once on every success and failure path.
    - Never calls mkdir or chmod before validation; rejects symlinks/reparse points with zero side effects.

    Threat Model / User-Mode Limitation Statement:
    In user-mode Windows without a kernel filesystem minifilter driver, handle-anchored child
    resolution (NtOpenFile with RootDirectory) guarantees that path resolution begins from
    the kernel directory object referenced by the parent handle, preventing attacks that substitute
    ancestor directories. However, if an attacker with identical user privileges races to modify
    the child entry between handle checks, user space cannot achieve 100% atomic TOCTOU elimination.
    By opening components with FILE_OPEN_REPARSE_POINT and immediately validating BY_HANDLE_FILE_INFORMATION
    on the opened descriptor before proceeding to children, we achieve the maximum possible security
    guarantee supported by the Windows Win32/NT user-mode subsystem.
    """
    if sys.platform != "win32":
        return

    kernel32 = _get_win32_kernel32()
    ntdll = _get_win32_ntdll()

    # Win32 Constants
    GENERIC_READ = 0x80000000
    FILE_TRAVERSE = 0x00000020
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    DRIVE_REMOTE = 4
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    # NT Constants
    OBJ_CASE_INSENSITIVE = 0x00000040
    FILE_DIRECTORY_FILE = 0x00000001
    FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    FILE_OPEN_REPARSE_POINT_NT = 0x00200000
    FILE_CREATE = 0x00000002
    STATUS_SUCCESS = 0x00000000
    STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
    STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
    STATUS_OBJECT_NAME_COLLISION = 0xC0000035
    STATUS_NOT_A_DIRECTORY = 0xC0000103

    abs_path = os.path.abspath(str(storage_path))
    if abs_path.startswith(("\\\\", "//")):
        raise SecurityStorageError(f"Storage path {storage_path} is on a forbidden remote/UNC network path")

    drive, rest = os.path.splitdrive(abs_path)
    if drive:
        drive_path = drive + "\\"
        drive_type = kernel32.GetDriveTypeW(wintypes.LPCWSTR(drive_path))
        if drive_type == DRIVE_REMOTE:
            raise SecurityStorageError(f"Storage path {storage_path} is on a forbidden remote/network drive")

    p = Path(abs_path)
    parts = p.parts
    if not parts:
        raise SecurityStorageError(f"Invalid Windows path: {storage_path}")

    # Step 1: Open the root drive / mount point handle securely with backup semantics & open reparse point
    root_part = parts[0]
    root_handle = kernel32.CreateFileW(
        wintypes.LPCWSTR(root_part),
        wintypes.DWORD(GENERIC_READ | FILE_TRAVERSE),
        wintypes.DWORD(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE),
        None,
        wintypes.DWORD(OPEN_EXISTING),
        wintypes.DWORD(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT),
        None
    )
    if not root_handle or root_handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        raise SecurityStorageError(f"Failed to open root drive component '{root_part}': Windows error {err}")

    curr_handle = wintypes.HANDLE(root_handle)
    try:
        # Verify root handle is directory and not reparse point
        info = BY_HANDLE_FILE_INFORMATION()
        res = kernel32.GetFileInformationByHandle(curr_handle, ctypes.byref(info))
        if not res:
            err = ctypes.get_last_error()
            raise SecurityStorageError(f"GetFileInformationByHandle failed on root '{root_part}': Windows error {err}")
        if not (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY):
            raise SecurityStorageError(f"Root path component '{root_part}' is not a directory")
        if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise SecurityStorageError(f"Root path component '{root_part}' is an insecure reparse point")

        # Step 2: Handle-anchored component-by-component traversal
        for part in parts[1:]:
            if part in (".", ""):
                continue
            if part == "..":
                raise SecurityStorageError(f"Directory traversal component '..' not permitted: {storage_path}")

            u_str, raw_buf = _create_unicode_string(part)
            p_u_str = ctypes.pointer(u_str)
            oa = OBJECT_ATTRIBUTES(
                Length=ctypes.sizeof(OBJECT_ATTRIBUTES),
                RootDirectory=curr_handle,
                ObjectName=p_u_str,
                Attributes=OBJ_CASE_INSENSITIVE,
                SecurityDescriptor=None,
                SecurityQualityOfService=None
            )

            child_handle = wintypes.HANDLE()
            iosb = IO_STATUS_BLOCK()

            # Attempt to open existing child component relative to parent handle
            status = ntdll.NtOpenFile(
                ctypes.byref(child_handle),
                wintypes.DWORD(GENERIC_READ | FILE_TRAVERSE),
                ctypes.byref(oa),
                ctypes.byref(iosb),
                wintypes.DWORD(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE),
                wintypes.DWORD(FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT_NT | FILE_SYNCHRONOUS_IO_NONALERT)
            )

            st_unsigned = status & 0xFFFFFFFF
            if st_unsigned in (STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND):
                # Component does not exist: create directory securely inside parent handle
                child_handle = wintypes.HANDLE()
                iosb = IO_STATUS_BLOCK()
                status_create = ntdll.NtCreateFile(
                    ctypes.byref(child_handle),
                    wintypes.DWORD(GENERIC_READ | FILE_TRAVERSE),
                    ctypes.byref(oa),
                    ctypes.byref(iosb),
                    None,
                    wintypes.DWORD(FILE_ATTRIBUTE_DIRECTORY),
                    wintypes.DWORD(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE),
                    wintypes.DWORD(FILE_CREATE),
                    wintypes.DWORD(FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT_NT | FILE_SYNCHRONOUS_IO_NONALERT),
                    None,
                    0
                )
                create_unsigned = status_create & 0xFFFFFFFF
                if create_unsigned == STATUS_OBJECT_NAME_COLLISION:
                    # Concurrently created: retry opening
                    child_handle = wintypes.HANDLE()
                    iosb = IO_STATUS_BLOCK()
                    status = ntdll.NtOpenFile(
                        ctypes.byref(child_handle),
                        wintypes.DWORD(GENERIC_READ | FILE_TRAVERSE),
                        ctypes.byref(oa),
                        ctypes.byref(iosb),
                        wintypes.DWORD(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE),
                        wintypes.DWORD(FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT_NT | FILE_SYNCHRONOUS_IO_NONALERT)
                    )
                    st_unsigned = status & 0xFFFFFFFF
                    if st_unsigned != STATUS_SUCCESS:
                        raise SecurityStorageError(f"Failed to open path component '{part}' after collision: NTSTATUS 0x{st_unsigned:08X}")
                elif create_unsigned != STATUS_SUCCESS:
                    raise SecurityStorageError(f"Failed to create directory component '{part}' relative to parent: NTSTATUS 0x{create_unsigned:08X}")
            elif st_unsigned == STATUS_NOT_A_DIRECTORY:
                raise SecurityStorageError(f"Storage path component '{part}' is not a directory")
            elif st_unsigned != STATUS_SUCCESS:
                raise SecurityStorageError(f"Failed to open path component '{part}': NTSTATUS 0x{st_unsigned:08X}")

            # Child handle is successfully opened: verify attributes with strict leak-free lifecycle
            try:
                info = BY_HANDLE_FILE_INFORMATION()
                res = kernel32.GetFileInformationByHandle(child_handle, ctypes.byref(info))
                if not res:
                    err = ctypes.get_last_error()
                    raise SecurityStorageError(f"GetFileInformationByHandle failed on '{part}': Windows error {err}")

                if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    raise SecurityStorageError(f"Insecure storage path reparse point detected at '{part}'")

                if not (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY):
                    raise SecurityStorageError(f"Storage path component '{part}' is not a directory")

                # Advance anchor: promote child handle and close old parent handle
                old_parent = curr_handle
                curr_handle = child_handle
                child_handle = None  # Handoff complete: curr_handle owns descriptor
                kernel32.CloseHandle(old_parent)
            finally:
                if child_handle and getattr(child_handle, "value", None) not in (None, 0, INVALID_HANDLE_VALUE):
                    kernel32.CloseHandle(child_handle)
    finally:
        if curr_handle and getattr(curr_handle, "value", None) not in (None, 0, INVALID_HANDLE_VALUE):
            kernel32.CloseHandle(curr_handle)


def validate_storage_directory_security(storage_path: Path, skip_checks: bool = False) -> None:
    """
    Validates storage root security invariants (Section 11.1 & 11.2):
    - Component-by-component POSIX traversal with O_DIRECTORY | O_NOFOLLOW (no Path.resolve()).
    - Windows handle-based component-by-component reparse-point and remote-drive validation.
    - Remote filesystem mount rejection.
    - Never calls mkdir or chmod before validating path components; rejects symlinks/reparse points with zero side effects.
    - Mode 0700 and current process UID owner verification on the pinned descriptor.
    """
    if skip_checks:
        return

    if sys.platform != "win32":
        _validate_posix_security(storage_path)
    else:
        _validate_windows_security(storage_path)


def _validate_chunk_consistency(doc: KnowledgeDocument, chunks: List[KnowledgeChunk]) -> None:
    """Validates that every chunk's document_id and source_type match the parent KnowledgeDocument."""
    for i, c in enumerate(chunks):
        if c.document_id != doc.document_id:
            raise DocumentChunkMismatchError(
                f"Chunk at index {i} (id={c.chunk_id}) document_id '{c.document_id}' "
                f"does not match parent document_id '{doc.document_id}'"
            )
        if c.source_type != doc.source_type:
            raise DocumentChunkMismatchError(
                f"Chunk at index {i} (id={c.chunk_id}) source_type '{c.source_type}' "
                f"does not match parent source_type '{doc.source_type}'"
            )


# ---------------------------------------------------------------------------
# LocalKnowledgeStore Implementation
# ---------------------------------------------------------------------------

class LocalKnowledgeStore:
    """
    Local Knowledge Storage and Indexing Service (Phase 4B.3).
    Implements coordinator SQLite database, reconstructible Chroma vector index,
    fail-closed reader pin probing, and snapshot-isolated retrieval.
    """
    def __init__(
        self,
        config: StoreConfig,
        provider: Optional[EmbeddingProvider] = None,
        provider_registry: Optional[ProviderRegistry] = None,
        auto_reconcile: bool = True
    ):
        self.config = config
        self.provider_registry = provider_registry or GLOBAL_PROVIDER_REGISTRY

        # Initialize directories
        self.storage_path = self.config.storage_path
        validate_storage_directory_security(self.storage_path, skip_checks=self.config.skip_security_checks)

        self.locks_dir = self.storage_path / "locks"
        self.pins_dir = self.storage_path / "pins"
        self.chroma_dir = self.storage_path / "chroma"
        self.db_path = self.storage_path / "coordinator.sqlite3"

        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.pins_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)

        os.chmod(self.locks_dir, 0o700)
        os.chmod(self.pins_dir, 0o700)
        os.chmod(self.chroma_dir, 0o700)

        self._lock_manager = LockManager(self.locks_dir)
        self.conn_manager = ConnectionManager(self.db_path, busy_timeout_ms=self.config.busy_timeout_ms)

        init_provider = provider or DeterministicHashEmbeddingProvider()
        self.provider_registry.register(init_provider)

        # Database initialization & provider verification under writer lock
        self._lock_manager.acquire_writer_lock()
        try:
            conn = self.conn_manager.get_connection()
            initialize_database(conn, init_provider)

            manifest_row = conn.execute(
                "SELECT embedding_provider, embedding_model, embedding_dimension, embedding_config_version "
                "FROM storage_manifest WHERE manifest_key = 'manifest';"
            ).fetchone()

            if manifest_row is None:
                raise CorruptedGlobalStateError("Storage manifest row missing after initialization")

            m_provider = manifest_row["embedding_provider"]
            m_model = manifest_row["embedding_model"]
            m_dim = manifest_row["embedding_dimension"]
            m_cfg = manifest_row["embedding_config_version"]

            # If user provided an explicit provider, assert match against manifest
            if (
                init_provider.provider_id != m_provider
                or init_provider.model_name != m_model
                or init_provider.dimension != m_dim
                or init_provider.config_version != m_cfg
            ):
                raise EmbeddingProviderMismatchError(
                    f"Provided embedding config ({init_provider.provider_id}:{init_provider.model_name}:{init_provider.dimension}) "
                    f"does not match manifest ({m_provider}:{m_model}:{m_dim})"
                )

            self.active_provider = self._resolve_provider(m_provider, m_model, m_dim, m_cfg)

            # Initialize ChromaDB persistent client
            chroma_settings = ChromaSettings(anonymized_telemetry=False, is_persistent=True)
            self.chroma_client = chromadb.PersistentClient(path=str(self.chroma_dir), settings=chroma_settings)
        finally:
            self._lock_manager.release_writer_lock()

        if auto_reconcile:
            self.reconcile_startup()

    def _resolve_provider(self, p_id: str, m_name: str, dim: int, cfg_v: str) -> EmbeddingProvider:
        return self.provider_registry.resolve(p_id, m_name, dim, cfg_v)

    def _get_or_create_collection(self, generation: int, provider: Optional[EmbeddingProvider] = None) -> Any:
        col_name = f"chunks_gen_{generation}"
        p = provider or self.active_provider
        return self.chroma_client.get_or_create_collection(
            name=col_name,
            metadata={"generation": generation, "provider": p.provider_id, "model": p.model_name, "dimension": p.dimension}
        )

    # -----------------------------------------------------------------------
    # Pin Liveness Probing (Section 9.3)
    # -----------------------------------------------------------------------

    def probe_pin_liveness(self, pin_path: Path) -> PinStatus:
        """
        Probes a reader pin file to determine if a live process holds an OS kernel shared lock.
        Fail-closed contract:
        - Returns PinStatus.STALE only if non-blocking exclusive lock is acquired and released.
        - Returns PinStatus.LIVE on recognized kernel lock contention.
        - Returns PinStatus.UNKNOWN on any unexpected error (fail-closed).
        """
        pin_fd = -1
        try:
            try:
                pin_fd = os.open(str(pin_path), os.O_RDWR)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    return PinStatus.STALE  # File unlinked concurrently
                return PinStatus.UNKNOWN  # Permission or I/O failure -> fail closed

            try:
                self._lock_manager.acquire_pin_exclusive_nb(pin_fd)
                # Successfully acquired -> no live reader holds shared lock
                self._lock_manager.release_pin(pin_fd)
                return PinStatus.STALE
            except (BlockingIOError, OSError) as e:
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

    # -----------------------------------------------------------------------
    # Ingestion & Replacement Protocols (Section 5)
    # -----------------------------------------------------------------------

    def ingest_document(
        self,
        doc: KnowledgeDocument,
        chunks: List[KnowledgeChunk],
        source_identity: str,
        raw_run_id_sha256: Optional[str] = None
    ) -> None:
        """First-time document ingestion protocol with relational FK placeholder (Section 5.1)."""
        _validate_chunk_consistency(doc, chunks)
        now = datetime.now(timezone.utc).isoformat()
        self._lock_manager.acquire_writer_lock()
        try:
            conn = self.conn_manager.get_connection()
            active_gen = conn.execute("SELECT active_generation FROM storage_manifest WHERE manifest_key = 'manifest';").fetchone()[0]

            # Step 1: Staging Phase
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # 1. Upsert placeholder in source_catalog with active_document_id = NULL
                conn.execute(
                    "INSERT INTO source_catalog (source_identity, source_type, active_document_id, raw_run_id_sha256, commit_seq, created_at, updated_at) "
                    "VALUES (:source_id, :source_type, NULL, :raw_run_sha, 0, :now, :now) "
                    "ON CONFLICT(source_identity) DO NOTHING;",
                    {
                        "source_id": source_identity,
                        "source_type": doc.source_type.value,
                        "raw_run_sha": raw_run_id_sha256,
                        "now": now
                    }
                )
                catalog_row = conn.execute(
                    "SELECT source_type, raw_run_id_sha256, active_document_id FROM source_catalog WHERE source_identity = :source_id;",
                    {"source_id": source_identity}
                ).fetchone()

                if catalog_row["source_type"] != doc.source_type.value:
                    raise SourceTypeMismatchError(f"Source exists with type {catalog_row['source_type']}, got {doc.source_type.value}")
                if raw_run_id_sha256 and catalog_row["raw_run_id_sha256"] != raw_run_id_sha256:
                    raise IdentityCollisionError(f"Source collision: raw run hash mismatch for {source_identity}")
                if catalog_row["active_document_id"] is not None:
                    raise ConcurrencyConflictError(
                        f"Source {source_identity} already has an active document {catalog_row['active_document_id']}; use replace_document"
                    )

                # 2. Insert canonical document with status 'pending'
                fingerprint = hashlib.sha256("".join(c.content for c in chunks).encode("utf-8")).hexdigest()
                doc_params = {
                    "doc_id": doc.document_id,
                    "source_id": source_identity,
                    "source_type": doc.source_type.value,
                    "title": doc.title,
                    "source_ref": doc.source_ref,
                    "http_method": doc.http_method,
                    "path_template": doc.path_template,
                    "operation_id": doc.operation_id,
                    "created_at": doc.created_at.isoformat(),
                    "metadata_json": json.dumps(doc.metadata),
                    "fingerprint": fingerprint,
                    "chunk_count": len(chunks)
                }
                c_doc = conn.execute(
                    "INSERT INTO canonical_documents (document_id, source_identity, source_type, title, source_ref, http_method, "
                    "path_template, operation_id, created_at, metadata_json, content_fingerprint, chunk_count, status) "
                    "VALUES (:doc_id, :source_id, :source_type, :title, :source_ref, :http_method, :path_template, :operation_id, "
                    ":created_at, :metadata_json, :fingerprint, :chunk_count, 'pending');",
                    doc_params
                )
                if c_doc.rowcount != 1:
                    raise StorageTransactionError("Failed to insert pending canonical document")

                # 3. Batch insert canonical chunks
                chunk_tuples = [
                    (
                        doc.document_id,
                        c.chunk_id,
                        c.chunk_index,
                        source_identity,
                        c.source_type.value,
                        c.content,
                        c.http_method,
                        c.path_template,
                        c.operation_id,
                        c.failure_category if isinstance(c.failure_category, str) or c.failure_category is None else c.failure_category.value,
                        c.source_ref,
                        json.dumps(c.metadata)
                    )
                    for c in chunks
                ]
                if chunk_tuples:
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

            # ChromaDB Indexing
            if chunks:
                col = self._get_or_create_collection(active_gen)
                storage_ids = [format_chroma_id(doc.document_id, c.chunk_id) for c in chunks]
                texts = [c.content for c in chunks]
                metas = [
                    {
                        "document_id": doc.document_id,
                        "chunk_id": c.chunk_id,
                        "chunk_index": c.chunk_index,
                        "source_identity": source_identity,
                        "source_type": c.source_type.value,
                        "generation": active_gen
                    }
                    for c in chunks
                ]
                embeddings = self.active_provider.embed_batch(texts)
                col.add(ids=storage_ids, embeddings=embeddings, metadatas=metas)

            # Step 2: Commit Boundary (under lifecycle.lock EX)
            self._lock_manager.acquire_lifecycle_exclusive()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                # Increment global commit seq
                c_seq = conn.execute(
                    "UPDATE global_state SET global_commit_seq = global_commit_seq + 1, updated_at = :now WHERE singleton_key = 'state';",
                    {"now": now}
                )
                if c_seq.rowcount != 1:
                    raise CorruptedGlobalStateError("global_state singleton missing during commit")

                # Activate in source_catalog conditionally
                c_cat = conn.execute(
                    "UPDATE source_catalog SET active_document_id = :doc_id, "
                    "commit_seq = (SELECT global_commit_seq FROM global_state WHERE singleton_key = 'state'), updated_at = :now "
                    "WHERE source_identity = :source_id AND active_document_id IS NULL;",
                    {"doc_id": doc.document_id, "now": now, "source_id": source_identity}
                )
                if c_cat.rowcount != 1:
                    raise ConcurrencyConflictError("Source active_document_id changed concurrently during initial ingestion")

                # Transition document status to active conditionally
                c_act = conn.execute(
                    "UPDATE canonical_documents SET status = 'active' WHERE document_id = :doc_id AND status = 'pending';",
                    {"doc_id": doc.document_id}
                )
                if c_act.rowcount != 1:
                    raise ConcurrencyConflictError("Document status modified concurrently during initial ingestion")

                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            finally:
                self._lock_manager.release_lifecycle_exclusive()
        finally:
            self._lock_manager.release_writer_lock()

    def replace_document(
        self,
        old_doc_id: str,
        new_doc: KnowledgeDocument,
        new_chunks: List[KnowledgeChunk],
        source_identity: str
    ) -> None:
        """Atomic document replacement protocol D_old -> D_new (Section 5.2)."""
        _validate_chunk_consistency(new_doc, new_chunks)
        now = datetime.now(timezone.utc).isoformat()
        self._lock_manager.acquire_writer_lock()
        try:
            conn = self.conn_manager.get_connection()
            active_gen = conn.execute("SELECT active_generation FROM storage_manifest WHERE manifest_key = 'manifest';").fetchone()[0]

            # Verify old_doc_id is currently active
            cat = conn.execute(
                "SELECT active_document_id FROM source_catalog WHERE source_identity = :source_id;",
                {"source_id": source_identity}
            ).fetchone()
            if not cat or cat["active_document_id"] != old_doc_id:
                raise ConcurrencyConflictError(f"Document {old_doc_id} is not the active document for source {source_identity}")

            # Step 1: Staging Phase
            conn.execute("BEGIN IMMEDIATE;")
            try:
                fingerprint = hashlib.sha256("".join(c.content for c in new_chunks).encode("utf-8")).hexdigest()
                doc_params = {
                    "doc_id": new_doc.document_id,
                    "source_id": source_identity,
                    "source_type": new_doc.source_type.value,
                    "title": new_doc.title,
                    "source_ref": new_doc.source_ref,
                    "http_method": new_doc.http_method,
                    "path_template": new_doc.path_template,
                    "operation_id": new_doc.operation_id,
                    "created_at": new_doc.created_at.isoformat(),
                    "metadata_json": json.dumps(new_doc.metadata),
                    "fingerprint": fingerprint,
                    "chunk_count": len(new_chunks)
                }
                c_doc = conn.execute(
                    "INSERT INTO canonical_documents (document_id, source_identity, source_type, title, source_ref, http_method, "
                    "path_template, operation_id, created_at, metadata_json, content_fingerprint, chunk_count, status) "
                    "VALUES (:doc_id, :source_id, :source_type, :title, :source_ref, :http_method, :path_template, :operation_id, "
                    ":created_at, :metadata_json, :fingerprint, :chunk_count, 'pending');",
                    doc_params
                )
                if c_doc.rowcount != 1:
                    raise StorageTransactionError("Failed to insert pending replacement canonical document")

                chunk_tuples = [
                    (
                        new_doc.document_id,
                        c.chunk_id,
                        c.chunk_index,
                        source_identity,
                        c.source_type.value,
                        c.content,
                        c.http_method,
                        c.path_template,
                        c.operation_id,
                        c.failure_category if isinstance(c.failure_category, str) or c.failure_category is None else c.failure_category.value,
                        c.source_ref,
                        json.dumps(c.metadata)
                    )
                    for c in new_chunks
                ]
                if chunk_tuples:
                    conn.executemany("INSERT INTO canonical_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);", chunk_tuples)

                conn.execute(
                    "INSERT INTO document_generation_index (document_id, generation, indexed_at) VALUES (:doc_id, :gen, :now);",
                    {"doc_id": new_doc.document_id, "gen": active_gen, "now": now}
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

            # ChromaDB Indexing
            if new_chunks:
                col = self._get_or_create_collection(active_gen)
                storage_ids = [format_chroma_id(new_doc.document_id, c.chunk_id) for c in new_chunks]
                texts = [c.content for c in new_chunks]
                metas = [
                    {
                        "document_id": new_doc.document_id,
                        "chunk_id": c.chunk_id,
                        "chunk_index": c.chunk_index,
                        "source_identity": source_identity,
                        "source_type": c.source_type.value,
                        "generation": active_gen
                    }
                    for c in new_chunks
                ]
                embeddings = self.active_provider.embed_batch(texts)
                col.add(ids=storage_ids, embeddings=embeddings, metadatas=metas)

            # Step 2: Commit Boundary (under lifecycle.lock EX)
            self._lock_manager.acquire_lifecycle_exclusive()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                c_seq = conn.execute(
                    "UPDATE global_state SET global_commit_seq = global_commit_seq + 1, updated_at = :now WHERE singleton_key = 'state';",
                    {"now": now}
                )
                if c_seq.rowcount != 1:
                    raise CorruptedGlobalStateError("global_state singleton missing during replacement commit")

                c_cat = conn.execute(
                    "UPDATE source_catalog SET active_document_id = :d_new, "
                    "commit_seq = (SELECT global_commit_seq FROM global_state WHERE singleton_key = 'state'), updated_at = :now "
                    "WHERE source_identity = :source_id AND active_document_id = :d_old;",
                    {"d_new": new_doc.document_id, "d_old": old_doc_id, "source_id": source_identity, "now": now}
                )
                if c_cat.rowcount != 1:
                    raise ConcurrencyConflictError(f"Document replacement race: {old_doc_id} is no longer active for source {source_identity}")

                c_act = conn.execute(
                    "UPDATE canonical_documents SET status = 'active' WHERE document_id = :d_new AND status = 'pending';",
                    {"d_new": new_doc.document_id}
                )
                if c_act.rowcount != 1:
                    raise StorageTransactionError("Failed to activate new document version")

                c_ret = conn.execute(
                    "UPDATE canonical_documents SET status = 'retired' WHERE document_id = :d_old AND status = 'active';",
                    {"d_old": old_doc_id}
                )
                if c_ret.rowcount != 1:
                    raise StorageTransactionError("Failed to retire old document version")

                conn.execute(
                    "INSERT INTO cleanup_registry (document_id, generation, cleanup_state, registered_at, updated_at) "
                    "SELECT :d_old, generation, 'RETIRED', :now, :now "
                    "FROM document_generation_index WHERE document_id = :d_old;",
                    {"d_old": old_doc_id, "now": now}
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            finally:
                self._lock_manager.release_lifecycle_exclusive()
        finally:
            self._lock_manager.release_writer_lock()

    # -----------------------------------------------------------------------
    # Deletion Protocols (Section 6)
    # -----------------------------------------------------------------------

    def delete_document(self, doc_id: str) -> bool:
        """Deactivates and retires an active document, registering it for cleanup (Section 6.1)."""
        now = datetime.now(timezone.utc).isoformat()
        self._lock_manager.acquire_writer_lock()
        try:
            self._lock_manager.acquire_lifecycle_exclusive()
            try:
                conn = self.conn_manager.get_connection()
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    doc_row = conn.execute(
                        "SELECT source_identity, status FROM canonical_documents WHERE document_id = :doc_id;",
                        {"doc_id": doc_id}
                    ).fetchone()
                    if not doc_row or doc_row["status"] in ("retired", "tombstoned"):
                        conn.execute("COMMIT;")
                        return False  # Idempotent: already inactive or absent

                    source_id = doc_row["source_identity"]

                    c_seq = conn.execute(
                        "UPDATE global_state SET global_commit_seq = global_commit_seq + 1, updated_at = :now WHERE singleton_key = 'state';",
                        {"now": now}
                    )
                    if c_seq.rowcount != 1:
                        raise CorruptedGlobalStateError("global_state singleton missing during deletion")

                    c_cat = conn.execute(
                        "UPDATE source_catalog SET active_document_id = NULL, "
                        "commit_seq = (SELECT global_commit_seq FROM global_state WHERE singleton_key = 'state'), updated_at = :now "
                        "WHERE source_identity = :source_id AND active_document_id = :doc_id;",
                        {"source_id": source_id, "doc_id": doc_id, "now": now}
                    )
                    if c_cat.rowcount != 1:
                        raise ConcurrencyConflictError("Document is no longer the active pointer for source")

                    c_doc = conn.execute(
                        "UPDATE canonical_documents SET status = 'retired' WHERE document_id = :doc_id AND status = 'active';",
                        {"doc_id": doc_id}
                    )
                    if c_doc.rowcount != 1:
                        raise StorageTransactionError("Failed to mark document retired")

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
            finally:
                self._lock_manager.release_lifecycle_exclusive()
        finally:
            self._lock_manager.release_writer_lock()

    def delete_source(self, source_identity: str) -> bool:
        """Deletes the active document for a source while retaining catalog metadata (Section 6.2)."""
        self._lock_manager.acquire_writer_lock()
        try:
            conn = self.conn_manager.get_connection()
            rows = conn.execute(
                "SELECT active_document_id FROM source_catalog WHERE source_identity = :source_id;",
                {"source_id": source_identity}
            ).fetchall()
            if not rows or not rows[0]["active_document_id"]:
                return False
            active_doc_id = rows[0]["active_document_id"]
        finally:
            self._lock_manager.release_writer_lock()

        return self.delete_document(active_doc_id)

    # -----------------------------------------------------------------------
    # Cleanup State Machine (Section 6.3)
    # -----------------------------------------------------------------------

    def _run_cleanup_pass_locked(self, conn: sqlite3.Connection) -> None:
        """
        Internal cleanup pass executed under writer lock and lifecycle exclusive lock.
        Never acquires or releases locks, preventing reentrancy deadlocks on non-reentrant locks.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Transition 1: RETIRED -> TOMBSTONED (if not protected by any active snapshot)
        candidates = conn.execute(
            "SELECT document_id, generation FROM cleanup_registry WHERE cleanup_state = 'RETIRED';"
        ).fetchall()
        for row in candidates:
            doc_id, gen = row["document_id"], row["generation"]
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

            # ChromaDB Deletion
            collection_name = f"chunks_gen_{gen}"
            if collection_name in [c.name for c in self.chroma_client.list_collections()]:
                col = self.chroma_client.get_collection(collection_name)
                col.delete(where={"document_id": doc_id})
                remaining = col.get(where={"document_id": doc_id}, limit=1)
                if len(remaining["ids"]) > 0:
                    continue  # Defer completing until confirmed 0 chunks

            # Mark completed: atomic removal of cleanup_registry and document_generation_index
            conn.execute("BEGIN IMMEDIATE;")
            try:
                c_del = conn.execute(
                    "DELETE FROM cleanup_registry WHERE document_id = :doc_id AND generation = :gen AND cleanup_state = 'PURGING';",
                    {"doc_id": doc_id, "gen": gen}
                )
                if c_del.rowcount != 1:
                    conn.execute("ROLLBACK;")
                    continue

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
                has_cleanup = conn.execute(
                    "SELECT 1 FROM cleanup_registry WHERE document_id = :doc_id;", {"doc_id": p_id}
                ).fetchone()
                if has_cleanup:
                    conn.execute("ROLLBACK;")
                    continue

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
        """Public cleanup entrypoint: acquires writer lock EX and lifecycle lock EX exactly once."""
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

    # -----------------------------------------------------------------------
    # Retired-Generation Pruning (Section 9.3)
    # -----------------------------------------------------------------------

    def _prune_retired_generations_locked(self, conn: sqlite3.Connection) -> None:
        """
        Internal pruning routine executed under writer lock and lifecycle exclusive lock.
        Never acquires or releases locks.
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
                continue

            # 2. Check filesystem pins (fail-closed)
            can_prune = True
            if self.pins_dir.exists():
                for pin_file in self.pins_dir.glob("*.pin"):
                    pin_status = self.probe_pin_liveness(pin_file)
                    if pin_status != PinStatus.STALE:
                        can_prune = False
                        break

            if not can_prune:
                continue

            # 3. Chroma Collection Deletion FIRST
            try:
                if col_name in [c.name for c in self.chroma_client.list_collections()]:
                    self.chroma_client.delete_collection(col_name)
            except Exception:
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
        """Internal migration pruning entrypoint: assumes writer lock is already held."""
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

    # -----------------------------------------------------------------------
    # Reader Snapshots and Querying (Section 7 & 8)
    # -----------------------------------------------------------------------

    def acquire_query_session(self) -> QueryReaderSession:
        """Atomically captures reader snapshot bound to active generation provider (Section 7.1)."""
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        pin_path = self.pins_dir / f"{session_id}.pin"
        pin_fd = -1
        snapshot_committed = False

        try:
            self._lock_manager.acquire_lifecycle_shared()
            try:
                pin_fd = os.open(str(pin_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                self._lock_manager.acquire_pin_shared(pin_fd)

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
                self._lock_manager.release_lifecycle_shared()

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
            # Failure rollback path (Section 7.1 step 8)
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
                        raise
                    finally:
                        self._lock_manager.release_lifecycle_shared()
                except Exception:
                    raise

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

    def release_query_session(self, session: QueryReaderSession) -> None:
        """Releases query session resources: SQLite cleanup first, OS kernel pin release second (Section 7.2)."""
        # Phase 1: Database cleanup
        if not session._db_cleaned:
            self._lock_manager.acquire_lifecycle_shared()
            try:
                conn = self.conn_manager.get_connection()
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute("DELETE FROM snapshot_protected_documents WHERE session_id = ?;", (session.session_id,))
                conn.execute("DELETE FROM active_snapshots WHERE session_id = ?;", (session.session_id,))
                conn.execute("COMMIT;")
                session._db_cleaned = True
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            finally:
                self._lock_manager.release_lifecycle_shared()

        # Phase 2: OS Kernel Pin Release (only once DB commit succeeds)
        if not session._pin_released:
            try:
                self._lock_manager.release_pin(session.pin_fd)
            finally:
                try:
                    os.close(session.pin_fd)
                except OSError:
                    pass
                try:
                    os.unlink(session.pin_path)
                except FileNotFoundError:
                    pass
                session._pin_released = True

    def retrieve(
        self,
        query_text: str,
        top_k: int = 5,
        filter: Optional[Union[RetrievalFilter, Dict[str, Any]]] = None,
        source_type: Optional[Union[KnowledgeSourceType, str]] = None,
        session: Optional[QueryReaderSession] = None,
    ) -> List[RetrievalResult]:
        """
        Executes semantic vector search against pinned generation and hydrates canonical records from SQLite (Phase 4B.4).
        Applies SQLite canonical metadata filtering with bounded candidate expansion.
        Validates store object identity (session.store is self).
        """
        if not isinstance(query_text, str):
            raise ValueError(f"query_text must be a string, got {type(query_text).__name__}")
        cleaned_query = query_text.strip()
        if not cleaned_query:
            raise ValueError("query_text cannot be empty or whitespace-only")
        if len(cleaned_query) > 16384:
            raise ValueError(f"query_text exceeds maximum length of 16384 characters (got {len(cleaned_query)})")

        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise ValueError(f"top_k must be an integer, got {type(top_k).__name__}")
        if top_k <= 0 or top_k > 1000:
            raise ValueError(f"top_k must be between 1 and 1000, got {top_k}")

        parsed_filter: Optional[RetrievalFilter] = None
        if filter is not None:
            if isinstance(filter, RetrievalFilter):
                parsed_filter = filter
            elif isinstance(filter, dict):
                parsed_filter = RetrievalFilter(**filter)
            else:
                raise ValueError(f"filter must be a RetrievalFilter or dict, got {type(filter).__name__}")

            if source_type is not None:
                st = KnowledgeSourceType(source_type.strip().lower() if isinstance(source_type, str) else source_type)
                if parsed_filter.source_type is not None and parsed_filter.source_type != st:
                    raise ValueError(f"Conflicting source_type provided: argument '{st.value}' vs filter '{parsed_filter.source_type.value}'")
                elif parsed_filter.source_type is None:
                    parsed_filter = parsed_filter.model_copy(update={"source_type": st})
        elif source_type is not None:
            st = KnowledgeSourceType(source_type.strip().lower() if isinstance(source_type, str) else source_type)
            parsed_filter = RetrievalFilter(source_type=st)

        if session is not None:
            if session.store is not self:
                raise InvalidSessionError("Session belongs to another store instance")
            if not session.is_active:
                raise ClosedSessionError("Query session is closed")

        managed_session: Optional[QueryReaderSession] = None
        if session is None:
            managed_session = self.acquire_query_session()
            s = managed_session
        else:
            s = session

        try:
            # Fast path: Empty active document set returns [] without calling Chroma
            if not s.captured_active_doc_ids:
                return []

            # 1. Embed query using session's bound provider
            query_vector = s.embedding_provider.embed_text(cleaned_query)

            # 2. Vector search in ChromaDB pinned generation
            collection_name = f"chunks_gen_{s.generation}"
            if collection_name not in [c.name for c in self.chroma_client.list_collections()]:
                return []
            col = self.chroma_client.get_collection(collection_name)
            total_chunks = col.count()
            if total_chunks == 0:
                return []

            chroma_where: Dict[str, Any] = {"document_id": {"$in": list(s.captured_active_doc_ids)}}
            if parsed_filter is not None and parsed_filter.source_type is not None:
                chroma_where = {"$and": [chroma_where, {"source_type": parsed_filter.source_type.value}]}

            matched_results: List[RetrievalResult] = []
            seen_chunk_keys: Set[Tuple[str, str]] = set()
            current_k = min(total_chunks, max(top_k * 4, 20))

            while len(matched_results) < top_k:
                query_n = min(total_chunks, current_k)
                results = col.query(
                    query_embeddings=[query_vector],
                    n_results=query_n,
                    where=chroma_where,
                    include=["distances"]
                )
                raw_ids = results["ids"][0] if results and results.get("ids") else []
                raw_dists = results["distances"][0] if results and results.get("distances") else []

                new_candidates: List[Tuple[str, str, float]] = []
                for cid, dist in zip(raw_ids, raw_dists):
                    doc_id, chunk_id = parse_chroma_id(cid)
                    if doc_id not in s.captured_active_doc_ids:
                        continue
                    pair = (doc_id, chunk_id)
                    if pair in seen_chunk_keys:
                        continue
                    seen_chunk_keys.add(pair)
                    new_candidates.append((doc_id, chunk_id, float(dist)))

                if new_candidates:
                    conn = self.conn_manager.get_connection()
                    pairs_for_sql = [(doc_id, chunk_id) for doc_id, chunk_id, _ in new_candidates]
                    placeholders = ", ".join(["(?, ?)"] * len(pairs_for_sql))
                    flat_params = [item for p in pairs_for_sql for item in p]
                    sql = (
                        "SELECT document_id, chunk_id, chunk_index, source_identity, source_type, content, "
                        "http_method, path_template, operation_id, failure_category, source_ref, metadata_json "
                        f"FROM canonical_chunks WHERE (document_id, chunk_id) IN (VALUES {placeholders});"
                    )
                    rows = conn.execute(sql, flat_params).fetchall()
                    row_map = {(r["document_id"], r["chunk_id"]): r for r in rows}

                    for doc_id, chunk_id, dist in new_candidates:
                        pair = (doc_id, chunk_id)
                        if pair not in row_map:
                            raise CorruptedIndexIntegrityError(f"Chroma returned chunk missing from canonical storage: {pair}")
                        r = row_map[pair]

                        if parsed_filter is not None:
                            if parsed_filter.source_type is not None:
                                if r["source_type"] != parsed_filter.source_type.value:
                                    continue
                            if parsed_filter.http_method is not None:
                                if r["http_method"] != parsed_filter.http_method:
                                    continue
                            if parsed_filter.path_template is not None:
                                if r["path_template"] != parsed_filter.path_template:
                                    continue
                            if parsed_filter.operation_id is not None:
                                if r["operation_id"] != parsed_filter.operation_id:
                                    continue
                            if parsed_filter.failure_category is not None:
                                expected_fc = (
                                    parsed_filter.failure_category.value
                                    if isinstance(parsed_filter.failure_category, FailureCategory)
                                    else parsed_filter.failure_category
                                )
                                if r["failure_category"] != expected_fc:
                                    continue
                            if parsed_filter.metadata:
                                chunk_meta = json.loads(r["metadata_json"])
                                mismatch = False
                                for mk, mv in parsed_filter.metadata.items():
                                    if chunk_meta.get(mk) != mv:
                                        mismatch = True
                                        break
                                if mismatch:
                                    continue

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
                        score = compute_relevance_score(dist)
                        matched_results.append(RetrievalResult(chunk=chunk, score=score, distance=round(dist, 6)))
                        if len(matched_results) >= top_k:
                            break

                if len(matched_results) >= top_k:
                    break
                if query_n >= total_chunks:
                    break
                if len(raw_ids) < query_n:
                    break
                next_k = max(current_k * 4, current_k + top_k * 4)
                current_k = min(total_chunks, next_k)

            return matched_results[:top_k]
        finally:
            if managed_session is not None:
                self.release_query_session(managed_session)

    def query_chunks(
        self,
        query_text: str,
        top_k: int = 5,
        source_type: Optional[Union[KnowledgeSourceType, str]] = None,
        filter: Optional[Union[RetrievalFilter, Dict[str, Any]]] = None,
        session: Optional[QueryReaderSession] = None,
        return_scores: bool = False,
    ) -> Union[List[KnowledgeChunk], List[RetrievalResult]]:
        """
        Executes vector search against pinned generation and hydrates canonical records from SQLite (Section 8.2).
        Delegates to self.retrieve() with bounded candidate expansion and metadata filtering.
        """
        results = self.retrieve(
            query_text=query_text,
            top_k=top_k,
            filter=filter,
            source_type=source_type,
            session=session,
        )
        if return_scores:
            return results
        return [r.chunk for r in results]

    # -----------------------------------------------------------------------
    # Rebuild & Migration Protocols (Section 9.1)
    # -----------------------------------------------------------------------

    def rebuild_index(self) -> MigrationResult:
        """Rebuilds the vector index from canonical SQLite records using the active provider."""
        return self._execute_generation_transition(is_migration=False, target_provider=None)

    def migrate_embeddings(self, target_provider: EmbeddingProvider) -> MigrationResult:
        """Migrates active documents to a new generation using target_provider."""
        return self._execute_generation_transition(is_migration=True, target_provider=target_provider)

    def _execute_generation_transition(
        self,
        is_migration: bool,
        target_provider: Optional[EmbeddingProvider] = None
    ) -> MigrationResult:
        """18-step index rebuild / embedding migration protocol (Section 9.1)."""
        now = datetime.now(timezone.utc).isoformat()
        self._lock_manager.acquire_writer_lock()
        try:
            conn = self.conn_manager.get_connection()
            manifest = conn.execute("SELECT * FROM storage_manifest WHERE manifest_key = 'manifest';").fetchone()
            if manifest["migration_state"] != "IDLE":
                raise InvalidManifestStateError(f"Cannot start generation transition in state {manifest['migration_state']}")

            active_gen = manifest["active_generation"]
            if is_migration:
                if target_provider is None:
                    raise ValueError("target_provider required for embedding migration")
                intent_state = "MIGRATION_STAGING"
                p_id = target_provider.provider_id
                m_name = target_provider.model_name
                dim = target_provider.dimension
                cfg_v = target_provider.config_version
                provider = target_provider
                self.provider_registry.register(target_provider)
            else:
                intent_state = "REBUILD_STAGING"
                p_id = manifest["embedding_provider"]
                m_name = manifest["embedding_model"]
                dim = manifest["embedding_dimension"]
                cfg_v = manifest["embedding_config_version"]
                provider = self._resolve_provider(p_id, m_name, dim, cfg_v)

            stg_gen = active_gen + 1
            stg_col_name = f"chunks_gen_{stg_gen}"

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
                    {
                        "stg_gen": stg_gen,
                        "stg_col": stg_col_name,
                        "p_id": p_id,
                        "m_name": m_name,
                        "dim": dim,
                        "cfg_v": cfg_v,
                        "intent_state": intent_state,
                        "now": now
                    }
                )
                if cursor.rowcount != 1:
                    raise StorageTransactionError("Failed to persist staging intent in storage_manifest")
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
            expected_ids: Set[str] = set()

            for doc_row in active_docs:
                d_id = doc_row["active_document_id"]
                chunks = conn.execute(
                    "SELECT chunk_id, chunk_index, content, source_type, source_identity FROM canonical_chunks WHERE document_id = ?;",
                    (d_id,)
                ).fetchall()

                if chunks:
                    storage_ids = [format_chroma_id(d_id, c["chunk_id"]) for c in chunks]
                    texts = [c["content"] for c in chunks]
                    metas = [
                        {
                            "document_id": d_id,
                            "chunk_id": c["chunk_id"],
                            "chunk_index": c["chunk_index"],
                            "source_identity": c["source_identity"],
                            "source_type": c["source_type"],
                            "generation": stg_gen
                        }
                        for c in chunks
                    ]

                    embeddings = provider.embed_batch(texts)
                    stg_col.add(ids=storage_ids, embeddings=embeddings, metadatas=metas)
                    expected_ids.update(storage_ids)

                # Step 8: Record generation mappings safely
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    conn.execute(
                        "INSERT INTO document_generation_index (document_id, generation, indexed_at) "
                        "VALUES (?, ?, ?) ON CONFLICT(document_id, generation) DO NOTHING;",
                        (d_id, stg_gen, now)
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

            # Step 9: Verify expected-vs-actual document/chunk ID sets
            actual_get = stg_col.get(include=["metadatas"])
            actual_ids = set(actual_get["ids"]) if actual_get and actual_get.get("ids") else set()
            if actual_ids != expected_ids:
                raise MigrationVerificationError(f"ID mismatch: expected {len(expected_ids)}, got {len(actual_ids)}")

            # Step 10: Verify vector dimensions and finite values
            if expected_ids:
                sample = stg_col.get(limit=5, include=["embeddings"])
                if sample and sample.get("embeddings") is not None and len(sample["embeddings"]) > 0:
                    for vec in sample["embeddings"]:
                        if len(vec) != dim or any(not math.isfinite(x) for x in vec):
                            raise MigrationVerificationError("Invalid vector dimension or non-finite values detected")

            # Step 11: Atomically activate new generation in SQLite
            self._lock_manager.acquire_lifecycle_exclusive()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                # Increment global commit seq
                c1 = conn.execute(
                    "UPDATE global_state SET global_commit_seq = global_commit_seq + 1, updated_at = :now WHERE singleton_key = 'state';",
                    {"now": now}
                )
                if c1.rowcount != 1:
                    raise CorruptedGlobalStateError("global_state singleton missing during activation")

                # Register previous generation in retired_generations
                conn.execute(
                    "INSERT INTO retired_generations (generation, chunk_collection, embedding_provider, embedding_model, embedding_dimension, embedding_config_version, retired_at) "
                    "VALUES (:gen, :col, :p_id, :m_name, :dim, :cfg_v, :now);",
                    {
                        "gen": active_gen,
                        "col": f"chunks_gen_{active_gen}",
                        "p_id": manifest["embedding_provider"],
                        "m_name": manifest["embedding_model"],
                        "dim": manifest["embedding_dimension"],
                        "cfg_v": manifest["embedding_config_version"],
                        "now": now
                    }
                )

                # Clear staging fields and update active generation in storage_manifest
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
                    {
                        "stg_gen": stg_gen,
                        "stg_col": stg_col_name,
                        "p_id": p_id,
                        "m_name": m_name,
                        "dim": dim,
                        "cfg_v": cfg_v,
                        "now": now
                    }
                )
                if c2.rowcount != 1:
                    raise StorageTransactionError("Failed to update active storage_manifest on activation")

                conn.execute("COMMIT;")
                self.active_provider = provider
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            finally:
                self._lock_manager.release_lifecycle_exclusive()

            # Step 17: Deferred drop of old generation collections under already-held writer lock
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

    # -----------------------------------------------------------------------
    # Startup Reconciliation Procedure (Section 9.2)
    # -----------------------------------------------------------------------

    def _reconcile_startup_locked(self, conn: sqlite3.Connection) -> ReconciliationReport:
        """Internal startup reconciliation routine executed under both locks."""
        now = datetime.now(timezone.utc).isoformat()
        stale_count = 0
        preserved_count = 0
        pending_rollback_count = 0

        # Phase 1: Recover Stale Reader Pins and Orphaned Snapshots (Fail-Closed)
        stale_sessions: Set[str] = set()
        preserved_sessions: Set[str] = set()

        if self.pins_dir.exists():
            for pin_file in list(self.pins_dir.glob("*.pin")):
                session_id = pin_file.stem
                pin_status = self.probe_pin_liveness(pin_file)

                if pin_status == PinStatus.STALE:
                    stale_sessions.add(session_id)
                    conn.execute("BEGIN IMMEDIATE;")
                    try:
                        conn.execute("DELETE FROM snapshot_protected_documents WHERE session_id = :sid;", {"sid": session_id})
                        conn.execute("DELETE FROM active_snapshots WHERE session_id = :sid;", {"sid": session_id})
                        conn.execute("COMMIT;")
                        stale_count += 1
                    except Exception:
                        conn.execute("ROLLBACK;")
                        raise
                    try:
                        os.unlink(pin_file)
                    except FileNotFoundError:
                        pass
                else:
                    preserved_sessions.add(session_id)
                    preserved_count += 1

        conn.execute("BEGIN IMMEDIATE;")
        try:
            db_snapshots = [r[0] for r in conn.execute("SELECT session_id FROM active_snapshots;").fetchall()]
            for sid in db_snapshots:
                if sid in preserved_sessions:
                    continue
                pin_f = self.pins_dir / f"{sid}.pin"
                if not pin_f.exists():
                    conn.execute("DELETE FROM snapshot_protected_documents WHERE session_id = :sid;", {"sid": sid})
                    conn.execute("DELETE FROM active_snapshots WHERE session_id = :sid;", {"sid": sid})
                    stale_count += 1
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

        # Phase 2: Roll Back Incomplete Rebuild / Migration Staging
        manifest = conn.execute(
            "SELECT migration_state, staging_generation, staging_chunk_collection "
            "FROM storage_manifest WHERE manifest_key = 'manifest';"
        ).fetchone()
        if manifest and manifest["migration_state"] in ("REBUILD_STAGING", "MIGRATION_STAGING"):
            stg_gen = manifest["staging_generation"]
            stg_col = manifest["staging_chunk_collection"]

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

        # Phase 3: Roll Back Pending Ingestion and Replacement Records
        pending_docs = conn.execute(
            "SELECT document_id, source_identity FROM canonical_documents WHERE status = 'pending';"
        ).fetchall()
        active_gen = conn.execute("SELECT active_generation FROM storage_manifest WHERE manifest_key = 'manifest';").fetchone()[0]
        active_col_name = f"chunks_gen_{active_gen}"

        for p_doc in pending_docs:
            d_id = p_doc["document_id"]
            s_id = p_doc["source_identity"]

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

                conn.execute(
                    "DELETE FROM source_catalog "
                    "WHERE source_identity = :s_id AND active_document_id IS NULL "
                    "  AND NOT EXISTS (SELECT 1 FROM canonical_documents WHERE source_identity = :s_id);",
                    {"s_id": s_id}
                )
                conn.execute("COMMIT;")
                pending_rollback_count += 1
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        # Phase 4: Resume Interrupted Cleanup States
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
                    continue

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

        # Run internal cleanup pass without re-acquiring lifecycle lock
        self._run_cleanup_pass_locked(conn)

        # Phase 5: Prune Retired Generations without re-acquiring lifecycle lock
        self._prune_retired_generations_locked(conn)

        return ReconciliationReport(
            success=True,
            timestamp=now,
            stale_sessions_recovered=stale_count,
            preserved_sessions=preserved_count,
            pending_docs_rolled_back=pending_rollback_count
        )

    def reconcile_startup(self) -> ReconciliationReport:
        """Public startup reconciliation entrypoint: acquires writer lock EX, then lifecycle lock EX."""
        self._lock_manager.acquire_writer_lock()
        try:
            self._lock_manager.acquire_lifecycle_exclusive()
            try:
                conn = self.conn_manager.get_connection()
                return self._reconcile_startup_locked(conn)
            finally:
                self._lock_manager.release_lifecycle_exclusive()
        finally:
            self._lock_manager.release_writer_lock()

    def reset_store(self) -> None:
        """Purges all canonical documents, chunks, catalog rows, and vector collections."""
        self._lock_manager.acquire_writer_lock()
        try:
            self._lock_manager.acquire_lifecycle_exclusive()
            try:
                conn = self.conn_manager.get_connection()
                conn.execute("BEGIN IMMEDIATE;")
                conn.execute("DELETE FROM snapshot_protected_documents;")
                conn.execute("DELETE FROM active_snapshots;")
                conn.execute("DELETE FROM cleanup_registry;")
                conn.execute("DELETE FROM document_generation_index;")
                conn.execute("DELETE FROM canonical_chunks;")
                conn.execute("DELETE FROM canonical_documents;")
                conn.execute("DELETE FROM source_catalog;")
                conn.execute("DELETE FROM retired_generations;")
                conn.execute("UPDATE global_state SET global_commit_seq = 0;")
                conn.execute(
                    "UPDATE storage_manifest SET active_generation = 1, active_chunk_collection = 'chunks_gen_1', "
                    "staging_generation = NULL, staging_chunk_collection = NULL, migration_state = 'IDLE';"
                )
                conn.execute("COMMIT;")

                for col in self.chroma_client.list_collections():
                    self.chroma_client.delete_collection(col.name)
            finally:
                self._lock_manager.release_lifecycle_exclusive()
        finally:
            self._lock_manager.release_writer_lock()

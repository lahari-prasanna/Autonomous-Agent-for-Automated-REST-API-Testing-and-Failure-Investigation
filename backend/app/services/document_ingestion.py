"""
Secure, deterministic document ingestion and chunking service (Phase 4B.2).
Converts supported knowledge sources (OpenAPI specifications, local Markdown/text
documentation, and sanitized failure history) into validated KnowledgeDocument
and KnowledgeChunk instances.
Enforces component-by-component filesystem traversal with directory descriptors (O_NOFOLLOW),
strict content sanitization and quarantine, cumulative OpenAPI property budgets,
and deterministic, collision-resistant source identity.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
import posixpath
import re
import stat
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import unicodedata
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.failure import FailureCategory, FailureClassification
from backend.app.schemas.knowledge import (
    ALLOWED_METADATA_KEYS,
    MAX_CHUNK_CONTENT_LEN,
    MAX_ID_LEN,
    MAX_METADATA_VAL_LEN,
    MAX_PATH_LEN,
    MAX_REF_LEN,
    MAX_TITLE_LEN,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSourceType,
    _validate_source_ref,
)
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec
from backend.app.services.spec_parser import OpenAPIParser


# ---------------------------------------------------------------------------
# Constants & Configurable Limits
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES = 1_048_576          # 1 MB file size ceiling
MAX_CHUNK_CONTENT_CHARS = 2048           # Max chunk content length
MIN_CHUNK_CHARS = 50                     # Soft minimum chunk threshold
MAX_CHUNKS_PER_DOCUMENT = 100            # Max chunks per document
MAX_SCHEMA_DEPTH = 4                     # Max OpenAPI schema nesting depth
MAX_OPERATION_PARAMETERS = 30            # Max parameters per operation
MAX_OPERATION_RESPONSES = 10             # Max response status codes per operation
MAX_OPERATION_TOTAL_PROPERTIES = 80      # Cumulative property budget per operation
MAX_EXTRACTED_DESCRIPTION_LEN = 1024     # Max length for individual descriptions

APPROVED_SOURCE_ROOTS = frozenset({"docs", "specs", "mock_api"})

SENSITIVE_PROPERTY_NAMES = frozenset({
    "password",
    "passwd",
    "token",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "private_key",
})

SAFE_ANCHOR_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_SCOPE_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]+$")

# Redaction patterns (Category B)
REDACT_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-_\.]+")
REDACT_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]*")
REDACT_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z ]+KEY-----[\s\S]*?-----END [A-Z ]+KEY-----")
REDACT_ASSIGN_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|api_key|access_token|client_secret)\s*[:=]\s*['\"]?\S+['\"]?"
)
REDACT_USERINFO_PATTERN = re.compile(r"https?://[^:@/\s]+:[^@/\s]+@")

# Quarantine patterns (Category C - Suspicious/Ambiguous)
QUARANTINE_RAW_HEADER_PATTERN = re.compile(r"(?i)(?:set-cookie|cookie|authorization)\s*:\s*\S+")
QUARANTINE_HIGH_ENTROPY_PATTERN = re.compile(r"\b[A-Za-z0-9+/=]{48,}\b")


# ---------------------------------------------------------------------------
# Typed Ingestion Exceptions
# ---------------------------------------------------------------------------

class IngestionError(Exception):
    """Base exception for all document ingestion failures."""
    pass


class SourceNotFoundError(IngestionError):
    """Raised when a requested source file or directory does not exist."""
    pass


class SecurityPathError(IngestionError):
    """Raised when a path traversal, symlink escape, or unapproved root access is detected."""
    pass


class DocumentSizeLimitError(IngestionError):
    """Raised when a file size or document chunk count exceeds configured bounds."""
    pass


class MalformedDocumentError(IngestionError):
    """Raised when source content is corrupted, empty, or unparseable."""
    pass


class UnsupportedSourceTypeError(IngestionError):
    """Raised when an unrecognized source type is supplied."""
    pass


class ProvenanceVerificationError(IngestionError):
    """Raised when failure history endpoint information cannot be verified against the spec."""
    pass


# ---------------------------------------------------------------------------
# Quarantine & Ingestion Outcome Models (Separate from Phase 4B.1 Schemas)
# ---------------------------------------------------------------------------

class IngestionWarning(BaseModel):
    """
    Sanitized diagnostic metadata for quarantined, skipped, or truncated content.
    Guarantees zero exposure of sensitive original text, payloads, or credentials.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    warning_code: str = Field(
        ...,
        max_length=64,
        description="Controlled category (e.g. 'QUARANTINED_SENSITIVE_CONTENT', 'SCHEMA_BUDGET_TRUNCATED')",
    )
    source_ref: Optional[str] = Field(
        default=None,
        max_length=256,
        description="Sanitized provenance reference validated by _validate_source_ref()",
    )
    scope: str = Field(
        ...,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:/-]+$",
        description="Safe alphanumeric semantic scope identifier (e.g. 'section:auth_header')",
    )
    reason: str = Field(
        ...,
        max_length=128,
        description="Controlled explanation from an approved reason allowlist (no user text)",
    )
    rule_violated: str = Field(
        ...,
        max_length=64,
        description="Internal policy rule identifier (e.g. 'SUSPICIOUS_HIGH_ENTROPY_TOKEN')",
    )
    content_length: int = Field(
        ...,
        ge=0,
        description="Byte or character count of the quarantined fragment",
    )
    content_sha256: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 fingerprint of the quarantined content for audit correlation",
    )


class IngestionResult(BaseModel):
    """
    Typed outcome of document ingestion containing validated models and diagnostic warnings.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    documents: List[KnowledgeDocument] = Field(default_factory=list, description="Validated knowledge documents")
    chunks: List[KnowledgeChunk] = Field(default_factory=list, description="Validated searchable chunks")
    warnings: List[IngestionWarning] = Field(default_factory=list, description="Sanitized diagnostic warnings")
    quarantined_count: int = Field(default=0, description="Total count of quarantined sections")
    processed_count: int = Field(default=0, description="Total count of candidate sections processed")


# ---------------------------------------------------------------------------
# Secure Filesystem Traversal & Reading
# ---------------------------------------------------------------------------

def read_secure_file(
    workspace_dir: str,
    root_name: str,
    rel_path: str,
    max_bytes: int = MAX_FILE_SIZE_BYTES,
) -> Tuple[bytes, os.stat_result]:
    """
    Safely reads a local file within an approved workspace root.
    Uses component-by-component directory-FD traversal with O_DIRECTORY | O_NOFOLLOW
    on supported POSIX systems to prevent TOCTOU and intermediate symlink escapes.
    Enforces a bounded read loop to strictly prevent memory overruns.
    """
    if root_name not in APPROVED_SOURCE_ROOTS:
        raise SecurityPathError(
            f"Approved root '{root_name}' is not in allowed roots: {sorted(APPROVED_SOURCE_ROOTS)}."
        )

    # 1. Path-level pre-checks on rel_path
    if "\x00" in rel_path:
        raise SecurityPathError("Null byte detected in file path.")

    # Repeatedly unquote up to 3 times to neutralize double-encoded traversals
    cur_path = rel_path
    for _ in range(3):
        unq = unquote(cur_path)
        if unq == cur_path:
            break
        cur_path = unq

    normalized_rel = cur_path.replace("\\", "/")
    if normalized_rel.startswith("/") or re.match(r"^[a-zA-Z]:", normalized_rel) or normalized_rel.startswith("~"):
        raise SecurityPathError("Absolute or drive paths are not permitted.")

    # Split into components and check for traversal
    raw_parts = [p for p in normalized_rel.split("/") if p and p != "."]
    if any(p == ".." for p in raw_parts):
        raise SecurityPathError("Path traversal ('..') is not permitted.")
    if not raw_parts:
        raise SecurityPathError("Target path cannot be empty.")

    canonical_ws = os.path.realpath(os.path.abspath(workspace_dir))
    if not os.path.isdir(canonical_ws):
        raise SourceNotFoundError(f"Workspace directory does not exist: '{workspace_dir}'.")

    # Verify approved root resolves inside workspace
    candidate_root = os.path.realpath(os.path.abspath(os.path.join(canonical_ws, root_name)))
    try:
        if os.path.commonpath([canonical_ws, candidate_root]) != canonical_ws:
            raise SecurityPathError(f"Approved root '{root_name}' resolves outside workspace.")
    except ValueError:
        raise SecurityPathError("Cross-drive path detected.")

    if not os.path.isdir(candidate_root):
        raise SourceNotFoundError(f"Approved root directory does not exist: '{candidate_root}'.")

    # POSIX Component-by-Component Traversal
    use_posix_dir_fd = (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "open")
        and os.supports_dir_fd is not None
        and os.open in os.supports_dir_fd
    )

    open_fds: List[int] = []
    file_fd: Optional[int] = None
    try:
        if use_posix_dir_fd:
            # 1. Open Workspace directory FD
            ws_fd = os.open(canonical_ws, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
            open_fds.append(ws_fd)
            current_fd = ws_fd

            # 2. Step into root_name components
            root_components = [c for c in os.path.relpath(candidate_root, canonical_ws).replace("\\", "/").split("/") if c and c != "."]
            for comp in root_components:
                next_fd = os.open(
                    comp,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=current_fd,
                )
                open_fds.append(next_fd)
                current_fd = next_fd

            # 3. Step through target directory components
            dir_components = raw_parts[:-1]
            filename = raw_parts[-1]

            for comp in dir_components:
                next_fd = os.open(
                    comp,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=current_fd,
                )
                open_fds.append(next_fd)
                current_fd = next_fd

            # 4. Open final file
            file_fd = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current_fd,
            )
            open_fds.append(file_fd)

            st = os.fstat(file_fd)
            if not stat.S_ISREG(st.st_mode):
                raise SecurityPathError("Target is not a regular file (symlink or special file).")
            if st.st_size > max_bytes:
                raise DocumentSizeLimitError(f"File size ({st.st_size} bytes) exceeds limit of {max_bytes} bytes.")

            # Bounded read loop
            raw_bytes = _read_bounded_fd(file_fd, max_bytes)
            return raw_bytes, st

        else:
            # Non-POSIX Fallback (e.g. Windows)
            canonical_target = os.path.realpath(os.path.abspath(os.path.join(candidate_root, *raw_parts)))
            if os.path.commonpath([candidate_root, canonical_target]) != candidate_root:
                raise SecurityPathError("Target path escapes approved root.")
            if not os.path.exists(canonical_target):
                raise SourceNotFoundError(f"Source file not found: '{rel_path}'.")
            if not os.path.isfile(canonical_target):
                raise SecurityPathError("Target is not a regular file.")

            flags = os.O_RDONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY

            file_fd = os.open(canonical_target, flags)
            open_fds.append(file_fd)

            st = os.fstat(file_fd)
            if not stat.S_ISREG(st.st_mode):
                raise SecurityPathError("Target is not a regular file.")
            if st.st_size > max_bytes:
                raise DocumentSizeLimitError(f"File size ({st.st_size} bytes) exceeds limit of {max_bytes} bytes.")

            raw_bytes = _read_bounded_fd(file_fd, max_bytes)
            return raw_bytes, st

    except FileNotFoundError as e:
        raise SourceNotFoundError(f"Source file or directory not found: {e}") from e
    except (OSError, PermissionError) as e:
        if getattr(e, "errno", None) in (20, 40):  # ENOTDIR, ELOOP
            raise SecurityPathError(f"Symlink or path structure violation during traversal: {e}") from e
        raise
    finally:
        for fd in reversed(open_fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _read_bounded_fd(file_fd: int, max_bytes: int) -> bytes:
    """Reads at most max_bytes + 1 from a file descriptor to detect oversized files."""
    chunks: List[bytes] = []
    total_bytes = 0
    read_limit = max_bytes + 1
    BUFFER_SIZE = 64 * 1024

    while total_bytes < read_limit:
        to_read = min(BUFFER_SIZE, read_limit - total_bytes)
        chunk = os.read(file_fd, to_read)
        if not chunk:
            break
        chunks.append(chunk)
        total_bytes += len(chunk)

    if total_bytes > max_bytes:
        raise DocumentSizeLimitError(f"File content exceeds maximum limit of {max_bytes} bytes.")

    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Identity & Content Sanitization Utilities
# ---------------------------------------------------------------------------

def compute_source_fingerprint(source_type: KnowledgeSourceType, root_name: str, canonical_rel_path: str) -> str:
    """
    Computes a stable, collision-resistant 16-character source identity fingerprint.
    Normalizes path to forward slashes, Unicode NFC, and preserves case.
    """
    nfc_path = unicodedata.normalize("NFC", canonical_rel_path.strip().replace("\\", "/"))
    hash_input = f"{source_type.value}:{root_name}:{nfc_path}".encode("utf-8")
    return hashlib.sha256(hash_input).hexdigest()[:16]


def compute_source_slug(rel_path: str, fingerprint: str) -> str:
    """
    Computes a bounded, readable source slug incorporating the 16-character path fingerprint.
    Format: {readable_prefix}_{fingerprint[:16]} (max ~37 chars).
    """
    normalized = unicodedata.normalize("NFC", rel_path.strip().replace("\\", "/"))
    # Extract readable prefix
    slug_chars = [c if c.isalnum() else "_" for c in normalized]
    readable = re.sub(r"_+", "_", "".join(slug_chars)).strip("_")[:20]
    return f"{readable}_{fingerprint[:16]}"


def sanitize_text(text: str) -> Tuple[str, bool, Optional[str]]:
    """
    Applies the three-tier sanitization policy to arbitrary text:
    - Category B: Redacts recognized secrets (bearer tokens, JWTs, PEM keys, credential assignments).
    - Category C: Detects suspicious/ambiguous content (raw header dumps, high-entropy tokens).
    Returns (sanitized_text, is_quarantined, quarantine_rule_if_any).
    """
    if not text:
        return text, False, None

    # Category C: Check for quarantine conditions
    if QUARANTINE_RAW_HEADER_PATTERN.search(text):
        return text, True, "RAW_HEADER_INJECTION"
    if QUARANTINE_HIGH_ENTROPY_PATTERN.search(text):
        return text, True, "HIGH_ENTROPY_STRING"

    # Category B: Redact recognized patterns
    cleaned = REDACT_BEARER_PATTERN.sub("Bearer [REDACTED_TOKEN]", text)
    cleaned = REDACT_JWT_PATTERN.sub("[REDACTED_JWT]", cleaned)
    cleaned = REDACT_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", cleaned)
    cleaned = REDACT_ASSIGN_PATTERN.sub(r"\1=[REDACTED]", cleaned)
    cleaned = REDACT_USERINFO_PATTERN.sub("https://[REDACTED_USERINFO]@", cleaned)

    return cleaned, False, None


# ---------------------------------------------------------------------------
# Document Ingestion Service
# ---------------------------------------------------------------------------

class DocumentIngestionService:
    """
    Deterministic document ingestion and chunking engine.
    Processes OpenAPI specifications, API documentation, and failure histories.
    """

    def __init__(self, workspace_dir: str):
        self.workspace_dir = os.path.realpath(os.path.abspath(workspace_dir))

    # -----------------------------------------------------------------------
    # OpenAPI Specification Ingestion
    # -----------------------------------------------------------------------

    def ingest_openapi_spec(
        self,
        spec_or_path: Union[ParsedAPISpec, str],
        root_name: str = "mock_api",
        fixed_timestamp: Optional[datetime] = None,
    ) -> IngestionResult:
        """
        Ingests an OpenAPI specification into a KnowledgeDocument with operation-scoped KnowledgeChunks.
        Uses existing OpenAPIParser and enforces cumulative property budgets.
        """
        source_ref: Optional[str] = None
        source_file_mtime: Optional[float] = None
        raw_spec_bytes: bytes = b""

        if isinstance(spec_or_path, str):
            source_ref = posixpath.normpath(spec_or_path.replace("\\", "/"))
            raw_spec_bytes, st = read_secure_file(self.workspace_dir, root_name, source_ref)
            source_file_mtime = st.st_mtime
            # Parse using Phase 1B parser
            try:
                spec_str = raw_spec_bytes.decode("utf-8")
            except UnicodeDecodeError as e:
                raise MalformedDocumentError(f"Spec file '{source_ref}' is not valid UTF-8: {e}") from e
            parser = OpenAPIParser()
            try:
                spec = parser.parse_string(spec_str)
            except Exception as e:
                raise MalformedDocumentError(f"Failed to parse OpenAPI spec '{source_ref}': {e}") from e
        elif isinstance(spec_or_path, ParsedAPISpec):
            spec = spec_or_path
            source_ref = "in_memory/openapi.json"
        else:
            raise TypeError(f"spec_or_path must be a ParsedAPISpec or string path, got {type(spec_or_path).__name__}.")

        canonical_content = f"{spec.title}:{spec.version}:{len(spec.endpoints)}:{spec.description or ''}"
        fingerprint = compute_source_fingerprint(KnowledgeSourceType.OPENAPI_SPEC, root_name, source_ref)
        source_slug = compute_source_slug(source_ref, fingerprint)
        doc_id = f"doc-spec-{source_slug}-{hashlib.sha256(canonical_content.encode('utf-8')).hexdigest()[:12]}"

        # Resolve timestamp
        if fixed_timestamp is not None:
            created_at = fixed_timestamp
        elif source_file_mtime is not None:
            created_at = datetime.fromtimestamp(source_file_mtime, tz=timezone.utc)
        else:
            created_at = datetime.now(timezone.utc)

        doc = KnowledgeDocument(
            document_id=doc_id[:MAX_ID_LEN],
            source_type=KnowledgeSourceType.OPENAPI_SPEC,
            title=spec.title[:MAX_TITLE_LEN],
            source_ref=_validate_source_ref(source_ref),
            created_at=created_at,
            metadata={
                "version": spec.version[:MAX_METADATA_VAL_LEN],
                "tag": "openapi",
                "doc_format": "json",
            },
        )

        chunks: List[KnowledgeChunk] = []
        warnings: List[IngestionWarning] = []
        quarantined_count = 0
        processed_count = 0

        # Sort endpoints deterministically by path then method
        sorted_endpoints = sorted(spec.endpoints, key=lambda ep: (ep.path, ep.method))

        for ep in sorted_endpoints:
            processed_count += 1
            ep_scope = f"{ep.method.lower()}_{re.sub(r'[^A-Za-z0-9_.-]', '_', ep.path).strip('_')}"[:32]
            chunk_content, ep_warnings, is_quarantined = self._extract_endpoint_knowledge(
                ep, source_ref=source_ref, ep_scope=ep_scope
            )
            warnings.extend(ep_warnings)

            if is_quarantined:
                quarantined_count += 1
                continue

            if not chunk_content or len(chunk_content.strip()) < MIN_CHUNK_CHARS:
                continue

            # Sub-chunking if operation exceeds MAX_CHUNK_CONTENT_CHARS
            sub_chunks = self._split_text_bounded(chunk_content, MAX_CHUNK_CONTENT_CHARS)
            for part_idx, part_text in enumerate(sub_chunks):
                if len(chunks) >= MAX_CHUNKS_PER_DOCUMENT:
                    warnings.append(
                        IngestionWarning(
                            warning_code="MAX_CHUNKS_EXCEEDED",
                            source_ref=_validate_source_ref(source_ref),
                            scope=ep_scope,
                            reason="Document exceeded maximum allowed chunks count of 100",
                            rule_violated="MAX_CHUNKS_PER_DOCUMENT",
                            content_length=len(part_text),
                            content_sha256=hashlib.sha256(part_text.encode("utf-8")).hexdigest(),
                        )
                    )
                    break

                part_hash = hashlib.sha256(part_text.encode("utf-8")).hexdigest()[:8]
                chunk_id = f"chk-spec-{fingerprint}-{ep_scope}-{part_idx:02d}-{part_hash}"

                chunk_meta: Dict[str, Union[str, int, float, bool, None]] = {
                    "tag": ep.tags[0][:MAX_METADATA_VAL_LEN] if ep.tags else "api",
                    "category": "openapi_spec",
                    "doc_format": "json",
                    "is_deprecated": ep.deprecated,
                }
                if ep.responses:
                    # Record first numeric status code if present
                    first_numeric = next((int(sc) for sc in ep.responses if sc.isdigit() and 100 <= int(sc) <= 599), None)
                    if first_numeric is not None:
                        chunk_meta["status_code"] = first_numeric

                chunk = KnowledgeChunk(
                    chunk_id=chunk_id[:MAX_ID_LEN],
                    document_id=doc.document_id,
                    content=part_text,
                    chunk_index=len(chunks),
                    source_type=KnowledgeSourceType.OPENAPI_SPEC,
                    http_method=ep.method,
                    path_template=ep.path,
                    operation_id=ep.operation_id,
                    source_ref=_validate_source_ref(f"{source_ref}#{ep.operation_id or ep_scope}"),
                    metadata=chunk_meta,
                )
                chunks.append(chunk)

        return IngestionResult(
            documents=[doc],
            chunks=chunks,
            warnings=warnings,
            quarantined_count=quarantined_count,
            processed_count=processed_count,
        )

    def _extract_endpoint_knowledge(
        self,
        ep: EndpointDefinition,
        source_ref: str,
        ep_scope: str,
    ) -> Tuple[str, List[IngestionWarning], bool]:
        """
        Extracts operation content using field-level allowlist and cumulative property budget.
        Returns (content_text, warnings, is_quarantined).
        """
        warnings: List[IngestionWarning] = []
        lines: List[str] = []
        cumulative_props = 0

        # Operation Header
        lines.append(f"### {ep.method} {ep.path}")
        if ep.operation_id:
            lines.append(f"Operation ID: {ep.operation_id}")

        # Summary & Description
        if ep.summary:
            clean_sum, q, rule = sanitize_text(ep.summary[:MAX_EXTRACTED_DESCRIPTION_LEN])
            if q:
                warnings.append(
                    IngestionWarning(
                        warning_code="QUARANTINED_SENSITIVE_CONTENT",
                        source_ref=_validate_source_ref(source_ref),
                        scope=ep_scope,
                        reason="Operation summary triggered suspicious content quarantine",
                        rule_violated=rule or "UNKNOWN",
                        content_length=len(ep.summary),
                        content_sha256=hashlib.sha256(ep.summary.encode("utf-8")).hexdigest(),
                    )
                )
                return "", warnings, True
            lines.append(f"Summary: {clean_sum}")

        if ep.description:
            clean_desc, q, rule = sanitize_text(ep.description[:MAX_EXTRACTED_DESCRIPTION_LEN])
            if q:
                warnings.append(
                    IngestionWarning(
                        warning_code="QUARANTINED_SENSITIVE_CONTENT",
                        source_ref=_validate_source_ref(source_ref),
                        scope=ep_scope,
                        reason="Operation description triggered suspicious content quarantine",
                        rule_violated=rule or "UNKNOWN",
                        content_length=len(ep.description),
                        content_sha256=hashlib.sha256(ep.description.encode("utf-8")).hexdigest(),
                    )
                )
                return "", warnings, True
            lines.append(f"Description: {clean_desc}")

        # Parameters (bounded by MAX_OPERATION_PARAMETERS)
        if ep.parameters:
            lines.append("Parameters:")
            for p in ep.parameters[:MAX_OPERATION_PARAMETERS]:
                if cumulative_props >= MAX_OPERATION_TOTAL_PROPERTIES:
                    lines.append("[TRUNCATED_OPERATION_PROPERTY_BUDGET_EXCEEDED]")
                    warnings.append(
                        IngestionWarning(
                            warning_code="SCHEMA_BUDGET_TRUNCATED",
                            source_ref=_validate_source_ref(source_ref),
                            scope=ep_scope,
                            reason="Exceeded maximum cumulative operation property budget",
                            rule_violated="MAX_OPERATION_TOTAL_PROPERTIES",
                            content_length=0,
                            content_sha256=hashlib.sha256(b"").hexdigest(),
                        )
                    )
                    break

                cumulative_props += 1
                req_str = " (required)" if p.required else " (optional)"
                p_type = p.schema_.get("type", "any") if isinstance(p.schema_, dict) else "any"
                lines.append(f"  - {p.name} in {p.in_}{req_str}: type={p_type}")

        # Request Body Schema Properties (bounded)
        if ep.request_body and ep.request_body.content:
            lines.append("Request Body:")
            for media_type, mt_obj in ep.request_body.content.items():
                lines.append(f"  Content-Type: {media_type}")
                schema = mt_obj.get("schema", {}) if isinstance(mt_obj, dict) else {}
                props_extracted, cumulative_props, w_list = self._extract_schema_properties(
                    schema, depth=1, cumulative_props=cumulative_props, source_ref=source_ref, ep_scope=ep_scope
                )
                warnings.extend(w_list)
                for pr in props_extracted:
                    lines.append(f"    - {pr}")

        # Responses (bounded by MAX_OPERATION_RESPONSES)
        if ep.responses:
            lines.append("Responses:")
            resp_items = sorted(ep.responses.items())[:MAX_OPERATION_RESPONSES]
            for sc, r_def in resp_items:
                clean_r_desc = r_def.description[:MAX_EXTRACTED_DESCRIPTION_LEN]
                lines.append(f"  Status {sc}: {clean_r_desc}")
                for media_type, mt_obj in r_def.content.items():
                    schema = mt_obj.get("schema", {}) if isinstance(mt_obj, dict) else {}
                    props_extracted, cumulative_props, w_list = self._extract_schema_properties(
                        schema, depth=1, cumulative_props=cumulative_props, source_ref=source_ref, ep_scope=ep_scope
                    )
                    warnings.extend(w_list)
                    for pr in props_extracted:
                        lines.append(f"    - [{sc}] {pr}")

        final_content = "\n".join(lines)
        return final_content, warnings, False

    def _extract_schema_properties(
        self,
        schema: Any,
        depth: int,
        cumulative_props: int,
        source_ref: str,
        ep_scope: str,
    ) -> Tuple[List[str], int, List[IngestionWarning]]:
        """Extracts schema property summaries within depth and cumulative property budgets."""
        extracted: List[str] = []
        warnings: List[IngestionWarning] = []

        if not isinstance(schema, dict) or depth > MAX_SCHEMA_DEPTH:
            if depth > MAX_SCHEMA_DEPTH:
                extracted.append("[TRUNCATED_SCHEMA_DEPTH_EXCEEDED]")
            return extracted, cumulative_props, warnings

        # Local circular reference protection
        if "$ref" in schema:
            ref_val = str(schema["$ref"])
            extracted.append(f"$ref: {ref_val}")
            return extracted, cumulative_props, warnings

        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for prop_name, prop_def in properties.items():
                if cumulative_props >= MAX_OPERATION_TOTAL_PROPERTIES:
                    extracted.append("[TRUNCATED_OPERATION_PROPERTY_BUDGET_EXCEEDED]")
                    warnings.append(
                        IngestionWarning(
                            warning_code="SCHEMA_BUDGET_TRUNCATED",
                            source_ref=_validate_source_ref(source_ref),
                            scope=ep_scope,
                            reason="Exceeded maximum cumulative operation property budget",
                            rule_violated="MAX_OPERATION_TOTAL_PROPERTIES",
                            content_length=0,
                            content_sha256=hashlib.sha256(b"").hexdigest(),
                        )
                    )
                    break

                cumulative_props += 1
                prop_name_str = str(prop_name)
                # Sensitive property filtering
                if prop_name_str.lower() in SENSITIVE_PROPERTY_NAMES:
                    extracted.append(f"{prop_name_str}: [SENSITIVE_FIELD_REDACTED]")
                    continue

                if isinstance(prop_def, dict):
                    prop_type = prop_def.get("type", "object")
                    extracted.append(f"{prop_name_str} ({prop_type})")
                    if prop_type == "object" and "properties" in prop_def:
                        if depth < MAX_SCHEMA_DEPTH:
                            sub_props, cumulative_props, sub_w = self._extract_schema_properties(
                                prop_def, depth + 1, cumulative_props, source_ref, ep_scope
                            )
                            warnings.extend(sub_w)
                            for sp in sub_props:
                                extracted.append(f"  .{sp}")
                        else:
                            extracted.append("  .[TRUNCATED_SCHEMA_DEPTH_EXCEEDED]")
                else:
                    extracted.append(f"{prop_name_str}")

        return extracted, cumulative_props, warnings

    # -----------------------------------------------------------------------
    # Local Markdown / Plain Text Documentation Ingestion
    # -----------------------------------------------------------------------

    def ingest_documentation_file(
        self,
        rel_path: str,
        root_name: str = "docs",
        fixed_timestamp: Optional[datetime] = None,
    ) -> IngestionResult:
        """
        Ingests a local Markdown (.md) or text (.txt) file from an approved root.
        Applies heading-based chunking with safe anchor preservation.
        """
        source_ref = posixpath.normpath(rel_path.replace("\\", "/"))
        raw_bytes, st = read_secure_file(self.workspace_dir, root_name, source_ref)

        try:
            content_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise MalformedDocumentError(f"File '{source_ref}' is not valid UTF-8: {e}") from e

        content_text = unicodedata.normalize("NFC", content_text).replace("\r\n", "\n")
        if not content_text.strip():
            raise MalformedDocumentError(f"Document '{source_ref}' is empty or whitespace-only.")

        fingerprint = compute_source_fingerprint(KnowledgeSourceType.API_DOCUMENTATION, root_name, source_ref)
        source_slug = compute_source_slug(source_ref, fingerprint)
        doc_id = f"doc-doc-{source_slug}-{hashlib.sha256(content_text.encode('utf-8')).hexdigest()[:12]}"

        # Title from first # Heading or file basename
        title_match = re.search(r"^#\s+(.+)$", content_text, re.MULTILINE)
        doc_title = title_match.group(1).strip() if title_match else posixpath.basename(source_ref)

        # Resolve timestamp
        if fixed_timestamp is not None:
            created_at = fixed_timestamp
        else:
            created_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)

        doc = KnowledgeDocument(
            document_id=doc_id[:MAX_ID_LEN],
            source_type=KnowledgeSourceType.API_DOCUMENTATION,
            title=doc_title[:MAX_TITLE_LEN],
            source_ref=_validate_source_ref(source_ref),
            created_at=created_at,
            metadata={
                "category": "developer_guide",
                "doc_format": "markdown" if source_ref.endswith(".md") else "text",
            },
        )

        chunks: List[KnowledgeChunk] = []
        warnings: List[IngestionWarning] = []
        quarantined_count = 0
        processed_count = 0

        # Split content into heading-based sections
        sections = self._split_markdown_sections(content_text)
        occurrence_counter: Dict[str, int] = {}

        for raw_heading, section_body in sections:
            if not section_body.strip():
                continue
            processed_count += 1
            # Base anchor slug
            base_anchor = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_heading.lower()).strip("_") or "section"
            base_anchor = base_anchor[:32]

            occurrence_counter[base_anchor] = occurrence_counter.get(base_anchor, 0) + 1
            anchor_slug = f"{base_anchor}-{occurrence_counter[base_anchor]}" if occurrence_counter[base_anchor] > 1 else base_anchor

            section_full_text = f"## {raw_heading}\n\n{section_body}".strip() if raw_heading else section_body.strip()
            if not section_full_text or not section_full_text.strip():
                continue

            # Sanitization & Quarantine check
            clean_text, is_q, q_rule = sanitize_text(section_full_text)
            if is_q:
                quarantined_count += 1
                warnings.append(
                    IngestionWarning(
                        warning_code="QUARANTINED_SENSITIVE_CONTENT",
                        source_ref=_validate_source_ref(f"{source_ref}#{anchor_slug}"),
                        scope=anchor_slug[:64],
                        reason="Section triggered suspicious credential or raw header quarantine",
                        rule_violated=q_rule or "UNKNOWN",
                        content_length=len(section_full_text),
                        content_sha256=hashlib.sha256(section_full_text.encode("utf-8")).hexdigest(),
                    )
                )
                continue

            # Split bounded text if oversized
            sub_chunks = self._split_text_bounded(clean_text, MAX_CHUNK_CONTENT_CHARS)
            for part_idx, part_text in enumerate(sub_chunks):
                if len(chunks) >= MAX_CHUNKS_PER_DOCUMENT:
                    warnings.append(
                        IngestionWarning(
                            warning_code="MAX_CHUNKS_EXCEEDED",
                            source_ref=_validate_source_ref(source_ref),
                            scope=anchor_slug[:64],
                            reason="Document exceeded maximum chunk limit of 100",
                            rule_violated="MAX_CHUNKS_PER_DOCUMENT",
                            content_length=len(part_text),
                            content_sha256=hashlib.sha256(part_text.encode("utf-8")).hexdigest(),
                        )
                    )
                    break

                part_hash = hashlib.sha256(part_text.encode("utf-8")).hexdigest()[:8]
                chunk_id = f"chk-doc-{fingerprint}-{anchor_slug}-{part_idx:02d}-{part_hash}"

                chunk = KnowledgeChunk(
                    chunk_id=chunk_id[:MAX_ID_LEN],
                    document_id=doc.document_id,
                    content=part_text,
                    chunk_index=len(chunks),
                    source_type=KnowledgeSourceType.API_DOCUMENTATION,
                    source_ref=_validate_source_ref(f"{source_ref}#{anchor_slug}"),
                    metadata={
                        "category": "developer_guide",
                        "section": anchor_slug[:MAX_METADATA_VAL_LEN],
                        "doc_format": "markdown" if source_ref.endswith(".md") else "text",
                    },
                )
                chunks.append(chunk)

        return IngestionResult(
            documents=[doc],
            chunks=chunks,
            warnings=warnings,
            quarantined_count=quarantined_count,
            processed_count=processed_count,
        )

    def _split_markdown_sections(self, text: str) -> List[Tuple[str, str]]:
        """Splits markdown text into (heading_title, section_body) pairs."""
        heading_regex = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
        matches = list(heading_regex.finditer(text))

        if not matches:
            return [("Document Content", text)]

        sections: List[Tuple[str, str]] = []
        # Pre-heading content if any
        if matches[0].start() > 0:
            pre_body = text[: matches[0].start()].strip()
            if pre_body:
                sections.append(("Overview", pre_body))

        for i, match in enumerate(matches):
            heading_title = match.group(2).strip()
            start_pos = match.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start_pos:end_pos].strip()
            sections.append((heading_title, body))

        return sections

    # -----------------------------------------------------------------------
    # Failure History Ingestion
    # -----------------------------------------------------------------------

    def ingest_failure_classification(
        self,
        classification: Union[FailureClassification, Dict[str, Any]],
        spec: ParsedAPISpec,
        run_id: str = "run_default",
        fixed_timestamp: Optional[datetime] = None,
    ) -> IngestionResult:
        """
        Ingests a sanitized FailureClassification outcome or compact history entry into a KnowledgeDocument.
        Validates endpoint method/path provenance against spec.endpoints.
        Rejects arbitrary dicts or unverified paths.
        """
        if not isinstance(spec, ParsedAPISpec):
            raise TypeError(f"spec must be a ParsedAPISpec, got {type(spec).__name__}.")

        # 1. Normalize input to FailureClassification model
        if isinstance(classification, FailureClassification):
            fc = classification
        elif isinstance(classification, dict):
            # Verify if it is a validated compact history dict or classification dump
            if "primary_category" in classification and "evidence" in classification:
                fc = FailureClassification.model_validate(classification)
            elif "status" in classification and "method" in classification:
                # Compact history record from Phase 4A
                fc = self._compact_history_to_classification(classification)
            else:
                raise MalformedDocumentError(
                    "Arbitrary dictionaries are not accepted. Input must be a valid FailureClassification or compact history record."
                )
        else:
            raise TypeError(
                f"classification must be a FailureClassification or sanitized history dict, got {type(classification).__name__}."
            )

        # 2. Validate endpoint provenance against spec.endpoints
        cand_path = fc.evidence.path_template
        cand_method = fc.evidence.http_method

        if cand_path is not None:
            # Must match an actual endpoint in spec
            matched = any(
                ep.path == cand_path and (cand_method is None or ep.method.upper() == cand_method.upper())
                for ep in spec.endpoints
            )
            if not matched:
                raise ProvenanceVerificationError(
                    f"Endpoint provenance validation failed: path '{cand_path}' (method '{cand_method}') not found in parsed specification."
                )

        clean_run_id = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id).strip("_")[:32] or "run_0"
        canonical_content = f"{clean_run_id}:{fc.primary_category.value if fc.primary_category else 'none'}:{cand_path}:{cand_method}:{fc.summary}"
        fingerprint = hashlib.sha256(f"failure_history:run:{clean_run_id}".encode("utf-8")).hexdigest()[:16]
        doc_id = f"doc-hist-run_{clean_run_id}_{fingerprint[:16]}-{hashlib.sha256(canonical_content.encode('utf-8')).hexdigest()[:12]}"

        created_at = fixed_timestamp or datetime.now(timezone.utc)

        doc = KnowledgeDocument(
            document_id=doc_id[:MAX_ID_LEN],
            source_type=KnowledgeSourceType.FAILURE_HISTORY,
            title=f"Failure: {fc.primary_category.value if fc.primary_category else 'Unknown'}"[:MAX_TITLE_LEN],
            source_ref=_validate_source_ref(f"run://{clean_run_id}#failure_investigation"),
            http_method=cand_method,
            path_template=cand_path,
            created_at=created_at,
            metadata={
                "category": fc.primary_category.value if fc.primary_category else "unknown_failure",
                "verified": True,
                "status_code": fc.evidence.status_code if fc.evidence.status_code and 100 <= fc.evidence.status_code <= 599 else None,
            },
        )

        # Assemble chunk content
        chunk_lines = [
            f"### Failure Investigation Report ({clean_run_id})",
            f"Primary Category: {fc.primary_category.value if fc.primary_category else 'Unknown'}",
            f"Severity: {fc.severity.value}",
            f"Summary: {fc.summary}",
        ]
        if cand_method and cand_path:
            chunk_lines.append(f"Target: {cand_method} {cand_path}")
        if fc.evidence.status_code is not None:
            chunk_lines.append(f"Observed Status: {fc.evidence.status_code}")
        if fc.evidence.failed_assertion_types:
            chunk_lines.append(f"Failed Assertions: {', '.join(fc.evidence.failed_assertion_types)}")
        if fc.evidence.schema_keywords:
            chunk_lines.append(f"Violated Keywords: {', '.join(fc.evidence.schema_keywords)}")
        if fc.evidence.contract_violation_summaries:
            chunk_lines.append("Contract Violations:")
            for cv in fc.evidence.contract_violation_summaries[:5]:
                chunk_lines.append(f"  - {cv}")
        chunk_lines.append(f"Retry Policy: {fc.retry_guidance.policy.value} (Idempotent: {fc.retry_guidance.is_idempotent_method})")
        chunk_lines.append(f"Retry Reason: {fc.retry_guidance.reason}")

        chunk_text = "\n".join(chunk_lines)
        chunk_text, is_q, q_rule = sanitize_text(chunk_text)

        warnings: List[IngestionWarning] = []
        chunks: List[KnowledgeChunk] = []

        if is_q:
            warnings.append(
                IngestionWarning(
                    warning_code="QUARANTINED_SENSITIVE_CONTENT",
                    source_ref=_validate_source_ref(f"run://{clean_run_id}#failure"),
                    scope="failure_report",
                    reason="Failure report contained suspicious content triggering quarantine",
                    rule_violated=q_rule or "UNKNOWN",
                    content_length=len(chunk_text),
                    content_sha256=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                )
            )
            return IngestionResult(
                documents=[doc],
                chunks=[],
                warnings=warnings,
                quarantined_count=1,
                processed_count=1,
            )

        chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:8]
        chunk_id = f"chk-hist-{fingerprint}-failure_report-00-{chunk_hash}"

        chunk = KnowledgeChunk(
            chunk_id=chunk_id[:MAX_ID_LEN],
            document_id=doc.document_id,
            content=chunk_text,
            chunk_index=0,
            source_type=KnowledgeSourceType.FAILURE_HISTORY,
            http_method=cand_method,
            path_template=cand_path,
            failure_category=fc.primary_category,
            source_ref=_validate_source_ref(f"run://{clean_run_id}#failure"),
            metadata={
                "category": fc.primary_category.value if fc.primary_category else "unknown_failure",
                "verified": True,
                "status_code": fc.evidence.status_code if fc.evidence.status_code and 100 <= fc.evidence.status_code <= 599 else None,
            },
        )
        chunks.append(chunk)

        return IngestionResult(
            documents=[doc],
            chunks=chunks,
            warnings=warnings,
            quarantined_count=0,
            processed_count=1,
        )

    def _compact_history_to_classification(self, history: Dict[str, Any]) -> FailureClassification:
        """Adapts a sanitized Phase 4A compact history record into a FailureClassification."""
        from backend.app.schemas.failure import RetryGuidance, RetryPolicy, SanitizedFailureEvidence

        status_code = history.get("status_code")
        error_type = history.get("error_type")
        method = history.get("method")
        path_template = history.get("path_template")

        # Map to primary category
        if status_code is not None and 500 <= status_code <= 599:
            cat = FailureCategory.SERVER_ERROR
        elif status_code == 404:
            cat = FailureCategory.RESOURCE_NOT_FOUND
        elif status_code == 401:
            cat = FailureCategory.AUTHENTICATION_FAILURE
        elif status_code == 403:
            cat = FailureCategory.AUTHORIZATION_FAILURE
        elif status_code in (400, 422):
            cat = FailureCategory.REQUEST_VALIDATION_FAILURE
        elif error_type == "TimeoutException":
            cat = FailureCategory.REQUEST_TIMEOUT
        elif error_type == "SSRFSecurityError":
            cat = FailureCategory.SSRF_SECURITY_BLOCK
        else:
            cat = FailureCategory.UNKNOWN_FAILURE

        return FailureClassification(
            has_failure=True,
            primary_category=cat,
            summary=f"Historical test failure: status {status_code}, error {error_type}",
            retry_guidance=RetryGuidance(
                policy=RetryPolicy.DO_NOT_RETRY,
                is_idempotent_method=method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE"),
                reason="Derived from historical execution record.",
            ),
            evidence=SanitizedFailureEvidence(
                http_method=method,
                path_template=path_template,
                status_code=status_code if isinstance(status_code, int) else None,
                error_type=error_type,
            ),
        )

    # -----------------------------------------------------------------------
    # Text Splitting Engine
    # -----------------------------------------------------------------------

    def _split_text_bounded(self, text: str, max_chars: int = MAX_CHUNK_CONTENT_CHARS) -> List[str]:
        """
        Splits text into chunks <= max_chars using a hierarchical pipeline:
        1. Paragraphs (\\n\\n)
        2. Sentences (. / ? / !)
        3. Words (\\s+)
        4. Hard character boundary
        Merges small trailing slices (< MIN_CHUNK_CHARS) into preceding chunk when possible.
        """
        if len(text) <= max_chars:
            return [text]

        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        chunks: List[str] = []
        current_chunk: List[str] = []
        current_len = 0

        for p in paragraphs:
            if len(p) <= max_chars:
                p_len = len(p) + (2 if current_chunk else 0)
                if current_len + p_len <= max_chars:
                    current_chunk.append(p)
                    current_len += p_len
                else:
                    if current_chunk:
                        chunks.append("\n\n".join(current_chunk))
                    current_chunk = [p]
                    current_len = len(p)
            else:
                # Oversized paragraph -> Sentence split
                prefix = "\n\n".join(current_chunk) if current_chunk else ""
                current_chunk = []
                current_len = 0

                sentence_chunks = self._split_paragraph_by_sentences(p, max_chars, prefix=prefix)
                chunks.extend(sentence_chunks)

        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        # Merge trailing tiny chunk (< MIN_CHUNK_CHARS) if fits
        if len(chunks) > 1 and len(chunks[-1]) < MIN_CHUNK_CHARS:
            if len(chunks[-2]) + 2 + len(chunks[-1]) <= max_chars:
                merged = f"{chunks[-2]}\n\n{chunks[-1]}"
                chunks = chunks[:-2] + [merged]

        return [c for c in chunks if c.strip()]

    def _split_paragraph_by_sentences(self, paragraph: str, max_chars: int, prefix: str = "") -> List[str]:
        """Splits an oversized paragraph into sentences, words, or character slices <= max_chars."""
        sentence_regex = re.compile(r"(?<=[.?!])\s+")
        sentences = [s.strip() for s in sentence_regex.split(paragraph) if s.strip()]

        chunks: List[str] = []
        if prefix and sentences:
            if len(prefix) + 2 + len(sentences[0]) <= max_chars:
                sentences[0] = f"{prefix}\n\n{sentences[0]}"
            else:
                chunks.append(prefix)
        elif prefix:
            chunks.append(prefix)

        chunks: List[str] = []
        current_chunk: List[str] = []
        current_len = 0

        for s in sentences:
            if len(s) <= max_chars:
                s_len = len(s) + (1 if current_chunk else 0)
                if current_len + s_len <= max_chars:
                    current_chunk.append(s)
                    current_len += s_len
                else:
                    if current_chunk:
                        chunks.append(" ".join(current_chunk))
                    current_chunk = [s]
                    current_len = len(s)
            else:
                # Oversized sentence -> Word split
                if current_chunk:
                    chunks.append(" ".join(current_chunk))
                    current_chunk = []
                    current_len = 0

                word_chunks = self._split_sentence_by_words(s, max_chars)
                chunks.extend(word_chunks)

        if current_chunk:
            chunks.append(" ".join(current_chunk))

        return chunks

    def _split_sentence_by_words(self, sentence: str, max_chars: int) -> List[str]:
        """Splits an oversized sentence by words or hard character slices."""
        words = sentence.split()
        chunks: List[str] = []
        current_chunk: List[str] = []
        current_len = 0

        for w in words:
            if len(w) <= max_chars:
                w_len = len(w) + (1 if current_chunk else 0)
                if current_len + w_len <= max_chars:
                    current_chunk.append(w)
                    current_len += w_len
                else:
                    if current_chunk:
                        chunks.append(" ".join(current_chunk))
                    current_chunk = [w]
                    current_len = len(w)
            else:
                # Single unbroken word > max_chars -> Hard slice
                if current_chunk:
                    chunks.append(" ".join(current_chunk))
                    current_chunk = []
                    current_len = 0

                for i in range(0, len(w), max_chars):
                    chunks.append(w[i : i + max_chars])

        if current_chunk:
            chunks.append(" ".join(current_chunk))

        return chunks

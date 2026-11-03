"""
Structured schemas for RAG knowledge documents and searchable chunks (Phase 4B.1).
Defines strongly-typed, validated Pydantic v2 models representing knowledge sources,
including OpenAPI specifications, documentation, and sanitized historical failure classifications.
Enforces strict boundaries against credentials, raw payloads, unbounded metadata, and vector-specific fields.
"""

from datetime import datetime, timezone
from enum import Enum
import math
import re
from typing import Any, Dict, Optional, Union
from urllib.parse import unquote, urlsplit, urlunsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.schemas.failure import FailureCategory

# ---------------------------------------------------------------------------
# Constants & Allowlist Constraints
# ---------------------------------------------------------------------------

MAX_ID_LEN = 128
MAX_TITLE_LEN = 256
MAX_REF_LEN = 512
MAX_PATH_LEN = 256
MAX_CHUNK_CONTENT_LEN = 16384
MAX_METADATA_ENTRIES = 10
MAX_METADATA_KEY_LEN = 64
MAX_METADATA_VAL_LEN = 256

VALID_HTTP_METHODS = frozenset({
    "GET",
    "POST",
    "PUT",
    "DELETE",
    "PATCH",
    "OPTIONS",
    "HEAD",
    "TRACE",
})

ALLOWED_METADATA_KEYS = frozenset({
    "tag",
    "category",
    "version",
    "status_code",
    "section",
    "doc_format",
    "is_deprecated",
    "content_type",
    "scenario",
    "verified",
})

SAFE_FRAGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")

SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"^bearer\s+[A-Za-z0-9\-_\.]+", re.IGNORECASE),
    re.compile(r"^eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]+KEY-----"),              # Private key
    re.compile(r"(?:password|passwd|secret|api_key|token)\s*[:=]\s*\S+", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class KnowledgeSourceType(str, Enum):
    """
    Taxonomy of supported knowledge source origins for RAG retrieval.
    """
    OPENAPI_SPEC = "openapi_spec"
    API_DOCUMENTATION = "api_documentation"
    FAILURE_HISTORY = "failure_history"


# ---------------------------------------------------------------------------
# Shared Sanitization and Validation Helpers
# ---------------------------------------------------------------------------

def _validate_identifier(val: Any, field_name: str, max_len: int = MAX_ID_LEN) -> str:
    """Validates that an identifier is a non-empty string within max_len."""
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a string, got {type(val).__name__}.")
    cleaned = val.strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be empty or whitespace-only.")
    if len(cleaned) > max_len:
        raise ValueError(f"{field_name} exceeds maximum length of {max_len} characters (got {len(cleaned)}).")
    return cleaned


def _validate_title(val: Any) -> str:
    """Validates that a title is a non-empty string within MAX_TITLE_LEN."""
    if not isinstance(val, str):
        raise ValueError(f"Title must be a string, got {type(val).__name__}.")
    cleaned = val.strip()
    if not cleaned:
        raise ValueError("Title cannot be empty or whitespace-only.")
    if len(cleaned) > MAX_TITLE_LEN:
        raise ValueError(f"Title exceeds maximum length of {MAX_TITLE_LEN} characters (got {len(cleaned)}).")
    return cleaned


def _validate_source_ref(val: Optional[Any]) -> Optional[str]:
    """
    Validates and normalizes source reference strings.

    Guarantees:
    - HTTP/HTTPS:
      * Validates presence of valid scheme and hostname.
      * Rejects embedded credentials in userinfo (both literal and percent-encoded).
      * Strips query parameters entirely (preventing credential / token leakage).
      * Preserves safe section anchor fragments matching ^[A-Za-z0-9_.-]+$, removing unsafe ones.
    - Local / Relative References:
      * Rejects absolute filesystem paths (/..., \\..., C:\\...).
      * Rejects home-directory references (~/...).
      * Rejects path traversal segments (..).
      * Preserves safe relative documentation paths and anchors.
    - Maximum length: bounded to MAX_REF_LEN (512).
    """
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"source_ref must be a string, got {type(val).__name__}.")
    cleaned = val.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_REF_LEN:
        raise ValueError(f"source_ref exceeds maximum length of {MAX_REF_LEN} characters (got {len(cleaned)}).")

    # Local file path traversal and absolute path protection
    if cleaned.startswith("/") or cleaned.startswith("\\") or re.match(r"^[a-zA-Z]:[\\/]", cleaned):
        raise ValueError("Absolute local filesystem paths are not permitted in source_ref.")
    if cleaned.startswith("~") or "/~" in cleaned or "\\~" in cleaned:
        raise ValueError("Home-directory references ('~') are not permitted in source_ref.")
    if (
        cleaned == ".."
        or cleaned.startswith("../")
        or cleaned.startswith("..\\")
        or "/../" in cleaned
        or "\\..\\" in cleaned
        or cleaned.endswith("/..")
        or cleaned.endswith("\\..")
    ):
        raise ValueError("Path traversal segments ('..') are not permitted in source_ref.")

    # Parse reference using urlsplit
    parts = urlsplit(cleaned)
    if parts.scheme in ("http", "https"):
        if not parts.hostname:
            raise ValueError("HTTP/HTTPS URL must contain a valid hostname.")
        # Reject credentials in netloc
        netloc = parts.netloc
        unquoted_netloc = unquote(netloc)
        if "@" in netloc or "@" in unquoted_netloc or parts.username or parts.password:
            raise ValueError("source_ref cannot contain embedded credentials in URL.")
        # Strip query parameters entirely; preserve safe section anchor fragment
        clean_frag = parts.fragment if SAFE_FRAGMENT_PATTERN.match(parts.fragment) else ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", clean_frag))
    elif parts.scheme:
        # Custom internal URI scheme (e.g. run://)
        netloc = parts.netloc
        unquoted_netloc = unquote(netloc)
        if "@" in netloc or "@" in unquoted_netloc or parts.username or parts.password:
            raise ValueError("source_ref cannot contain embedded credentials in URI.")
        clean_frag = parts.fragment if SAFE_FRAGMENT_PATTERN.match(parts.fragment) else ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", clean_frag))
    else:
        # Relative reference without URI scheme (e.g. docs/guide.md#overview)
        ref_no_query, _, _ = cleaned.partition("?")
        path_part, has_frag, frag_part = ref_no_query.partition("#")
        if has_frag and SAFE_FRAGMENT_PATTERN.match(frag_part):
            return f"{path_part}#{frag_part}"
        return path_part


def _validate_http_method(method: Optional[Any]) -> Optional[str]:
    """Normalizes HTTP method to uppercase and validates against RFC standard methods."""
    if method is None:
        return None
    if not isinstance(method, str):
        raise ValueError(f"HTTP method must be a string, got {type(method).__name__}.")
    cleaned = method.strip().upper()
    if not cleaned:
        return None
    if cleaned not in VALID_HTTP_METHODS:
        raise ValueError(f"Invalid HTTP method: '{method}'. Must be one of {sorted(VALID_HTTP_METHODS)}.")
    return cleaned


def _validate_path_template(path: Optional[Any]) -> Optional[str]:
    """Validates OpenAPI route template, enforcing leading '/' and rejecting query/fragments/schemes."""
    if path is None:
        return None
    if not isinstance(path, str):
        raise ValueError(f"Path template must be a string, got {type(path).__name__}.")
    cleaned = path.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_PATH_LEN:
        raise ValueError(f"Path template exceeds maximum length of {MAX_PATH_LEN} characters.")
    if "://" in cleaned:
        raise ValueError(f"Path template cannot contain URI schemes ('://'): '{cleaned}'.")
    if "?" in cleaned:
        raise ValueError(f"Path template cannot contain query parameters ('?'): '{cleaned}'.")
    if "#" in cleaned:
        raise ValueError(f"Path template cannot contain URI fragments ('#'): '{cleaned}'.")
    if not cleaned.startswith("/"):
        raise ValueError(f"Path template must begin with '/': '{cleaned}'.")
    return cleaned


def _validate_operation_id(val: Optional[Any]) -> Optional[str]:
    """Validates optional operation ID."""
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValueError(f"operation_id must be a string, got {type(val).__name__}.")
    cleaned = val.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_ID_LEN:
        raise ValueError(f"operation_id exceeds maximum length of {MAX_ID_LEN} characters (got {len(cleaned)}).")
    return cleaned


def _validate_metadata_dict(
    meta: Optional[Any],
) -> Dict[str, Union[str, int, float, bool, None]]:
    """
    Validates that metadata strictly conforms to the explicit ALLOWED_METADATA_KEYS allowlist.
    Applies key-specific value type validation, string length limits, and sensitive value filtering.

    Important: Defining an allowlist of approved metadata keys does not guarantee that
    user-provided string values are completely free of sensitive data. Ingestion and prompt-building
    layers must independently sanitize and redact untrusted content before exposing it to LLMs.
    """
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise ValueError(f"Metadata must be a dictionary, got {type(meta).__name__}.")
    if len(meta) > len(ALLOWED_METADATA_KEYS):
        raise ValueError(
            f"Metadata exceeds maximum limit of {len(ALLOWED_METADATA_KEYS)} entries (got {len(meta)})."
        )

    validated: Dict[str, Union[str, int, float, bool, None]] = {}
    for raw_k, v in meta.items():
        if not isinstance(raw_k, str):
            raise ValueError(f"Metadata key must be a string, got {type(raw_k).__name__}.")
        k = raw_k.strip()
        if not k:
            raise ValueError("Metadata key cannot be empty or whitespace-only.")
        if k not in ALLOWED_METADATA_KEYS:
            raise ValueError(
                f"Unrecognized or forbidden metadata key: '{k}'. Must be one of {sorted(ALLOWED_METADATA_KEYS)}."
            )

        # Key-specific value validation
        if k == "status_code":
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError(f"Metadata 'status_code' must be an integer, got {type(v).__name__}.")
            if not (100 <= v <= 599):
                raise ValueError(f"Metadata 'status_code' must be between 100 and 599, got {v}.")
            validated[k] = v
        elif k in ("is_deprecated", "verified"):
            if not isinstance(v, bool):
                raise ValueError(f"Metadata '{k}' must be a boolean, got {type(v).__name__}.")
            validated[k] = v
        else:
            # String fields: tag, category, version, section, doc_format, content_type, scenario
            if not isinstance(v, str):
                raise ValueError(f"Metadata '{k}' must be a string, got {type(v).__name__}.")
            cleaned_val = v.strip()
            if not cleaned_val:
                raise ValueError(f"Metadata string value for key '{k}' cannot be empty or whitespace-only.")
            if len(cleaned_val) > MAX_METADATA_VAL_LEN:
                raise ValueError(
                    f"Metadata string value for key '{k}' exceeds maximum length of {MAX_METADATA_VAL_LEN} characters."
                )
            for pat in SENSITIVE_VALUE_PATTERNS:
                if pat.search(cleaned_val):
                    raise ValueError(
                        f"Metadata value for key '{k}' contains prohibited sensitive credential pattern."
                    )
            validated[k] = cleaned_val

    return validated


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class KnowledgeDocument(BaseModel):
    """
    Represents an ingested knowledge source document (Phase 4B.1).
    Serves as the parent document for partitioned, searchable knowledge chunks.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    document_id: str = Field(..., description="Stable, unique document identifier")
    source_type: KnowledgeSourceType = Field(..., description="Type of knowledge source")
    title: str = Field(..., description="Human-readable title or name of the document")
    source_ref: Optional[str] = Field(
        default=None,
        description="Optional source reference URI, file path, or origin identifier",
    )
    http_method: Optional[str] = Field(
        default=None,
        description="Optional normalized HTTP method for operation-scoped documents",
    )
    path_template: Optional[str] = Field(
        default=None,
        description="Optional normalized OpenAPI path template (e.g. '/api/products/{id}')",
    )
    operation_id: Optional[str] = Field(
        default=None,
        description="Optional OpenAPI operation identifier",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC creation or ingestion timestamp",
    )
    metadata: Dict[str, Union[str, int, float, bool, None]] = Field(
        default_factory=dict,
        description="Bounded, allowlisted metadata dictionary containing safe primitives only",
    )

    @field_validator("document_id")
    @classmethod
    def _val_doc_id(cls, v: Any) -> str:
        return _validate_identifier(v, "document_id", MAX_ID_LEN)

    @field_validator("title")
    @classmethod
    def _val_title(cls, v: Any) -> str:
        return _validate_title(v)

    @field_validator("source_ref")
    @classmethod
    def _val_source_ref(cls, v: Optional[Any]) -> Optional[str]:
        return _validate_source_ref(v)

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

    @field_validator("metadata", mode="before")
    @classmethod
    def _val_metadata(cls, v: Any) -> Dict[str, Union[str, int, float, bool, None]]:
        return _validate_metadata_dict(v)


class KnowledgeChunk(BaseModel):
    """
    Represents a searchable section of a knowledge document (Phase 4B.1).
    Self-contained with sufficient provenance and operation context for independent retrieval.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    chunk_id: str = Field(..., description="Stable, unique chunk identifier")
    document_id: str = Field(..., description="Identifier of the parent KnowledgeDocument")
    content: str = Field(..., description="Searchable textual content of the chunk")
    chunk_index: int = Field(
        default=0,
        ge=0,
        description="Zero-based sequential index of the chunk within the parent document",
    )
    source_type: KnowledgeSourceType = Field(..., description="Type of knowledge source")
    http_method: Optional[str] = Field(
        default=None,
        description="Optional normalized HTTP method for operation-scoped chunks",
    )
    path_template: Optional[str] = Field(
        default=None,
        description="Optional normalized OpenAPI path template",
    )
    operation_id: Optional[str] = Field(
        default=None,
        description="Optional OpenAPI operation identifier",
    )
    failure_category: Optional[Union[FailureCategory, str]] = Field(
        default=None,
        description="Optional failure classification category (e.g. from FailureCategory)",
    )
    source_ref: Optional[str] = Field(
        default=None,
        description="Optional provenance reference (section, heading anchor, or page)",
    )
    metadata: Dict[str, Union[str, int, float, bool, None]] = Field(
        default_factory=dict,
        description="Bounded, allowlisted metadata dictionary containing safe primitives only",
    )

    @model_validator(mode="before")
    @classmethod
    def _handle_text_alias(cls, data: Any) -> Any:
        """Permits 'text' as an intuitive alias for 'content' during construction."""
        if isinstance(data, dict):
            if "content" not in data and "text" in data:
                data = dict(data)
                data["content"] = data.pop("text")
        return data

    @property
    def text(self) -> str:
        """Convenience property alias for chunk content."""
        return self.content

    @field_validator("chunk_id")
    @classmethod
    def _val_chunk_id(cls, v: Any) -> str:
        return _validate_identifier(v, "chunk_id", MAX_ID_LEN)

    @field_validator("document_id")
    @classmethod
    def _val_doc_id(cls, v: Any) -> str:
        return _validate_identifier(v, "document_id", MAX_ID_LEN)

    @field_validator("content")
    @classmethod
    def _val_content(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError(f"Chunk content must be a string, got {type(v).__name__}.")
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Chunk content cannot be empty or whitespace-only.")
        if len(v) > MAX_CHUNK_CONTENT_LEN:
            raise ValueError(
                f"Chunk content exceeds maximum length of {MAX_CHUNK_CONTENT_LEN} characters (got {len(v)})."
            )
        return v

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

    @field_validator("source_ref")
    @classmethod
    def _val_source_ref(cls, v: Optional[Any]) -> Optional[str]:
        return _validate_source_ref(v)

    @field_validator("metadata", mode="before")
    @classmethod
    def _val_metadata(cls, v: Any) -> Dict[str, Union[str, int, float, bool, None]]:
        return _validate_metadata_dict(v)

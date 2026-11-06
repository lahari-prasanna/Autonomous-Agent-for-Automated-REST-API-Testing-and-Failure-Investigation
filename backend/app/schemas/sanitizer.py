"""
Central boundary sanitization engine (Phase 4C).
Dependency-light module safe for schema and service imports.
"""

from __future__ import annotations

import html
import ipaddress
import math
import re
from typing import Any, Optional
import urllib.parse


class ReportDataSanitizer:
    """Sanitizes text, URLs, source references, and parameter values at model boundaries."""

    SECRET_ASSIGNMENT_PATTERN = re.compile(
        r'(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?token|auth|authorization|credential|private[_-]?key)\b\s*([:=])\s*(?!(?:\[REDACTED|Bearer\s+\[REDACTED\]))(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;&\r\n]+)'
    )
    BEARER_PATTERN = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE)
    JWT_PATTERN = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')
    COOKIE_PATTERN = re.compile(r'(?i)\b(cookie|set-cookie):\s*[^\r\n]+', re.IGNORECASE)
    SENSITIVE_KEY_PATTERN = re.compile(
        r'(?i)(token|key|secret|password|passwd|auth|authorization|sig|signature|credential|jwt|bearer|session|cookie|apikey)'
    )
    RELATIVE_REF_PATTERN = re.compile(r'^[a-zA-Z0-9_\-./#]+$')

    @classmethod
    def sanitize_text(cls, text: Optional[str]) -> str:
        """Masks bearer tokens, JWTs, cookies, and secret assignments; strips control chars."""
        if not text:
            return ""
        s = cls.BEARER_PATTERN.sub("Bearer [REDACTED]", str(text))
        s = cls.JWT_PATTERN.sub("[REDACTED_JWT]", s)
        s = cls.COOKIE_PATTERN.sub(r"\1: [REDACTED]", s)
        s = cls.SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2[REDACTED]", s)
        s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
        return s

    @classmethod
    def sanitize_url(cls, url: Optional[str]) -> Optional[str]:
        """
        Strips userinfo credentials, masks sensitive query values, validates host/port,
        and ensures strict HTTP/HTTPS scheme. Returns None if malformed or unsafe.
        """
        if not url:
            return None
        raw = str(url).strip()
        try:
            parsed = urllib.parse.urlsplit(raw)
        except Exception:
            return None

        if parsed.scheme.lower() not in ("http", "https"):
            return None

        if not parsed.hostname:
            return None

        port = None
        try:
            port = parsed.port
        except ValueError:
            return None

        if port is not None and not (1 <= port <= 65535):
            return None

        raw_host = parsed.hostname
        if ":" in raw_host:
            try:
                ipaddress.IPv6Address(raw_host.strip("[]"))
            except ValueError:
                return None
            host_str = f"[{raw_host.strip('[]')}]"
        else:
            host_str = raw_host

        netloc = f"{host_str}:{port}" if port is not None else host_str

        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        sanitized_pairs = []
        for k, v in query_pairs:
            if cls.SENSITIVE_KEY_PATTERN.search(k):
                sanitized_pairs.append((k, "[REDACTED]"))
            else:
                sanitized_pairs.append((k, cls.sanitize_text(v)))
        clean_query = urllib.parse.urlencode(sanitized_pairs)
        clean_path = re.sub(r"[\x00-\x1f]", "", parsed.path)

        return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, clean_path, clean_query, ""))

    @classmethod
    def sanitize_source_reference(cls, ref: Optional[str]) -> Optional[str]:
        """
        Preserves safe relative references (Phase 4B) and valid HTTP(S) URLs.
        Suppresses directory traversal, unsafe schemes, and malformed inputs to None.
        """
        if not ref:
            return None
        s = str(ref).strip()
        # Reject traversal, leading slash, encoded traversal
        if ".." in s or "\\" in s or s.startswith("/") or "%2e" in s.lower() or "%2f" in s.lower() or "%5c" in s.lower():
            return None

        if s.lower().startswith(("http://", "https://")):
            return cls.sanitize_url(s)

        if ":" in s and not s.startswith("paths."):
            return None

        if cls.RELATIVE_REF_PATTERN.match(s):
            return s
        return None

    @classmethod
    def sanitize_parameter_value(cls, param_name: str, value: Any) -> Any:
        """Redacts values for sensitive parameter names, or scrubs string values."""
        if cls.SENSITIVE_KEY_PATTERN.search(str(param_name)):
            return "[REDACTED]"
        if isinstance(value, str):
            return cls.sanitize_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return "[COMPLEX_PAYLOAD_REDACTED]"

    @classmethod
    def ensure_finite_non_negative(cls, val: Any, name: str) -> float:
        """Validates that a numeric field is finite and non-negative."""
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a valid float")
        if not math.isfinite(f) or f < 0.0:
            raise ValueError(f"{name} must be finite and >= 0.0 (got {val})")
        return f

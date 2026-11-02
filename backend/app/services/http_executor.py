"""
Deterministic HTTP Execution Engine (Phase 2B).
Integrates async HTTPX with SSRFSafeAsyncTransport, hard concurrency limits,
10-second bounded timeouts, streaming response size caps, and manual redirect validation.
"""

from typing import Any, Dict, List, Optional
import asyncio
import json
import logging
import time
from urllib.parse import urljoin, urlsplit

import httpx

from backend.app.core.security import (
    SSRFSafeAsyncTransport,
    SSRFSecurityError,
    resolve_and_validate_hostname,
    sanitize_url_for_logging,
    validate_url,
)
from backend.app.schemas.execution import (
    ExecutionRequest,
    ExecutionResult,
    SENSITIVE_HEADER_NAMES,
    sanitize_headers_for_logging,
)

logger = logging.getLogger(__name__)

# Hard limits mandated by architectural specification
MAX_RESPONSE_BYTES = 10 * 1024 * 1024       # 10 MB hard cap
MAX_CONCURRENT_REQUESTS = 5                  # Connection concurrency limit
MAX_TOTAL_TIMEOUT = 10.0                     # Hard upper bound on total duration
MAX_REDIRECT_HOPS = 3                        # Maximum permitted manual redirect hops

DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 7.0
DEFAULT_WRITE_TIMEOUT = 3.0


class ResponseSizeLimitExceeded(Exception):
    """Raised when an incoming response payload exceeds the maximum permitted byte size."""
    pass


class HTTPExecutor:
    """
    Deterministic asynchronous HTTP execution engine.
    
    Guarantees:
    - Zero ordinary HTTP transport usage: strictly utilizes SSRFSafeAsyncTransport.
    - Zero ambient proxy leakage (trust_env=False).
    - DNS rebinding & TOCTOU protection on every request hop.
    - Strict 10-second total timeout limit (cannot be bypassed by caller).
    - Global concurrency throttling (max 5 simultaneous requests).
    - Streamed decompression-bomb protection (10MB payload ceiling).
    - Manual, hop-by-hop redirect verification.
    """

    def __init__(
        self,
        safe_dev_mode: bool = False,
        max_concurrency: int = MAX_CONCURRENT_REQUESTS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self.safe_dev_mode = safe_dev_mode
        # Clamped strictly: callers can reduce for testing but cannot exceed hard architectural limits
        self.max_concurrency = min(max(1, max_concurrency), MAX_CONCURRENT_REQUESTS)
        self.max_response_bytes = min(max_response_bytes, MAX_RESPONSE_BYTES)
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """
        Executes a structured ExecutionRequest and returns an ExecutionResult.
        Throttled by internal concurrency semaphore.
        Enforces a hard wall-clock deadline bounding the entire execution lifecycle,
        starting when execute() is called and including semaphore queue waiting,
        DNS validation, connection setup, redirects, and response streaming.
        """
        start_time = time.perf_counter()
        enforced_timeout_seconds = min(request.timeout, MAX_TOTAL_TIMEOUT)

        async def _throttled_execute() -> ExecutionResult:
            async with self._semaphore:
                return await self._execute_internal(request, start_time, enforced_timeout_seconds)

        try:
            return await asyncio.wait_for(
                _throttled_execute(),
                timeout=enforced_timeout_seconds,
            )
        except (asyncio.TimeoutError, TimeoutError):
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return ExecutionResult(
                method=request.method,
                url=sanitize_url_for_logging(request.url),
                status_code=0,
                headers={},
                body=None,
                raw_body_bytes_len=0,
                latency_ms=latency_ms,
                is_success=False,
                is_timeout=True,
                error_message=f"Request timed out: wall-clock deadline exceeded ({enforced_timeout_seconds}s limit)",
                redirect_count=0,
            )

    async def _execute_internal(
        self,
        request: ExecutionRequest,
        start_time: Optional[float] = None,
        enforced_timeout_seconds: Optional[float] = None,
    ) -> ExecutionResult:
        if start_time is None:
            start_time = time.perf_counter()
        if enforced_timeout_seconds is None:
            enforced_timeout_seconds = min(request.timeout, MAX_TOTAL_TIMEOUT)

        current_url = request.url
        current_method = request.method
        current_headers = dict(request.headers)
        current_json = request.json_body
        current_raw = request.raw_body
        redirect_count = 0
        timeout = httpx.Timeout(
            connect=min(DEFAULT_CONNECT_TIMEOUT, enforced_timeout_seconds),
            read=min(DEFAULT_READ_TIMEOUT, enforced_timeout_seconds),
            write=min(DEFAULT_WRITE_TIMEOUT, enforced_timeout_seconds),
            pool=min(DEFAULT_CONNECT_TIMEOUT, enforced_timeout_seconds),
        )

        # Initialize fortified transport with SSRF protection and connection pinning
        transport = SSRFSafeAsyncTransport(safe_dev_mode=self.safe_dev_mode)

        try:
            async with httpx.AsyncClient(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,  # Automatic redirects strictly disabled
            ) as client:
                while True:
                    # 1. Pre-validate target URL through Phase 2A SSRF Guard
                    try:
                        scheme, hostname, port, _ = validate_url(current_url, safe_dev_mode=self.safe_dev_mode)
                        resolve_and_validate_hostname(hostname, port, safe_dev_mode=self.safe_dev_mode)
                    except SSRFSecurityError as ssrf_err:
                        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
                        return ExecutionResult(
                            request_id=None,
                            method=current_method,
                            url=sanitize_url_for_logging(current_url),
                            status_code=0,
                            headers={},
                            body=None,
                            raw_body_bytes_len=0,
                            latency_ms=latency_ms,
                            is_success=False,
                            is_ssrf_blocked=True,
                            error_message=str(ssrf_err),
                            redirect_count=redirect_count,
                        )

                    # 2. Build HTTPX request payload
                    req_kwargs: Dict[str, Any] = {
                        "headers": current_headers,
                        "params": request.params if redirect_count == 0 else None,
                    }
                    if current_json is not None:
                        req_kwargs["json"] = current_json
                    elif current_raw is not None:
                        req_kwargs["content"] = current_raw

                    # 3. Stream response and enforce 10MB size ceiling on decompressed chunks
                    try:
                        async with client.stream(
                            current_method, current_url, **req_kwargs
                        ) as response:
                            body_bytes = bytearray()
                            async for chunk in response.aiter_bytes():
                                body_bytes.extend(chunk)
                                if len(body_bytes) > self.max_response_bytes:
                                    raise ResponseSizeLimitExceeded(
                                        f"Response body exceeded hard cap of {self.max_response_bytes} bytes."
                                    )

                            status_code = response.status_code
                            response_headers = dict(response.headers)

                    except ResponseSizeLimitExceeded as size_err:
                        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
                        return ExecutionResult(
                            method=current_method,
                            url=sanitize_url_for_logging(current_url),
                            status_code=0,
                            headers={},
                            body=None,
                            raw_body_bytes_len=len(body_bytes) if "body_bytes" in locals() else 0,
                            latency_ms=latency_ms,
                            is_success=False,
                            is_network_error=True,
                            error_message=str(size_err),
                            redirect_count=redirect_count,
                        )

                    # 4. Check for Manual Redirect Traversal
                    is_redirect = status_code in (301, 302, 303, 307, 308)
                    if is_redirect and request.follow_redirects:
                        location = response_headers.get("location")
                        if not location:
                            # Redirect response with no Location header: return as-is
                            break

                        redirect_count += 1
                        if redirect_count > min(request.max_redirects, MAX_REDIRECT_HOPS):
                            # Exceeded hop limit: return response
                            break

                        # Resolve relative Location URL
                        next_url = urljoin(current_url, location)

                        # Strip all sensitive headers if redirecting to different origin
                        cur_parts = urlsplit(current_url)
                        next_parts = urlsplit(next_url)
                        if (cur_parts.scheme, cur_parts.netloc) != (next_parts.scheme, next_parts.netloc):
                            current_headers = {
                                k: v for k, v in current_headers.items()
                                if k.lower() not in SENSITIVE_HEADER_NAMES
                            }

                        # Adjust method and drop body on 301/302/303 standard redirects
                        if status_code == 303 or (status_code in (301, 302) and current_method == "POST"):
                            current_method = "GET"
                            current_json = None
                            current_raw = None

                        current_url = next_url
                        continue  # Proceed to next hop
                    else:
                        break

            # 5. Parse response body safely
            parsed_body = self._parse_response_body(body_bytes, response_headers)
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

            return ExecutionResult(
                method=current_method,
                url=sanitize_url_for_logging(current_url),
                status_code=status_code,
                headers=response_headers,
                body=parsed_body,
                raw_body_bytes_len=len(body_bytes),
                latency_ms=latency_ms,
                is_success=True,
                is_network_error=False,
                is_timeout=False,
                is_ssrf_blocked=False,
                redirect_count=redirect_count,
            )

        except SSRFSecurityError as ssrf_err:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return ExecutionResult(
                method=current_method,
                url=sanitize_url_for_logging(current_url),
                status_code=0,
                headers={},
                body=None,
                raw_body_bytes_len=0,
                latency_ms=latency_ms,
                is_success=False,
                is_ssrf_blocked=True,
                error_message=str(ssrf_err),
                redirect_count=redirect_count,
            )

        except httpx.TimeoutException as timeout_err:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return ExecutionResult(
                method=current_method,
                url=sanitize_url_for_logging(current_url),
                status_code=0,
                headers={},
                body=None,
                raw_body_bytes_len=0,
                latency_ms=latency_ms,
                is_success=False,
                is_timeout=True,
                error_message=f"Request timed out after {enforced_timeout_seconds}s: {timeout_err}",
                redirect_count=redirect_count,
            )

        except (httpx.NetworkError, httpx.TransportError) as net_err:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return ExecutionResult(
                method=current_method,
                url=sanitize_url_for_logging(current_url),
                status_code=0,
                headers={},
                body=None,
                raw_body_bytes_len=0,
                latency_ms=latency_ms,
                is_success=False,
                is_network_error=True,
                error_message=f"Network communication failure: {net_err}",
                redirect_count=redirect_count,
            )

        except Exception as unexpected_err:
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return ExecutionResult(
                method=current_method,
                url=sanitize_url_for_logging(current_url),
                status_code=0,
                headers={},
                body=None,
                raw_body_bytes_len=0,
                latency_ms=latency_ms,
                is_success=False,
                is_network_error=True,
                error_message=f"Unexpected execution error: {unexpected_err}",
                redirect_count=redirect_count,
            )

    def _parse_response_body(self, raw_bytes: bytearray, headers: Dict[str, str]) -> Optional[Any]:
        """
        Attempts to parse received bytes into a structured JSON object.
        Falls back to UTF-8 decoded string if not JSON, or None if empty.
        """
        if not raw_bytes:
            return None

        # Check content-type header hint
        content_type = headers.get("content-type", "").lower()

        # Attempt JSON decoding if content-type indicates JSON or if it starts with { / [
        if "json" in content_type or raw_bytes.startswith((b"{", b"[")):
            try:
                return json.loads(raw_bytes.decode("utf-8"))
            except Exception:
                pass

        # Fallback to UTF-8 text preview
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # Binary payload fallback
            return f"<binary data: {len(raw_bytes)} bytes>"

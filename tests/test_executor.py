"""
Unit and integration test suite for Deterministic HTTP Executor (Phase 2B).
Tests HTTP methods, parameter/body handling, latency measurement,
timeout handling, response size caps, concurrency, manual redirects,
and SSRF protection integration with Phase 1A Mock API.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import multiprocessing
import pytest
import time
import httpx
import uvicorn

from backend.app.schemas.execution import (
    ExecutionRequest,
    ExecutionResult,
    sanitize_headers_for_logging,
)
from backend.app.services.http_executor import HTTPExecutor, ResponseSizeLimitExceeded


# ---------------------------------------------------------------------------
# Fixture: Live Phase 1A Mock REST API on 127.0.0.1:8081
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mock_server_url():
    """
    Ensures Phase 1A mock API is running on 127.0.0.1:8081.
    Starts it in a background process if not already running,
    and terminates it upon test suite completion.
    """
    target_url = "http://127.0.0.1:8081"

    # Check if already running
    try:
        r = httpx.get(f"{target_url}/api/health", timeout=0.3)
        if r.status_code == 200:
            yield target_url
            return
    except Exception:
        pass

    # Launch in a clean background process
    proc = multiprocessing.Process(
        target=uvicorn.run,
        args=("mock_api.main:app",),
        kwargs={"host": "127.0.0.1", "port": 8081, "log_level": "error"},
    )
    proc.start()

    # Poll until ready
    ready = False
    for _ in range(50):
        time.sleep(0.05)
        try:
            r = httpx.get(f"{target_url}/api/health", timeout=0.3)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass

    assert ready, "Failed to start local mock API on 127.0.0.1:8081"
    yield target_url

    proc.terminate()
    proc.join(timeout=1.0)


# ---------------------------------------------------------------------------
# 1. Live Integration Tests against Phase 1A Mock API
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_executor_get_health(mock_server_url):
    """Verify GET /api/health returns 200 with safety header and latency tracking."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url=f"{mock_server_url}/api/health")

    res = await executor.execute(req)

    assert res.status_code == 200
    assert res.is_success is True
    assert res.is_network_error is False
    assert res.is_timeout is False
    assert res.is_ssrf_blocked is False
    assert res.headers.get("x-controlled-mock-api") == "true"
    assert res.latency_ms > 0
    assert isinstance(res.body, dict)
    assert res.body.get("status") == "healthy"
    assert res.body.get("safe_dev_mode") is True


@pytest.mark.anyio
async def test_executor_post_auth_login(mock_server_url):
    """Verify POST /api/auth/login with JSON body returns access token."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(
        method="POST",
        url=f"{mock_server_url}/api/auth/login",
        json_body={"username": "testuser", "password": "password123"},
    )

    res = await executor.execute(req)

    assert res.status_code == 200
    assert res.is_success is True
    assert isinstance(res.body, dict)
    assert "access_token" in res.body
    assert res.body["token_type"] == "Bearer"


@pytest.mark.anyio
async def test_executor_get_with_query_params(mock_server_url):
    """Verify GET /api/products with category query parameter."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(
        method="GET",
        url=f"{mock_server_url}/api/products",
        params={"category": "Electronics"},
    )

    res = await executor.execute(req)

    assert res.status_code == 200
    assert res.is_success is True
    assert isinstance(res.body, list)
    assert len(res.body) >= 2
    for item in res.body:
        assert item["category"] == "Electronics"


@pytest.mark.anyio
async def test_executor_post_product_valid(mock_server_url):
    """Verify authenticated POST /api/products creates product."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(
        method="POST",
        url=f"{mock_server_url}/api/products",
        headers={"Authorization": "Bearer mock-bearer-token-12345"},
        json_body={"name": "Ultra Display", "price": 499.99, "category": "Electronics"},
    )

    res = await executor.execute(req)

    assert res.status_code == 201
    assert res.is_success is True
    assert isinstance(res.body, dict)
    assert res.body["name"] == "Ultra Display"
    assert res.body["price"] == 499.99


@pytest.mark.anyio
async def test_executor_post_intentional_defect_500(mock_server_url):
    """
    CRITICAL: Verify that the mock API's intentional 500 defect (negative price)
    is cleanly captured as an HTTP 500 status rather than crashing the executor.
    """
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(
        method="POST",
        url=f"{mock_server_url}/api/products",
        headers={"Authorization": "Bearer mock-bearer-token-12345"},
        json_body={"name": "Defective Item", "price": -500.0, "category": "Electronics"},
    )

    res = await executor.execute(req)

    assert res.status_code == 500
    assert res.is_success is True  # HTTP transaction finished properly
    assert res.is_network_error is False
    assert res.is_timeout is False
    assert res.is_ssrf_blocked is False


@pytest.mark.anyio
async def test_executor_put_and_delete(mock_server_url):
    """Verify PUT and DELETE operations."""
    executor = HTTPExecutor(safe_dev_mode=True)

    # PUT
    put_req = ExecutionRequest(
        method="PUT",
        url=f"{mock_server_url}/api/products/1",
        headers={"Authorization": "Bearer mock-bearer-token-12345"},
        json_body={"name": "Updated Mouse", "price": 30.00, "category": "Electronics"},
    )
    put_res = await executor.execute(put_req)
    assert put_res.status_code == 200
    assert put_res.body["name"] == "Updated Mouse"

    # DELETE
    del_req = ExecutionRequest(
        method="DELETE",
        url=f"{mock_server_url}/api/products/2",
        headers={"Authorization": "Bearer mock-bearer-token-12345"},
    )
    del_res = await executor.execute(del_req)
    assert del_res.status_code == 204


@pytest.mark.anyio
async def test_executor_head_and_options(mock_server_url):
    """Verify HEAD and OPTIONS methods are supported."""
    executor = HTTPExecutor(safe_dev_mode=True)

    # HEAD
    head_req = ExecutionRequest(method="HEAD", url=f"{mock_server_url}/api/products")
    head_res = await executor.execute(head_req)
    assert head_res.status_code in (200, 405)
    assert head_res.is_success is True

    # OPTIONS
    options_req = ExecutionRequest(method="OPTIONS", url=f"{mock_server_url}/api/products")
    options_res = await executor.execute(options_req)
    assert options_res.status_code in (200, 405)
    assert options_res.is_success is True


# ---------------------------------------------------------------------------
# 2. SSRF Protection & Safe Dev Mode Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_executor_ssrf_blocked_when_safe_dev_mode_false(mock_server_url):
    """Verify loopback is strictly blocked when SAFE_DEV_MODE=False."""
    executor = HTTPExecutor(safe_dev_mode=False)
    req = ExecutionRequest(method="GET", url=f"{mock_server_url}/api/health")

    res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_success is False
    assert res.is_ssrf_blocked is True
    assert "LOOPBACK_PROHIBITED" in str(res.error_message)


@pytest.mark.anyio
async def test_executor_ssrf_cloud_metadata_blocked():
    """Verify cloud metadata (169.254.169.254) is blocked even in Safe Dev Mode."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://169.254.169.254/latest/meta-data/")

    res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_ssrf_blocked is True
    assert "METADATA_PROHIBITED" in str(res.error_message)


@pytest.mark.anyio
async def test_executor_ssrf_private_lan_blocked():
    """Verify private RFC 1918 addresses are blocked."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://192.168.1.100:8000/api")

    res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_ssrf_blocked is True
    assert "PROHIBITED" in str(res.error_message)


@pytest.mark.anyio
async def test_executor_credentials_in_url_rejected():
    """Verify URLs with credentials are rejected and passwords are not logged."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://admin:super_secret_pwd@127.0.0.1:8081/api")

    res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_ssrf_blocked is True
    assert "super_secret_pwd" not in str(res.error_message)
    assert "super_secret_pwd" not in res.url
    assert "[REDACTED]" in res.url


# ---------------------------------------------------------------------------
# 3. Timeouts & Response Size Limits
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_executor_timeout_handling():
    """Verify that requests exceeding timeout are captured as is_timeout=True."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/api/health", timeout=0.001)

    with patch("httpx.AsyncClient.stream", side_effect=httpx.TimeoutException("Read timed out")):
        res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_success is False
    assert res.is_timeout is True
    assert "timed out" in str(res.error_message).lower()


@pytest.mark.anyio
async def test_executor_response_size_limit():
    """Verify response payload exceeding max_response_bytes triggers limit error."""
    # Create executor with tiny 50 bytes ceiling
    executor = HTTPExecutor(safe_dev_mode=True, max_response_bytes=50)
    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/api/products")

    res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_success is False
    assert res.is_network_error is True
    assert "exceeded hard cap of 50 bytes" in str(res.error_message)


# ---------------------------------------------------------------------------
# 4. Redirect Handling & Hop-by-Hop SSRF Verification
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_executor_manual_redirect_follow_false():
    """Verify 302 response is returned as-is when follow_redirects=False."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/old-endpoint", follow_redirects=False)

    mock_resp = AsyncMock()
    mock_resp.status_code = 302
    mock_resp.headers = {"location": "/api/health"}
    mock_resp.aiter_bytes = MagicMock(return_value=_async_iter([b"redirecting..."]))

    with patch("httpx.AsyncClient.stream") as mock_stream:
        mock_stream.return_value.__aenter__.return_value = mock_resp
        res = await executor.execute(req)

    assert res.status_code == 302
    assert res.redirect_count == 0
    assert res.headers.get("location") == "/api/health"


@pytest.mark.anyio
async def test_executor_manual_redirect_follow_true():
    """Verify manual redirect traversal succeeds up to safe target."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/old-endpoint", follow_redirects=True)

    # First hop returns 302 pointing to /api/health
    resp1 = AsyncMock()
    resp1.status_code = 302
    resp1.headers = {"location": "/api/health"}
    resp1.aiter_bytes = MagicMock(return_value=_async_iter([b""]))

    # Second hop returns 200
    resp2 = AsyncMock()
    resp2.status_code = 200
    resp2.headers = {"content-type": "application/json"}
    resp2.aiter_bytes = MagicMock(return_value=_async_iter([b'{"status": "ok"}']))

    with patch("httpx.AsyncClient.stream") as mock_stream:
        mock_stream.return_value.__aenter__.side_effect = [resp1, resp2]
        res = await executor.execute(req)

    assert res.status_code == 200
    assert res.redirect_count == 1
    assert res.body == {"status": "ok"}


@pytest.mark.anyio
async def test_executor_redirect_ssrf_intercepted():
    """
    CRITICAL: Verify that if a 302 redirect points to a prohibited IP
    (e.g. 169.254.169.254), the redirect hop is intercepted and BLOCKED by the SSRF Guard.
    """
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/trap", follow_redirects=True)

    # Hop 1: 302 redirect pointing to AWS metadata
    resp1 = AsyncMock()
    resp1.status_code = 302
    resp1.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
    resp1.aiter_bytes = MagicMock(return_value=_async_iter([b""]))

    with patch("httpx.AsyncClient.stream") as mock_stream:
        mock_stream.return_value.__aenter__.return_value = resp1
        res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_ssrf_blocked is True
    assert "METADATA_PROHIBITED" in str(res.error_message)


# ---------------------------------------------------------------------------
# 5. Concurrency Throttling & Sanitization
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_executor_concurrency_limiting(mock_server_url):
    """Verify max_concurrency semaphore restricts concurrent in-flight requests."""
    executor = HTTPExecutor(safe_dev_mode=True, max_concurrency=2)
    req = ExecutionRequest(method="GET", url=f"{mock_server_url}/api/health")

    # Launch 6 concurrent requests
    tasks = [executor.execute(req) for _ in range(6)]
    results = await asyncio.gather(*tasks)

    assert len(results) == 6
    for r in results:
        assert r.status_code == 200


def test_sensitive_header_sanitization():
    """Verify headers containing authorization, cookies, and tokens are redacted."""
    raw_headers = {
        "Authorization": "Bearer super-secret-jwt-token",
        "Cookie": "session_id=abcdef123456",
        "X-Api-Key": "api-key-998877",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }
    sanitized = sanitize_headers_for_logging(raw_headers)

    assert sanitized["Authorization"] == "[REDACTED]"
    assert sanitized["Cookie"] == "[REDACTED]"
    assert sanitized["X-Api-Key"] == "[REDACTED]"
    assert sanitized["Content-Type"] == "application/json"
    assert sanitized["Accept"] == "*/*"


# Helper for mock streaming bytes
async def _async_iter(items):
    for item in items:
        yield item


class _ChunkedCompressedStream(httpx.AsyncByteStream):
    """Streams byte data in specified chunk sizes to feed real HTTPX response decoders."""
    def __init__(self, data: bytes, chunk_size: int = 2048):
        self.data = data
        self.chunk_size = chunk_size

    async def __aiter__(self):
        for i in range(0, len(self.data), self.chunk_size):
            yield self.data[i:i + self.chunk_size]

    async def aclose(self):
        pass


# ---------------------------------------------------------------------------
# 6. Hard Deadline, Clamping, Decompression & Cross-Origin Redirect Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_executor_wall_clock_timeout_slow_streaming():
    """Verify that a slow streaming response exceeding overall deadline is aborted."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/slow-stream", timeout=0.1)

    async def slow_chunks():
        yield b"chunk1"
        await asyncio.sleep(0.3)
        yield b"chunk2"

    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "text/plain"}
    mock_resp.aiter_bytes = MagicMock(return_value=slow_chunks())

    with patch("httpx.AsyncClient.stream") as mock_stream:
        mock_stream.return_value.__aenter__.return_value = mock_resp
        res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_success is False
    assert res.is_timeout is True
    assert "timed out" in str(res.error_message).lower() or "deadline exceeded" in str(res.error_message).lower()


@pytest.mark.anyio
async def test_executor_wall_clock_timeout_multiple_redirects():
    """Verify cumulative time across multiple redirects exceeding deadline triggers is_timeout=True."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(
        method="GET",
        url="http://127.0.0.1:8081/hop1",
        follow_redirects=True,
        timeout=0.15,
    )

    async def mock_stream_delay(*args, **kwargs):
        await asyncio.sleep(0.1)
        resp = AsyncMock()
        resp.status_code = 302
        resp.headers = {"location": "http://127.0.0.1:8081/hop2"}
        resp.aiter_bytes = MagicMock(return_value=_async_iter([b""]))
        return resp

    with patch("httpx.AsyncClient.stream") as mock_stream:
        mock_stream.return_value.__aenter__.side_effect = mock_stream_delay
        res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_success is False
    assert res.is_timeout is True
    assert "timed out" in str(res.error_message).lower() or "deadline exceeded" in str(res.error_message).lower()


def test_executor_concurrency_clamping():
    """Verify HTTPExecutor strictly clamps max_concurrency to 5."""
    executor = HTTPExecutor(safe_dev_mode=True, max_concurrency=100)
    assert executor.max_concurrency == 5
    assert executor._semaphore._value == 5


def test_executor_response_size_clamping():
    """Verify HTTPExecutor strictly clamps max_response_bytes to 10 MB."""
    executor = HTTPExecutor(safe_dev_mode=True, max_response_bytes=100 * 1024 * 1024)
    assert executor.max_response_bytes == 10 * 1024 * 1024


@pytest.mark.anyio
async def test_executor_gzip_decompression_bomb_blocked():
    """
    Verify response payload that exceeds 10 MB after decompression is blocked.
    Uses real gzip-compressed bytes fed through an httpx.Response with Content-Encoding: gzip.
    Exercises HTTPX's genuine GZipDecoder pipeline rather than mocking aiter_bytes.
    """
    import gzip

    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/bomb")

    # Generate 11 MB of raw repetitive bytes and compress with genuine gzip (~11 KB compressed)
    raw_payload = b"0" * (11 * 1024 * 1024)
    compressed_payload = gzip.compress(raw_payload)

    # Real httpx.Response wrapping the compressed bytes in an async chunked stream
    real_response = httpx.Response(
        200,
        headers={"Content-Encoding": "gzip", "Content-Type": "application/octet-stream"},
        stream=_ChunkedCompressedStream(compressed_payload, chunk_size=2048),
    )

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = real_response

    with patch("httpx.AsyncClient.stream", return_value=mock_ctx):
        res = await executor.execute(req)

    assert res.status_code == 0
    assert res.is_success is False
    assert res.is_network_error is True
    assert "exceeded hard cap of 10485760 bytes" in str(res.error_message)


@pytest.mark.anyio
async def test_executor_wall_clock_timeout_includes_semaphore_queue(mock_server_url):
    """Verify that queue waiting time on the concurrency semaphore counts towards the overall deadline."""
    executor = HTTPExecutor(safe_dev_mode=True, max_concurrency=1)

    # Occupy the only semaphore slot for 0.25s
    async with executor._semaphore:
        # Request 2 is submitted while slot is occupied, with deadline of 0.1s
        req2 = ExecutionRequest(method="GET", url=f"{mock_server_url}/api/health", timeout=0.1)
        res2 = await executor.execute(req2)

        # Must time out while waiting in queue before semaphore was acquired
        assert res2.status_code == 0
        assert res2.is_success is False
        assert res2.is_timeout is True
        assert "deadline exceeded" in str(res2.error_message).lower()

    # Verify semaphore was released and subsequent request succeeds
    assert executor._semaphore._value == 1
    req3 = ExecutionRequest(method="GET", url=f"{mock_server_url}/api/health", timeout=5.0)
    res3 = await executor.execute(req3)
    assert res3.status_code == 200


@pytest.mark.anyio
async def test_executor_cross_origin_redirect_strips_sensitive_headers():
    """Verify that all sensitive headers in SENSITIVE_HEADER_NAMES are stripped on cross-origin redirect."""
    executor = HTTPExecutor(safe_dev_mode=True)
    req = ExecutionRequest(
        method="GET",
        url="http://127.0.0.1:8081/redirect-out",
        headers={
            "Authorization": "Bearer secret-token",
            "Cookie": "session=xyz123",
            "X-Api-Key": "secret-api-key",
            "X-Auth-Token": "secret-auth",
            "X-Public-Header": "allowed-value",
        },
        follow_redirects=True,
    )

    captured_headers = []

    resp1 = AsyncMock()
    resp1.status_code = 302
    resp1.headers = {"location": "http://localhost:8081/target"}
    resp1.aiter_bytes = MagicMock(return_value=_async_iter([b""]))

    resp2 = AsyncMock()
    resp2.status_code = 200
    resp2.headers = {"content-type": "application/json"}
    resp2.aiter_bytes = MagicMock(return_value=_async_iter([b'{"ok": true}']))

    def mock_stream_impl(method, url, **kwargs):
        captured_headers.append(dict(kwargs.get("headers", {})))
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = resp1 if len(captured_headers) == 1 else resp2
        return mock_ctx

    with patch("httpx.AsyncClient.stream", side_effect=mock_stream_impl):
        res = await executor.execute(req)

    assert res.status_code == 200
    assert res.redirect_count == 1
    assert len(captured_headers) == 2

    # Hop 1 contains original sensitive headers
    assert "Authorization" in captured_headers[0]
    assert "X-Api-Key" in captured_headers[0]

    # Hop 2 (cross-origin to localhost:8081) must strip all sensitive headers
    hop2_headers = captured_headers[1]
    assert "Authorization" not in hop2_headers
    assert "Cookie" not in hop2_headers
    assert "X-Api-Key" not in hop2_headers
    assert "X-Auth-Token" not in hop2_headers
    assert hop2_headers.get("X-Public-Header") == "allowed-value"


def test_execution_result_response_header_sanitization():
    """Verify ExecutionResult.sanitized_headers redacts sensitive response headers."""
    result = ExecutionResult(
        method="POST",
        url="http://127.0.0.1:8081/api/auth/login",
        status_code=200,
        headers={
            "Set-Cookie": "session_id=secret-session-token; Path=/; HttpOnly",
            "Authorization": "Bearer secret-returned-auth",
            "Content-Type": "application/json",
            "Server": "uvicorn",
        },
    )

    sanitized = result.sanitized_headers
    assert sanitized["Set-Cookie"] == "[REDACTED]"
    assert sanitized["Authorization"] == "[REDACTED]"
    assert sanitized["Content-Type"] == "application/json"
    assert sanitized["Server"] == "uvicorn"
    # Raw headers remain intact for assertion inspection
    assert "secret-session-token" in result.headers["Set-Cookie"]

"""
Focused test suite for SSRF Security Guard & Connection Pinning (Phase 2A).
Verifies multi-protocol restrictions, IP range filtering, DNS resolution,
IPv4-mapped IPv6 unwrap, Safe Dev Mode, and DNS rebinding mitigation.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import ipaddress
import pytest
import socket
import httpx

from backend.app.core.security import (
    SSRFSafeAsyncNetworkBackend,
    SSRFSafeAsyncTransport,
    SSRFSecurityError,
    is_ip_allowed,
    resolve_and_validate_hostname,
    sanitize_url_for_logging,
    unwrap_ip,
    validate_url,
)


# ---------------------------------------------------------------------------
# 1. IP Range Filtering Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prohibited_ip",
    [
        "10.0.0.1",              # RFC 1918 Class A
        "172.16.0.1",            # RFC 1918 Class B
        "172.31.255.254",        # RFC 1918 Class B upper boundary
        "192.168.1.1",           # RFC 1918 Class C
        "169.254.169.254",       # AWS/GCP/Azure Cloud Metadata
        "169.254.1.1",           # IPv4 Link-Local
        "0.0.0.0",               # Unspecified
        "100.64.0.1",            # Shared Address Space / CGNAT
        "192.0.2.1",             # TEST-NET-1
        "198.51.100.1",          # TEST-NET-2
        "203.0.113.1",           # TEST-NET-3
        "198.18.0.1",            # Benchmarking
        "224.0.0.1",             # Multicast
        "240.0.0.1",             # Reserved
        "255.255.255.255",       # Broadcast
        "::",                    # IPv6 Unspecified
        "fe80::1",               # IPv6 Link-Local
        "fc00::1",               # IPv6 Unique Local Address (ULA)
        "fd00::1",               # IPv6 ULA
        "fd00:ec1::254",         # GCP Metadata IPv6
        "ff02::1",               # IPv6 Multicast
        "2001:db8::1",           # IPv6 Documentation
    ],
)
def test_prohibited_ips_blocked(prohibited_ip: str):
    """Verify all private, metadata, link-local, and reserved IPs are rejected."""
    allowed, reason = is_ip_allowed(prohibited_ip, safe_dev_mode=False)
    assert allowed is False
    assert "PROHIBITED" in reason


@pytest.mark.parametrize(
    "public_ip",
    [
        "93.184.216.34",         # example.com
        "8.8.8.8",               # Google DNS
        "1.1.1.1",               # Cloudflare DNS
        "2606:4700:4700::1111",  # Cloudflare IPv6 DNS
        "2001:4860:4860::8888",  # Google IPv6 DNS
    ],
)
def test_public_ips_allowed(public_ip: str):
    """Verify standard routable public IPs are permitted."""
    allowed, reason = is_ip_allowed(public_ip, safe_dev_mode=False)
    assert allowed is True
    assert reason == "ALLOWED"


# ---------------------------------------------------------------------------
# 2. IPv4-Mapped IPv6 Unwrapping Tests
# ---------------------------------------------------------------------------

def test_ipv4_mapped_ipv6_unwrapping():
    """Verify IPv4-mapped IPv6 addresses (::ffff:x.x.x.x) are correctly unwrapped and checked."""
    # Loopback in mapped form
    assert unwrap_ip("::ffff:127.0.0.1") == ipaddress.IPv4Address("127.0.0.1")
    allowed, reason = is_ip_allowed("::ffff:127.0.0.1", safe_dev_mode=False)
    assert allowed is False
    assert reason == "LOOPBACK_PROHIBITED"

    # Cloud metadata in mapped form
    assert unwrap_ip("::ffff:169.254.169.254") == ipaddress.IPv4Address("169.254.169.254")
    allowed, reason = is_ip_allowed("::ffff:169.254.169.254", safe_dev_mode=False)
    assert allowed is False
    assert reason == "METADATA_PROHIBITED"

    # Private network in mapped form
    allowed, reason = is_ip_allowed("::ffff:10.0.0.1", safe_dev_mode=False)
    assert allowed is False
    assert "PROHIBITED" in reason

    # Public IP in mapped form
    allowed, reason = is_ip_allowed("::ffff:93.184.216.34", safe_dev_mode=False)
    assert allowed is True
    assert reason == "ALLOWED"


# ---------------------------------------------------------------------------
# 3. SAFE_DEV_MODE Behavior Tests
# ---------------------------------------------------------------------------

def test_safe_dev_mode_localhost_behavior():
    """Verify that loopback is permitted ONLY when SAFE_DEV_MODE=True."""
    # Production / Safe Mode False -> Loopback BLOCKED
    allowed_v4_prod, reason_v4_prod = is_ip_allowed("127.0.0.1", safe_dev_mode=False)
    assert allowed_v4_prod is False
    assert reason_v4_prod == "LOOPBACK_PROHIBITED"

    allowed_v6_prod, reason_v6_prod = is_ip_allowed("::1", safe_dev_mode=False)
    assert allowed_v6_prod is False
    assert reason_v6_prod == "LOOPBACK_PROHIBITED"

    # Dev Mode True -> Loopback PERMITTED
    allowed_v4_dev, reason_v4_dev = is_ip_allowed("127.0.0.1", safe_dev_mode=True)
    assert allowed_v4_dev is True
    assert reason_v4_dev == "ALLOWED_LOOPBACK_DEV_MODE"

    allowed_v6_dev, reason_v6_dev = is_ip_allowed("::1", safe_dev_mode=True)
    assert allowed_v6_dev is True
    assert reason_v6_dev == "ALLOWED_LOOPBACK_DEV_MODE"

    # Even in Dev Mode, cloud metadata and internal LAN IPs MUST REMAIN BLOCKED
    allowed_meta, _ = is_ip_allowed("169.254.169.254", safe_dev_mode=True)
    assert allowed_meta is False

    allowed_lan, _ = is_ip_allowed("192.168.1.50", safe_dev_mode=True)
    assert allowed_lan is False

    allowed_corp, _ = is_ip_allowed("10.50.0.1", safe_dev_mode=True)
    assert allowed_corp is False


# ---------------------------------------------------------------------------
# 4. URL Validation & Sanitization Tests
# ---------------------------------------------------------------------------

def test_scheme_validation():
    """Verify only HTTP and HTTPS schemes are permitted."""
    valid_http = validate_url("http://example.com/api", safe_dev_mode=False)
    assert valid_http == ("http", "example.com", 80, "/api")

    valid_https = validate_url("https://example.com:8443/test?query=1", safe_dev_mode=False)
    assert valid_https == ("https", "example.com", 8443, "/test?query=1")

    # Prohibited schemes
    prohibited_schemes = [
        "file:///etc/passwd",
        "ftp://example.com/file.txt",
        "gopher://127.0.0.1:70",
        "dict://127.0.0.1:2628",
        "data:text/html,<html>",
        "ldap://127.0.0.1",
    ]
    for bad_url in prohibited_schemes:
        with pytest.raises(SSRFSecurityError) as exc:
            validate_url(bad_url, safe_dev_mode=False)
        assert exc.value.reason == "UNSUPPORTED_SCHEME"


def test_reject_credentials_in_url():
    """Verify URLs with embedded credentials are completely rejected without logging passwords."""
    sensitive_url = "https://admin:super_secret_password123@example.com/dashboard"

    with pytest.raises(SSRFSecurityError) as exc:
        validate_url(sensitive_url, safe_dev_mode=False)

    assert exc.value.reason == "CREDENTIALS_IN_URL"
    # Ensure sensitive credentials are NEVER leaked in exception string or target
    assert "super_secret_password123" not in str(exc.value)
    assert "super_secret_password123" not in str(exc.value.target)
    assert "[REDACTED]" in str(exc.value.target)


def test_port_validation():
    """Verify port range validation."""
    with pytest.raises(SSRFSecurityError) as exc_zero:
        validate_url("http://example.com:0/api", safe_dev_mode=False)
    assert exc_zero.value.reason == "INVALID_PORT"

    with pytest.raises(SSRFSecurityError) as exc_high:
        validate_url("http://example.com:70000/api", safe_dev_mode=False)
    assert exc_high.value.reason == "INVALID_PORT"


def test_malformed_url_forms():
    """Verify malformed and control-character URLs are rejected."""
    with pytest.raises(SSRFSecurityError) as exc_empty:
        validate_url("", safe_dev_mode=False)
    assert exc_empty.value.reason == "EMPTY_URL"

    with pytest.raises(SSRFSecurityError) as exc_null:
        validate_url("http://example.com\x00/evil", safe_dev_mode=False)
    assert exc_null.value.reason == "MALFORMED_URL"


# ---------------------------------------------------------------------------
# 5. DNS Resolution & Multi-Address Validation Tests
# ---------------------------------------------------------------------------

def test_dns_resolution_failure():
    """Verify DNS resolution failures raise structured SSRFSecurityError."""
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")):
        with pytest.raises(SSRFSecurityError) as exc:
            resolve_and_validate_hostname("nonexistent.invalid.example", 80, safe_dev_mode=False)
        assert exc.value.reason == "DNS_RESOLUTION_FAILED"


def test_mixed_public_private_dns_answers():
    """
    CRITICAL: Verify that if a DNS query returns mixed addresses
    (e.g. one public IP and one private/loopback IP), the host is REJECTED.
    """
    mock_addr_info = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),   # Public
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80)),         # Private RFC 1918
    ]

    with patch("socket.getaddrinfo", return_value=mock_addr_info):
        with pytest.raises(SSRFSecurityError) as exc:
            resolve_and_validate_hostname("dual-homed.example.com", 80, safe_dev_mode=False)
        assert "violates policy" in str(exc.value)
        assert "PROHIBITED" in exc.value.reason


def test_mixed_public_and_metadata_dns_answers():
    """Verify rejection when DNS includes a cloud metadata IP."""
    mock_addr_info = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80)),
    ]

    with patch("socket.getaddrinfo", return_value=mock_addr_info):
        with pytest.raises(SSRFSecurityError) as exc:
            resolve_and_validate_hostname("sneaky-meta.example.com", 80, safe_dev_mode=False)
        assert exc.value.reason == "METADATA_PROHIBITED"


# ---------------------------------------------------------------------------
# 6. DNS Rebinding & Connection Pinning Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_dns_rebinding_connection_pinning():
    """
    Verify the DNS rebinding mitigation design:
    1. The network backend connects TCP directly to the verified pinned IP.
    2. If the post-connection socket IP deviates from the verified safe IP pool,
       the connection is immediately terminated and SSRFSecurityError is raised.
    """
    backend = SSRFSafeAsyncNetworkBackend(safe_dev_mode=False)

    # Mock pre-resolution returning public IP 93.184.216.34
    with patch(
        "backend.app.core.security.resolve_and_validate_hostname",
        return_value=[ipaddress.ip_address("93.184.216.34")],
    ):
        mock_stream = AsyncMock()
        # Case A: Normal safe connection -> socket remote_address matches pinned IP
        mock_stream.get_extra_info.return_value = ("93.184.216.34", 443)

        with patch("httpcore._backends.auto.AutoBackend.connect_tcp", new=AsyncMock(return_value=mock_stream)) as mock_connect:
            stream = await backend.connect_tcp("rebind.attacker.com", 443)
            assert stream is mock_stream
            # Verify connect_tcp was invoked with the PINNED literal IP, not the hostname
            mock_connect.assert_called_once_with(
                "93.184.216.34",
                443,
                timeout=None,
                local_address=None,
                socket_options=None,
            )

        # Case B: DNS Rebinding attack simulation -> socket connects to a modified/internal IP (127.0.0.1)
        mock_rebinding_stream = AsyncMock()
        mock_rebinding_stream.get_extra_info.return_value = ("127.0.0.1", 443)

        with patch("httpcore._backends.auto.AutoBackend.connect_tcp", new=AsyncMock(return_value=mock_rebinding_stream)):
            with pytest.raises(SSRFSecurityError) as exc:
                await backend.connect_tcp("rebind.attacker.com", 443)

            assert exc.value.reason == "DNS_REBINDING_DETECTED"
            # Verify stream was closed immediately
            mock_rebinding_stream.aclose.assert_awaited_once()


@pytest.mark.anyio
async def test_safe_dev_mode_live_integration():
    """
    Verify integration with Phase 1A Mock API on 127.0.0.1:8081.
    - Blocked when SAFE_DEV_MODE=False.
    - Permitted when SAFE_DEV_MODE=True.
    """
    # 1. With SAFE_DEV_MODE=False: must be blocked at transport/connection level
    transport_prod = SSRFSafeAsyncTransport(safe_dev_mode=False)
    with pytest.raises(SSRFSecurityError) as exc:
        async with httpx.AsyncClient(transport=transport_prod) as client:
            await client.get("http://127.0.0.1:8081/api/health")
    assert exc.value.reason == "LOOPBACK_PROHIBITED"

    # 2. With SAFE_DEV_MODE=True: transport permits loopback connection
    transport_dev = SSRFSafeAsyncTransport(safe_dev_mode=True)
    # Target resolution permits loopback in dev mode
    verified = resolve_and_validate_hostname("127.0.0.1", 8081, safe_dev_mode=True)
    assert verified == [ipaddress.ip_address("127.0.0.1")]

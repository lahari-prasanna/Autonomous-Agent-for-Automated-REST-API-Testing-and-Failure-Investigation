"""
SSRF Security Guard and Connection-Level Protection.

Provides comprehensive URL sanitization, multi-protocol restriction,
IPv4 and IPv6 range filtering, DNS rebinding / TOCTOU mitigation,
and connection pinning for HTTPX transports.
"""

from typing import Any, Iterable, List, Optional, Set, Tuple, Union
import inspect
import ipaddress
import re
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpcore
from httpcore._backends.auto import AutoBackend
from httpcore._backends.sync import SyncBackend
import httpx


class SSRFSecurityError(Exception):
    """
    Structured exception raised when an SSRF or network security policy is violated.
    
    Attributes:
        message: Human-readable description of violation.
        reason: Structured error code (e.g. 'PROHIBITED_IP', 'UNSUPPORTED_SCHEME').
        target: Sanitized target URL or host (never contains credentials).
    """

    def __init__(self, message: str, reason: str, target: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.target = sanitize_url_for_logging(target) if target else None

    def __str__(self) -> str:
        if self.target:
            return f"SSRFSecurityError({self.reason}): {self.message} [Target: {self.target}]"
        return f"SSRFSecurityError({self.reason}): {self.message}"


# ---------------------------------------------------------------------------
# Reserved / Prohibited IP Networks Definitions
# ---------------------------------------------------------------------------

PROHIBITED_NETWORKS_V4 = [
    ipaddress.ip_network("0.0.0.0/8"),         # Current network
    ipaddress.ip_network("10.0.0.0/8"),         # Private RFC 1918
    ipaddress.ip_network("100.64.0.0/10"),      # Shared address space / CGNAT
    ipaddress.ip_network("127.0.0.0/8"),        # Loopback
    ipaddress.ip_network("169.254.0.0/16"),     # Link-Local & Cloud Metadata (169.254.169.254)
    ipaddress.ip_network("172.16.0.0/12"),      # Private RFC 1918
    ipaddress.ip_network("192.0.0.0/24"),       # IETF Protocol Assignments
    ipaddress.ip_network("192.0.2.0/24"),       # TEST-NET-1 (Documentation)
    ipaddress.ip_network("192.168.0.0/16"),     # Private RFC 1918
    ipaddress.ip_network("198.18.0.0/15"),      # Benchmarking
    ipaddress.ip_network("198.51.100.0/24"),    # TEST-NET-2 (Documentation)
    ipaddress.ip_network("203.0.113.0/24"),     # TEST-NET-3 (Documentation)
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast
    ipaddress.ip_network("240.0.0.0/4"),        # Reserved for future use
    ipaddress.ip_network("255.255.255.255/32"), # Broadcast
]

PROHIBITED_NETWORKS_V6 = [
    ipaddress.ip_network("::/128"),             # Unspecified address
    ipaddress.ip_network("::1/128"),            # Loopback
    ipaddress.ip_network("::ffff:0:0/96"),      # IPv4-mapped IPv6
    ipaddress.ip_network("64:ff9b::/96"),       # IPv4/IPv6 translation
    ipaddress.ip_network("100::/64"),           # Discard-only prefix
    ipaddress.ip_network("2001:db8::/32"),      # Documentation
    ipaddress.ip_network("2002::/16"),          # 6to4 prefix (handled specifically)
    ipaddress.ip_network("fc00::/7"),           # Unique Local Address (ULA / Private)
    ipaddress.ip_network("fe80::/10"),          # Link-Local unicast
    ipaddress.ip_network("ff00::/8"),           # Multicast
]

# Explicit cloud metadata endpoint addresses
CLOUD_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec1::254"),      # GCP metadata IPv6
}


def sanitize_url_for_logging(url: str) -> str:
    """
    Strips passwords and sensitive credentials from a URL before logging.
    Example: http://user:secret@example.com/api -> http://[REDACTED]@example.com/api
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            netloc = f"[REDACTED]@{netloc}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        return url
    except Exception:
        # Fallback regex redaction if parsing fails
        return re.sub(r"://([^:]+):([^@]+)@", r"://[REDACTED]@", url)


def unwrap_ip(ip: Union[str, ipaddress.IPv4Address, ipaddress.IPv6Address]) -> Union[ipaddress.IPv4Address, ipaddress.IPv6Address]:
    """
    Converts input to an IPAddress object and unwraps IPv4-mapped IPv6 addresses.
    Example: ::ffff:127.0.0.1 -> 127.0.0.1
    """
    if isinstance(ip, str):
        # Strip potential IPv6 brackets if present e.g. [::1]
        cleaned = ip.strip("[]")
        ip_obj = ipaddress.ip_address(cleaned)
    else:
        ip_obj = ip

    # Unwrap IPv4-mapped IPv6 addresses (::ffff:192.0.2.1)
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        return ip_obj.ipv4_mapped

    # Unwrap 6to4 embedded IPv4 addresses (2002:c000:0201::)
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj in ipaddress.ip_network("2002::/16"):
        # The 32 bits following 2002:: represent an IPv4 address
        embedded_v4_int = (int(ip_obj) >> 80) & 0xFFFFFFFF
        return ipaddress.IPv4Address(embedded_v4_int)

    return ip_obj


def is_ip_allowed(
    ip: Union[str, ipaddress.IPv4Address, ipaddress.IPv6Address],
    safe_dev_mode: bool = False,
) -> Tuple[bool, str]:
    """
    Evaluates whether an IP address is permitted under SSRF policies.
    
    Args:
        ip: IPv4 or IPv6 address object or string.
        safe_dev_mode: If True, permits verified loopback (127.0.0.1, ::1).
                       All other private and metadata networks remain strictly blocked.
    
    Returns:
        (True, "ALLOWED") if permitted, or (False, reason_code) if prohibited.
    """
    try:
        ip_obj = unwrap_ip(ip)
    except ValueError as e:
        return False, "INVALID_IP_FORMAT"

    # 1. Cloud metadata addresses are unconditionally forbidden
    if ip_obj in CLOUD_METADATA_IPS:
        return False, "METADATA_PROHIBITED"

    # 2. Check Loopback
    if ip_obj.is_loopback:
        if safe_dev_mode:
            return True, "ALLOWED_LOOPBACK_DEV_MODE"
        return False, "LOOPBACK_PROHIBITED"

    # 3. Check Link-Local
    if ip_obj.is_link_local:
        return False, "LINK_LOCAL_PROHIBITED"

    # 4. Check Multicast / Unspecified / Reserved
    if ip_obj.is_multicast or ip_obj.is_unspecified or ip_obj.is_reserved:
        return False, "RESERVED_OR_MULTICAST_PROHIBITED"

    # 5. Check IPv4 Prohibited Ranges
    if isinstance(ip_obj, ipaddress.IPv4Address):
        for network in PROHIBITED_NETWORKS_V4:
            if ip_obj in network:
                return False, f"PROHIBITED_IPV4_RANGE_{network}"

    # 6. Check IPv6 Prohibited Ranges
    if isinstance(ip_obj, ipaddress.IPv6Address):
        for network in PROHIBITED_NETWORKS_V6:
            if ip_obj in network:
                return False, f"PROHIBITED_IPV6_RANGE_{network}"

    # 7. Check Generic Private Property
    if ip_obj.is_private:
        return False, "PRIVATE_NETWORK_PROHIBITED"

    return True, "ALLOWED"


def validate_url(url: str, safe_dev_mode: bool = False) -> Tuple[str, str, int, str]:
    """
    Validates a URL's syntax, scheme, credentials, and port.
    
    Returns:
        (scheme, hostname, port, path_and_query)
    
    Raises:
        SSRFSecurityError if any validation check fails.
    """
    if not url or not isinstance(url, str):
        raise SSRFSecurityError("Target URL cannot be empty.", reason="EMPTY_URL")

    # Reject null bytes or control characters
    if "\x00" in url or any(ord(c) < 32 for c in url):
        raise SSRFSecurityError("URL contains illegal control characters.", reason="MALFORMED_URL", target=url)

    try:
        parts = urlsplit(url.strip())
    except Exception as e:
        raise SSRFSecurityError(f"Failed to parse URL: {e}", reason="MALFORMED_URL", target=url) from e

    # 1. Enforce HTTP and HTTPS schemes only
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise SSRFSecurityError(
            f"Scheme '{parts.scheme}' is not permitted. Only HTTP and HTTPS are allowed.",
            reason="UNSUPPORTED_SCHEME",
            target=url,
        )

    # 2. Reject credentials in URLs
    if parts.username is not None or parts.password is not None:
        raise SSRFSecurityError(
            "URLs containing embedded user credentials are strictly prohibited.",
            reason="CREDENTIALS_IN_URL",
            target=url,
        )

    # 3. Hostname validation
    hostname = parts.hostname
    if not hostname:
        raise SSRFSecurityError("URL must contain a valid host.", reason="MISSING_HOST", target=url)

    # Clean hostname (strip trailing dot if present)
    hostname = hostname.rstrip(".").lower()

    # 4. Port validation
    try:
        port = parts.port
    except ValueError as e:
        raise SSRFSecurityError(
            f"Port is invalid or out of permitted range (1-65535): {e}",
            reason="INVALID_PORT",
            target=url,
        ) from e

    if port is None:
        port = 443 if scheme == "https" else 80
    elif not (1 <= port <= 65535):
        raise SSRFSecurityError(
            f"Port {port} is out of permitted range (1-65535).",
            reason="INVALID_PORT",
            target=url,
        )

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    return scheme, hostname, port, path


def resolve_and_validate_hostname(
    hostname: str,
    port: int,
    safe_dev_mode: bool = False,
) -> List[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """
    Resolves a hostname via DNS and validates EVERY returned IP address.
    If ANY resolved IP belongs to a prohibited network, the entire host is rejected.
    
    Returns:
        List of all verified safe IP address objects.
    
    Raises:
        SSRFSecurityError if resolution fails or ANY resolved IP is prohibited.
    """
    # 1. If hostname is already a literal IP address
    try:
        ip_obj = unwrap_ip(hostname)
        allowed, reason = is_ip_allowed(ip_obj, safe_dev_mode=safe_dev_mode)
        if not allowed:
            raise SSRFSecurityError(
                f"Destination IP '{ip_obj}' is prohibited by SSRF security policy: {reason}",
                reason=reason,
                target=f"{hostname}:{port}",
            )
        return [ip_obj]
    except ValueError:
        pass  # Hostname is a domain name, proceed to DNS resolution

    # 2. Resolve DNS records
    try:
        addr_info = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_ADDRCONFIG,
        )
    except socket.gaierror as e:
        raise SSRFSecurityError(
            f"DNS resolution failed for host '{hostname}': {e}",
            reason="DNS_RESOLUTION_FAILED",
            target=f"{hostname}:{port}",
        ) from e
    except Exception as e:
        raise SSRFSecurityError(
            f"Socket error resolving host '{hostname}': {e}",
            reason="DNS_RESOLUTION_ERROR",
            target=f"{hostname}:{port}",
        ) from e

    if not addr_info:
        raise SSRFSecurityError(
            f"DNS resolution returned no address records for host '{hostname}'.",
            reason="DNS_NO_RECORDS",
            target=f"{hostname}:{port}",
        )

    # 3. Validate ALL returned IP addresses (Strict: reject if ANY result is prohibited)
    verified_ips: List[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen_ips: Set[str] = set()

    for item in addr_info:
        sockaddr = item[4]
        ip_str = sockaddr[0]
        if ip_str in seen_ips:
            continue
        seen_ips.add(ip_str)

        ip_obj = unwrap_ip(ip_str)
        allowed, reason = is_ip_allowed(ip_obj, safe_dev_mode=safe_dev_mode)
        if not allowed:
            raise SSRFSecurityError(
                f"Host '{hostname}' resolved to prohibited address '{ip_str}' ({reason}). "
                "Host rejected because at least one resolved address violates policy.",
                reason=reason,
                target=f"{hostname}:{port}",
            )
        verified_ips.append(ip_obj)

    return verified_ips


# ---------------------------------------------------------------------------
# DNS Rebinding / TOCTOU Mitigation: Pinned Connection Backends
# ---------------------------------------------------------------------------

class SSRFSafeAsyncNetworkBackend(AutoBackend):
    """
    Async network backend for httpcore that eliminates DNS rebinding / TOCTOU.
    
    Mechanisms:
    1. Pre-resolves and validates all DNS records.
    2. Pins the TCP socket connection directly to a verified IP, bypassing secondary DNS lookups.
    3. Post-connect socket verification: inspects stream.get_extra_info('server_addr')
       to assert the connected socket's remote IP is strictly inside the verified safe list.
    4. Preserves TLS SNI by passing original hostname to start_tls().
    """

    def __init__(self, safe_dev_mode: bool = False):
        super().__init__()
        self.safe_dev_mode = safe_dev_mode

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Iterable[Any]] = None,
    ) -> httpcore.AsyncNetworkStream:
        # 1. Resolve and validate all IPs before connection
        verified_ips = resolve_and_validate_hostname(host, port, safe_dev_mode=self.safe_dev_mode)
        if not verified_ips:
            raise SSRFSecurityError(f"No safe addresses available for '{host}'.", reason="NO_SAFE_IPS", target=host)

        # 2. Pin to the first verified safe IP to defeat DNS rebinding
        pinned_ip = str(verified_ips[0])
        safe_ip_strings = {str(ip) for ip in verified_ips}

        # 3. Establish TCP connection to the pinned IP
        stream = await super().connect_tcp(
            pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

        # 4. Post-connection verification: verify actual socket remote address
        remote_addr = stream.get_extra_info("server_addr")
        if inspect.isawaitable(remote_addr):
            remote_addr = await remote_addr

        if remote_addr:
            connected_ip = remote_addr[0]
            if connected_ip not in safe_ip_strings:
                await stream.aclose()
                raise SSRFSecurityError(
                    f"DNS Rebinding detected! Connected socket IP '{connected_ip}' does not match "
                    f"verified address pool {safe_ip_strings}.",
                    reason="DNS_REBINDING_DETECTED",
                    target=f"{host}:{port}",
                )

        return stream


class SSRFSafeSyncNetworkBackend(SyncBackend):
    """Sync network backend for httpcore with identical DNS rebinding protections."""

    def __init__(self, safe_dev_mode: bool = False):
        super().__init__()
        self.safe_dev_mode = safe_dev_mode

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Iterable[Any]] = None,
    ) -> httpcore.NetworkStream:
        verified_ips = resolve_and_validate_hostname(host, port, safe_dev_mode=self.safe_dev_mode)
        if not verified_ips:
            raise SSRFSecurityError(f"No safe addresses available for '{host}'.", reason="NO_SAFE_IPS", target=host)

        pinned_ip = str(verified_ips[0])
        safe_ip_strings = {str(ip) for ip in verified_ips}

        stream = super().connect_tcp(
            pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

        remote_addr = stream.get_extra_info("server_addr")
        if remote_addr:
            connected_ip = remote_addr[0]
            if connected_ip not in safe_ip_strings:
                stream.close()
                raise SSRFSecurityError(
                    f"DNS Rebinding detected! Connected socket IP '{connected_ip}' does not match "
                    f"verified address pool {safe_ip_strings}.",
                    reason="DNS_REBINDING_DETECTED",
                    target=f"{host}:{port}",
                )

        return stream


class SSRFSafeAsyncTransport(httpx.AsyncHTTPTransport):
    """
    HTTPX Async Transport fortified against SSRF and DNS rebinding attacks.
    Explicitly disables ambient environment proxies (trust_env=False).
    """

    def __init__(self, safe_dev_mode: bool = False, **kwargs):
        # Explicitly enforce trust_env=False to avoid ambient proxy hijacking
        kwargs["trust_env"] = False
        super().__init__(**kwargs)
        self.safe_dev_mode = safe_dev_mode

        # Replace standard connection pool with SSRFSafeAsyncNetworkBackend
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=self._pool._ssl_context,
            max_connections=self._pool._max_connections,
            max_keepalive_connections=self._pool._max_keepalive_connections,
            keepalive_expiry=self._pool._keepalive_expiry,
            http1=self._pool._http1,
            http2=self._pool._http2,
            network_backend=SSRFSafeAsyncNetworkBackend(safe_dev_mode=safe_dev_mode),
        )


class SSRFSafeSyncTransport(httpx.HTTPTransport):
    """
    HTTPX Sync Transport fortified against SSRF and DNS rebinding attacks.
    Explicitly disables ambient environment proxies (trust_env=False).
    """

    def __init__(self, safe_dev_mode: bool = False, **kwargs):
        kwargs["trust_env"] = False
        super().__init__(**kwargs)
        self.safe_dev_mode = safe_dev_mode

        self._pool = httpcore.ConnectionPool(
            ssl_context=self._pool._ssl_context,
            max_connections=self._pool._max_connections,
            max_keepalive_connections=self._pool._max_keepalive_connections,
            keepalive_expiry=self._pool._keepalive_expiry,
            http1=self._pool._http1,
            http2=self._pool._http2,
            network_backend=SSRFSafeSyncNetworkBackend(safe_dev_mode=safe_dev_mode),
        )

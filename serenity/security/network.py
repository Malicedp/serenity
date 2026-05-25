"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse, unquote

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local v6
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped IPv6 (covers ::ffff:127.x.x.x etc.)
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

# DNS lookup timeout in seconds — bounds the blocking time during SSRF checks
_DNS_TIMEOUT = 3.0

_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking (e.g. Tailscale's 100.64.0.0/10)."""
    global _allowed_networks
    nets = []
    for cidr in cidrs:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            pass
    _allowed_networks = nets


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if _allowed_networks and any(addr in net for net in _allowed_networks):
        return False
    return any(addr in net for net in _BLOCKED_NETWORKS)


def _dns_lookup(hostname: str) -> list:
    """Perform a DNS lookup with a short timeout to bound blocking time."""
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(_DNS_TIMEOUT)
        return socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    finally:
        socket.setdefaulttimeout(old_timeout)


def validate_url_target(url: str) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    Decodes percent-encoded characters before checking so that
    http://127%2E0%2E0%2E1/ does not bypass the private-IP block.

    Returns (ok, error_message).  When ok is True, error_message is empty.
    """
    # Decode percent-encoding before any checks
    decoded_url = unquote(url)
    try:
        p = urlparse(decoded_url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    try:
        infos = _dns_lookup(hostname)
    except socket.gaierror:
        # DNS failure — treat as blocked (fail-safe) rather than allowing through
        return False, f"Cannot resolve hostname: {hostname}"

    for info in infos:
        try:
            raw_addr = ipaddress.ip_address(info[4][0])
            # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1 → 127.0.0.1) for clean matching
            addr = raw_addr.ipv4_mapped or raw_addr
        except ValueError:
            continue
        if _is_private(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    return True, ""


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate an already-fetched URL (e.g. after redirect). Only checks the IP, skips DNS."""
    decoded_url = unquote(url)
    try:
        p = urlparse(decoded_url)
    except Exception:
        return True, ""

    hostname = p.hostname
    if not hostname:
        return True, ""

    try:
        raw_addr = ipaddress.ip_address(hostname)
        addr = raw_addr.ipv4_mapped or raw_addr
        if _is_private(addr):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        # hostname is a domain name, resolve it
        try:
            infos = _dns_lookup(hostname)
        except socket.gaierror:
            # DNS failure on redirect — treat as blocked (fail-safe)
            return False, f"Cannot resolve redirect hostname: {hostname}"
        for info in infos:
            try:
                raw_addr = ipaddress.ip_address(info[4][0])
                addr = raw_addr.ipv4_mapped or raw_addr
            except ValueError:
                continue
            if _is_private(addr):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


def contains_internal_url(command: str) -> bool:
    """Return True if the command string contains a URL targeting an internal/private address."""
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url)
        if not ok:
            return True
    return False

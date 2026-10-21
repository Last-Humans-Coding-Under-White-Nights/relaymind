"""HTTP proxy policy for local inference and runtime health (RM01, RM18)."""

from ipaddress import ip_address

import httpx


def is_loopback_url(base_url: str) -> bool:
    """Recognize localhost and loopback literals without resolving hostnames."""
    host = httpx.URL(base_url).host
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def health_client(base_url: str) -> httpx.Client:
    """Build a runtime health client; callers own its context/lifetime."""
    return httpx.Client(base_url=base_url, timeout=2, trust_env=not is_loopback_url(base_url))

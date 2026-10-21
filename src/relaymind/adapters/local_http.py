"""HTTP proxy policy for local inference and runtime health (RM01, RM18)."""

from ipaddress import ip_address

import httpx


def is_loopback_host(host: str) -> bool:
    """Recognize localhost and loopback literals without resolving hostnames."""
    host = host.removeprefix("[").removesuffix("]")
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(base_url: str) -> bool:
    """Recognize a base URL whose host is localhost or a loopback literal."""
    return is_loopback_host(httpx.URL(base_url).host)


def health_client(base_url: str) -> httpx.Client:
    """Build a runtime health client; callers own its context/lifetime."""
    return httpx.Client(base_url=base_url, timeout=2, trust_env=not is_loopback_url(base_url))

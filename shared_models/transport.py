"""System proxy routing for model HTTP, without exposing proxy credentials."""
from ipaddress import ip_address
from urllib.parse import urlparse
from urllib.request import getproxies, proxy_bypass

import httpx


def client_options(url: str) -> dict:
    """Resolve a route once; loopback always stays direct, including on macOS."""
    try:
        target = urlparse(url)
        host = (target.hostname or "").lower().rstrip(".")
        if host == "localhost":
            return {"trust_env": False}
        try:
            if ip_address(host).is_loopback:
                return {"trust_env": False}
        except ValueError:
            pass
        if proxy_bypass(target.netloc):
            return {"trust_env": False}
        proxies = getproxies()
        proxy = proxies.get(target.scheme) or proxies.get("all")
        if not proxy:
            return {"trust_env": False}
        if not isinstance(proxy, str) or any(ord(c) <= 32 or ord(c) == 127 for c in proxy):
            raise ValueError()
        if "://" not in proxy:
            proxy = f"http://{proxy}"
        parsed = urlparse(proxy)
        if not parsed.hostname or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError()
        parsed.port
        checked = httpx.Proxy(proxy)
        return {"trust_env": False, "proxy": checked}
    except (OSError, ValueError, TypeError, UnicodeError, httpx.InvalidURL):
        raise httpx.ProxyError("Invalid system proxy configuration") from None


def create_client(client_type, **options):
    """HTTPX constructor errors must not expose a proxy URL or its credentials."""
    try:
        return client_type(**options)
    except (ValueError, ImportError, UnicodeError, httpx.InvalidURL):
        raise httpx.ProxyError("Could not initialize model HTTP transport; check proxy configuration and dependencies") from None

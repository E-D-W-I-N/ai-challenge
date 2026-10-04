"""Browser request metadata guard for local credential-bearing RAG actions."""
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def _origin(value: str) -> tuple[str, str, int] | None:
    # Origin is a serialized origin, never a credential-bearing URL. Reject
    # ambiguous encodings/controls rather than treating invalid input as absent.
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value) or "%" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        return parsed.scheme, host, port if port is not None else (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        return None


def trusted_rag_request(request: Request) -> None:
    """Allow same-origin UI/direct CLI; deny explicit cross-origin triggers."""
    sites = request.headers.getlist("sec-fetch-site")
    origins = request.headers.getlist("origin")
    if len(sites) > 1 or (sites and sites[0].lower() not in {"same-origin", "none"}):
        raise HTTPException(403, "Cross-origin RAG request denied")
    if origins:
        source = _origin(origins[0])
        target = _origin(f"{request.url.scheme}://{request.url.netloc}")
        if len(origins) != 1 or source is None or target is None or source != target:
            raise HTTPException(403, "Cross-origin RAG request denied")

"""Fetch a single public web page's readable text, with SSRF and size guards.

``fetch_html`` refuses non-``http(s)`` schemes and any host that resolves to a
non-public address (loopback/private/link-local/…), re-checking every redirect
hop, and streams under a byte cap. ``extract_web_text`` pulls the main article
text (and title) with trafilatura.
"""

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

_MAX_BYTES = 5 * 1024 * 1024
_TIMEOUT = 15.0
_MAX_HOPS = 5


class WebFetchError(Exception):
    """A URL could not be fetched (bad scheme, blocked host, HTTP error, too big)."""


def _assert_public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise WebFetchError(f"Could not resolve host {host!r}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise WebFetchError("Refusing to fetch a non-public address.")


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebFetchError("Only http and https URLs are supported.")
    if not parsed.hostname:
        raise WebFetchError("URL has no host.")
    _assert_public_host(parsed.hostname)


def fetch_html(url: str, *, transport: httpx.BaseTransport | None = None) -> str:
    """Fetch ``url`` and return its decoded HTML, enforcing all guards."""
    current = url
    with httpx.Client(
        timeout=_TIMEOUT, follow_redirects=False, transport=transport, trust_env=False
    ) as client:
        for _ in range(_MAX_HOPS + 1):
            _validate_url(current)
            request = client.build_request("GET", current)
            resp = client.send(request, stream=True)
            try:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise WebFetchError("Redirect without a location.")
                    current = str(httpx.URL(current).join(location))
                    continue
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise WebFetchError(f"Fetch failed: {exc}") from exc
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype and "text/" not in ctype:
                    raise WebFetchError(f"Unsupported content type: {ctype!r}.")
                total = 0
                parts: list[bytes] = []
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        raise WebFetchError("Response exceeds the size cap.")
                    parts.append(chunk)
                return b"".join(parts).decode(resp.encoding or "utf-8", errors="replace")
            finally:
                resp.close()
    raise WebFetchError("Too many redirects.")


def extract_web_text(html: str) -> tuple[str, str | None]:
    """Return ``(main_text, title_or_none)`` extracted from ``html``."""
    import trafilatura

    text = trafilatura.extract(html) or ""
    metadata = trafilatura.extract_metadata(html)
    title = metadata.title if metadata is not None else None
    return text, title

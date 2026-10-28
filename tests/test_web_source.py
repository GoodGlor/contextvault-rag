import httpx
import pytest

from contextvault.services.web_source import (
    WebFetchError,
    extract_web_text,
    fetch_html,
)


def test_fetch_rejects_non_http_scheme() -> None:
    with pytest.raises(WebFetchError, match="http"):
        fetch_html("file:///etc/passwd")


def test_fetch_rejects_private_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "contextvault.services.web_source.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )
    with pytest.raises(WebFetchError, match="non-public"):
        fetch_html("http://localhost/")


def test_fetch_returns_body_for_public_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "contextvault.services.web_source.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html><body>Hi</body></html>"
        )
    )
    body = fetch_html("http://example.com/", transport=transport)
    assert "Hi" in body


def test_fetch_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "contextvault.services.web_source.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    monkeypatch.setattr("contextvault.services.web_source._MAX_BYTES", 8)
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/html"}, text="x" * 100)
    )
    with pytest.raises(WebFetchError, match="size cap"):
        fetch_html("http://example.com/", transport=transport)


def test_extract_web_text_pulls_main_content_and_title() -> None:
    html = (
        "<html><head><title>My Page</title></head>"
        "<body><article><p>The important sentence here.</p></article></body></html>"
    )
    text, title = extract_web_text(html)
    assert "important sentence" in text
    assert title == "My Page"

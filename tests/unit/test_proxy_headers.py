"""Tests for proxy request/response header filtering."""

from __future__ import annotations

import uuid

import pytest

from vscode_gateway.errors import GatewayError
from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry


def _multi(headers: dict[str, str]):
    class _M:
        def items(self):
            return list(headers.items())

    return _M()


def test_hop_by_hop_headers_filtered() -> None:
    registry = ProxyRegistry()
    adapter = ProxyAdapter(registry, None)  # type: ignore[arg-type]

    headers = {
        "host": "example.com",
        "connection": "keep-alive",
        "keep-alive": "timeout=5",
        "transfer-encoding": "chunked",
        "upgrade": "websocket",
        "x-custom": "value",
        "te": "trailers",
        "trailer": "x-checksum",
        "proxy-authenticate": "basic",
        "proxy-authorization": "bearer xyz",
    }

    filtered = adapter.filter_request_headers(_multi(headers))
    keys = {k.lower() for k, _ in filtered}
    assert "host" in keys
    assert "x-custom" in keys
    assert "connection" not in keys
    assert "keep-alive" not in keys
    assert "transfer-encoding" not in keys
    assert "upgrade" not in keys
    assert "te" not in keys
    assert "trailer" not in keys
    assert "proxy-authenticate" not in keys
    assert "proxy-authorization" not in keys


def test_gateway_cookie_stripped_from_cookie_header() -> None:
    registry = ProxyRegistry()
    adapter = ProxyAdapter(registry, None)  # type: ignore[arg-type]

    headers = {
        "cookie": ("gateway_session=secret; openvscode_session=abc; other=xyz"),
    }

    filtered = adapter.filter_request_headers(_multi(headers))
    cookie_values = [v for k, v in filtered if k.lower() == "cookie"]
    assert len(cookie_values) == 1
    cookie_val = cookie_values[0]
    assert "gateway_session=secret" not in cookie_val
    assert "openvscode_session=abc" in cookie_val
    assert "other=xyz" in cookie_val


def test_gateway_csrf_header_dropped() -> None:
    registry = ProxyRegistry()
    adapter = ProxyAdapter(registry, None)  # type: ignore[arg-type]

    headers = {"x-csrf-token": "abc", "x-custom": "value"}
    filtered = adapter.filter_request_headers(_multi(headers))
    keys = {k.lower() for k, _ in filtered}
    assert "x-csrf-token" not in keys
    assert "x-custom" in keys


def test_downstream_content_length_dropped() -> None:
    """Content-Length must not be forwarded because httpx recomputes it
    (or declares chunked encoding) based on the streamed body."""
    registry = ProxyRegistry()
    adapter = ProxyAdapter(registry, None)  # type: ignore[arg-type]

    headers = {"content-length": "1234"}
    filtered = adapter.filter_request_headers(_multi(headers))
    keys = {k.lower() for k, _ in filtered}
    assert "content-length" not in keys


def test_filter_upstream_response_raw_headers_strips_hop_by_hop() -> None:
    registry = ProxyRegistry()
    adapter = ProxyAdapter(registry, None)  # type: ignore[arg-type]

    raw = [
        (b"Content-Type", b"text/plain"),
        (b"Connection", b"keep-alive"),
        (b"Transfer-Encoding", b"chunked"),
        (b"Set-Cookie", b"foo=bar"),
        (b"Set-Cookie", b"baz=qux"),
    ]
    filtered = adapter.filter_upstream_response_raw_headers(raw)
    names = {n.lower() for n, _ in filtered}
    assert b"connection" not in names
    assert b"transfer-encoding" not in names
    set_cookies = [v for n, v in filtered if n.lower() == b"set-cookie"]
    assert set_cookies == [b"foo=bar", b"baz=qux"]


def test_proxy_registry_add_get_remove() -> None:
    registry = ProxyRegistry()
    sid = uuid.uuid4()

    registry.add(sid, 9001)
    assert registry.get(sid) == 9001

    with pytest.raises(GatewayError):
        registry.add(sid, 9002)

    registry.remove(sid)
    with pytest.raises(GatewayError):
        registry.get(sid)

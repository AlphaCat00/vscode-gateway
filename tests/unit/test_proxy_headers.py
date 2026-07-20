"""Tests for proxy header filtering."""

from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry


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

    filtered = adapter.filter_request_headers(headers)
    assert "host" in filtered
    assert "x-custom" in filtered
    assert "connection" not in filtered
    assert "keep-alive" not in filtered
    assert "transfer-encoding" not in filtered
    assert "upgrade" not in filtered
    assert "te" not in filtered
    assert "trailer" not in filtered
    assert "proxy-authenticate" not in filtered
    assert "proxy-authorization" not in filtered


def test_proxy_registry_add_get_remove() -> None:
    import uuid

    import pytest

    from vscode_gateway.errors import GatewayError

    registry = ProxyRegistry()
    sid = uuid.uuid4()

    registry.add(sid, 9001)
    assert registry.get(sid) == 9001

    # Duplicate registration should fail
    with pytest.raises(GatewayError):
        registry.add(sid, 9002)

    registry.remove(sid)
    with pytest.raises(GatewayError):
        registry.get(sid)

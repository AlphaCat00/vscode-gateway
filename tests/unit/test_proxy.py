"""Unit tests for bidirectional HTTP proxy streaming (HI-06)."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from starlette.requests import Request

from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_request(
    method: str = "GET",
    path: str = "/editor/00000000-0000-0000-0000-000000000000/",
    headers: dict[str, str] | None = None,
    body_chunks: list[bytes] | None = None,
) -> Request:
    raw_headers: list[tuple[bytes, bytes]] = []
    for k, v in (headers or {}).items():
        raw_headers.append((k.encode("latin-1"), v.encode("latin-1")))

    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    if body_chunks is None:
        body_chunks = []

    sent: list[int] = [0]

    async def receive() -> dict[str, Any]:
        if sent[0] >= len(body_chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        idx = sent[0]
        sent[0] += 1
        more = sent[0] < len(body_chunks)
        return {"type": "http.request", "body": body_chunks[idx], "more_body": more}

    return Request(scope, receive)


async def _drain_request_stream(stream_attr: Any) -> list[bytes]:
    """Iterate an httpx Request.stream (typed as a sync/async union)
    chunk-by-chunk, dropping Starlette's trailing empty terminator."""
    chunks: list[bytes] = []
    async for chunk in stream_attr:  # type: ignore[union-attr]
        if chunk:
            chunks.append(bytes(chunk))
    return chunks


def _await_response_background(response: Any) -> Any:
    bg = response.background
    return bg() if bg is not None else _noop()


async def _noop() -> None:
    return None


async def _consume_body(response: Any) -> list[bytes]:
    """Drain a Starlette StreamingResponse body iterator byte-by-byte."""
    iterator: AsyncIterator[Any] = cast(AsyncIterator[Any], response.body_iterator)
    out: list[bytes] = []
    async for chunk in iterator:
        out.append(_to_bytes(chunk))
    return out


async def _first_chunk(response: Any, timeout: float = 2.0) -> bytes:
    iterator: AsyncIterator[Any] = cast(AsyncIterator[Any], response.body_iterator)
    first = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
    return _to_bytes(first)


def _to_bytes(chunk: Any) -> bytes:
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, str):
        return chunk.encode("utf-8")
    if isinstance(chunk, memoryview):
        return chunk.tobytes()
    return bytes(chunk)


class _RecordingTransport(httpx.AsyncBaseTransport):
    """A custom transport that reads the request body stream iteratively
    (rather than pre-buffering it via ``request.aread()``) and records
    each chunk in arrival order, then returns a configurable response.
    """

    def __init__(
        self,
        *,
        response_body: bytes = b"OK",
        response_stream: Any = None,
        response_headers: list[tuple[str, str]] | None = None,
        status_code: int = 200,
    ) -> None:
        self._response_body = response_body
        self._response_stream = response_stream
        self._response_headers = response_headers or []
        self._status_code = status_code
        self.received_chunks: list[bytes] = []
        self.request_stream_class = ""
        self.received_request: httpx.Request | None = None
        self.received_method = ""
        self.received_url = ""
        self.received_headers: dict[str, str] = {}
        self.transfer_encoding: str | None = None
        self.content_length: str | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.received_request = request
        self.received_method = request.method
        self.received_url = str(request.url)
        self.request_stream_class = type(request.stream).__name__
        self.received_headers = dict(request.headers.multi_items())
        self.transfer_encoding = self.received_headers.get("transfer-encoding")
        self.content_length = self.received_headers.get("content-length")
        # Drain the request body chunk-by-chunk so we can verify the proxy
        # did not buffer the entire body before forwarding. We drop the
        # trailing empty bytes Starlette appends as a body terminator since
        # chunked encoding collapses it (it signals end-of-body, not a real
        # payload chunk).
        async for chunk in _aiter_async_stream(request.stream):
            if chunk:
                self.received_chunks.append(chunk)
        if self._response_stream is not None:
            return httpx.Response(self._status_code, content=self._response_stream)
        return httpx.Response(
            self._status_code,
            headers=self._response_headers,
            content=self._response_body,
        )


async def _aiter_async_stream(stream_attr: Any) -> AsyncIterator[bytes]:
    """Bridge ``httpx.Request.stream`` (typed as a sync/async union in
    stubs) to an async iterator. At runtime an ``AsyncIteratorByteStream``
    always supports ``__aiter__``."""
    async for chunk in stream_attr:  # type: ignore[union-attr]
        if isinstance(chunk, bytes):
            yield chunk
        else:
            yield bytes(chunk)


def _make_adapter(transport: httpx.AsyncBaseTransport) -> tuple[ProxyAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
    )
    registry = ProxyRegistry()
    sid = uuid.uuid4()
    registry.add(sid, 9123)
    adapter = ProxyAdapter(registry, client, session_service=None)
    return adapter, client


def _make_registered_session(adapter: ProxyAdapter) -> uuid.UUID:
    targets: dict[uuid.UUID, int] = adapter._registry._targets  # type: ignore[attr-defined]
    for sid, _port in targets.items():
        return sid
    raise AssertionError("no session registered")


# ---------------------------------------------------------------------------
# trust_env / follow_redirects
# ---------------------------------------------------------------------------


def test_client_has_trust_env_false_constructor_arg() -> None:
    """Asserting ``trust_env=False``/``follow_redirects=False`` directly
    on a freshly constructed client keeps this test independent of
    lifespan wiring."""
    client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    assert client.trust_env is False
    assert client.follow_redirects is False
    asyncio.run(client.aclose())


def test_app_lifespan_builds_trust_env_false_client(tmp_path: Path) -> None:
    """The lifespan-mounted ``http_client`` must have ``trust_env=False``
    so production traffic cannot be redirected by ``HTTP_PROXY`` etc."""
    from fastapi import FastAPI

    from vscode_gateway.app import lifespan
    from vscode_gateway.readiness import Readiness
    from vscode_gateway.settings import Settings

    settings_kwargs: dict[str, Any] = {
        "state_dir": tmp_path / "state",
        "runtime_dir": tmp_path / "runtime",
        "ssh_config_path": tmp_path / "ssh_config",
        "ssh_keys_dir": tmp_path / "keys",
        "password_hash_path": tmp_path / "state" / "password.hash",
        "session_secret_path": tmp_path / "state" / "session.secret",
    }
    settings = Settings(**settings_kwargs)  # type: ignore[arg-type]
    app = FastAPI()
    app.state.settings = settings
    app.state.readiness = Readiness()

    async def _drive() -> None:
        async with lifespan(app):
            client: httpx.AsyncClient | None = getattr(app.state, "http_client", None)
            assert client is not None
            assert client.trust_env is False
            assert client.follow_redirects is False

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Streaming upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_upload_forwarded_chunk_by_chunk() -> None:
    """A large request body is forwarded to the upstream chunk-by-chunk,
    in order, without the proxy materializing the entire body in memory.

    We assert via the custom transport that:

      * ``request.stream`` is an ``AsyncIteratorByteStream`` (not a
        ``ByteStream``), proving the proxy did not pre-buffer the body
        before passing it to httpx.
      * the chunks received by the upstream equal the input chunks in
        order.
      * the upstream saw ``Transfer-Encoding: chunked`` and no
        ``Content-Length`` (httpx emits chunked encoding because the
        generator length is unknown).
    """
    transport = _RecordingTransport()
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)
    # 8 MiB total body split into 8 chunks — far beyond any reasonable
    # in-memory cap, and not all materialized at once on the proxy.
    chunks = [b"a" * (1024 * 1024) for _ in range(8)]

    request = _build_request(
        method="POST",
        path=f"/editor/{sid}/some/path",
        headers={"content-type": "application/octet-stream"},
        body_chunks=chunks,
    )

    response = await adapter.proxy_http(sid, request)
    # Drain the response so the upstream request body is fully sent.
    await _consume_body(response)
    await _await_response_background(response)

    assert transport.received_method == "POST"
    assert transport.request_stream_class == "AsyncIteratorByteStream"
    assert transport.transfer_encoding == "chunked"
    assert transport.content_length is None
    assert transport.received_chunks == chunks
    assert b"".join(transport.received_chunks) == b"a" * (1024 * 1024 * 8)

    await client.aclose()


@pytest.mark.asyncio
async def test_streaming_upload_byte_for_byte_order() -> None:
    """Distinct chunks retain order — proves the proxy did not rearrange
    or coalesce body chunks."""
    transport = _RecordingTransport()
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)

    chunks = [b"alpha", b"beta", b"gamma", b"delta"]
    request = _build_request(
        method="POST",
        path=f"/editor/{sid}/upload",
        headers={"content-type": "application/octet-stream"},
        body_chunks=chunks,
    )

    response = await adapter.proxy_http(sid, request)
    await _consume_body(response)
    await _await_response_background(response)

    assert transport.received_chunks == chunks

    await client.aclose()


# ---------------------------------------------------------------------------
# Streaming download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_download_first_chunk_before_upstream_finishes() -> None:
    """A large upstream response is streamed to the client: the first
    chunk reaches the client BEFORE the upstream body has finished."""

    upstream_second_chunk_gate = asyncio.Event()
    upstream_first_chunk_sent = asyncio.Event()
    client_received_first = asyncio.Event()

    async def upstream_body() -> AsyncIterator[bytes]:
        upstream_first_chunk_sent.set()
        yield b"chunk-1"
        # Hand back control, then wait for the client to consume the
        # first chunk before producing the second. If the proxy buffered
        # the upstream body the client would never see chunk-1 until
        # both chunks were produced.
        client_received_first.set()
        await upstream_second_chunk_gate.wait()
        yield b"chunk-2"

    transport = _RecordingTransport(response_stream=upstream_body())
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)

    request = _build_request(method="GET", path=f"/editor/{sid}/download")

    response = await adapter.proxy_http(sid, request)

    # Pull the first chunk; this should NOT block on the upstream
    # generator producing the second chunk.
    first_chunk = await _first_chunk(response)
    assert first_chunk == b"chunk-1"
    assert upstream_first_chunk_sent.is_set()
    # Upstream must still be waiting on the gate we control.
    assert not upstream_second_chunk_gate.is_set()

    # Let the upstream body produce its second chunk and drain.
    client_received_first.set()
    upstream_second_chunk_gate.set()
    remaining = await _consume_body(response)
    assert b"".join(remaining) == b"chunk-2"

    await _await_response_background(response)
    await client.aclose()


# ---------------------------------------------------------------------------
# Client disconnect closes the upstream response
# ---------------------------------------------------------------------------


class _UpstreamResponseWithCloseSpy(httpx.Response):
    def __init__(self, content_body: bytes) -> None:
        super().__init__(200, content=content_body)
        self.aclose_calls = 0

    async def aclose(self) -> None:  # type: ignore[override]
        self.aclose_calls += 1
        await super().aclose()


@pytest.mark.asyncio
async def test_client_disconnect_triggers_upstream_aclose_via_background() -> None:
    """If the downstream client disconnects mid-stream, the background
    finalizer on the StreamingResponse must close the upstream
    response, releasing the connection back to the pool."""

    upstream = _UpstreamResponseWithCloseSpy(b"never-streamed")

    class _StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            async for _ in _aiter_async_stream(request.stream):
                pass
            return upstream

    transport = _StubTransport()
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)

    request = _build_request(method="GET", path=f"/editor/{sid}/blob")
    response = await adapter.proxy_http(sid, request)

    # Simulate a client disconnect by NOT iterating the body iterator
    # to completion, then invoking the background finalizer the way
    # Starlette would after an OSError on send.
    iterator = response.body_iterator
    _ = await iterator.__anext__()  # type: ignore[union-attr]
    assert response.background is not None
    await response.background()
    assert upstream.aclose_calls >= 1

    await client.aclose()


@pytest.mark.asyncio
async def test_normal_completion_releases_upstream_via_background() -> None:
    """A completed stream still triggers the upstream close."""

    upstream = _UpstreamResponseWithCloseSpy(b"hello")

    class _StubTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            async for _ in _aiter_async_stream(request.stream):
                pass
            return upstream

    transport = _StubTransport()
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)

    request = _build_request(method="GET", path=f"/editor/{sid}/file")
    response = await adapter.proxy_http(sid, request)

    out = await _consume_body(response)
    assert b"".join(out) == b"hello"
    await _await_response_background(response)
    assert upstream.aclose_calls >= 1

    await client.aclose()


# ---------------------------------------------------------------------------
# Multi-value response headers are preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_value_response_headers_preserved() -> None:
    """Repeated upstream response headers (e.g. two ``Set-Cookie``) must
    survive intact instead of being collapsed by a dict conversion."""
    transport = _RecordingTransport(
        response_headers=[
            ("Content-Type", "text/plain"),
            ("Set-Cookie", "editor=a; Path=/"),
            ("Set-Cookie", "tracking=b; Path=/"),
        ],
        response_body=b"hello",
    )
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)

    request = _build_request(method="GET", path=f"/editor/{sid}/")
    response = await adapter.proxy_http(sid, request)

    set_cookie_pairs = [
        (n.decode("latin-1"), v.decode("latin-1"))
        for n, v in response.raw_headers
        if n.lower() == b"set-cookie"
    ]
    assert len(set_cookie_pairs) == 2
    values = [v for _, v in set_cookie_pairs]
    assert values[0] == "editor=a; Path=/"
    assert values[1] == "tracking=b; Path=/"

    await _consume_body(response)
    await _await_response_background(response)
    await client.aclose()


# ---------------------------------------------------------------------------
# Gateway session cookie stripped from upstream Cookie header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_cookie_stripped_from_upstream_cookie_header() -> None:
    """The proxy must not forward the gateway's signed session cookie
    to the upstream editor; other cookies are preserved."""
    transport = _RecordingTransport()
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)

    request = _build_request(
        method="GET",
        path=f"/editor/{sid}/",
        headers={
            "cookie": (
                "gateway_session=signed-secret; openvscode_session=editor-token; theme=dark"
            ),
            "x-csrf-token": "abcdef",
        },
    )

    response = await adapter.proxy_http(sid, request)
    await _consume_body(response)
    await _await_response_background(response)

    cookie_to_upstream = transport.received_headers.get("cookie", "")
    assert "gateway_session=signed-secret" not in cookie_to_upstream
    assert "openvscode_session=editor-token" in cookie_to_upstream
    assert "theme=dark" in cookie_to_upstream
    # Gateway CSRF header must not be forwarded.
    assert "x-csrf-token" not in {k.lower() for k in transport.received_headers}

    await client.aclose()


@pytest.mark.asyncio
async def test_cookie_header_dropped_when_only_gateway_cookie_present() -> None:
    """If the Cookie header would only contain the gateway cookie after
    filtering, the entire ``Cookie`` header must be omitted upstream."""
    transport = _RecordingTransport()
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)

    request = _build_request(
        method="GET",
        path=f"/editor/{sid}/",
        headers={"cookie": "gateway_session=secret"},
    )

    response = await adapter.proxy_http(sid, request)
    await _consume_body(response)
    await _await_response_background(response)

    assert "cookie" not in {k.lower() for k in transport.received_headers}

    await client.aclose()


# ---------------------------------------------------------------------------
# Forwarding headers / follow_redirects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forwarding_headers_added() -> None:
    """The proxy adds X-Forwarded-* headers and does not follow upstream
    redirects (the client is constructed with follow_redirects=False)."""
    transport = _RecordingTransport(response_body=b"ok")
    adapter, client = _make_adapter(transport)
    sid = _make_registered_session(adapter)

    request = _build_request(
        method="GET",
        path=f"/editor/{sid}/path/",
        headers={"host": "gw.example"},
    )

    response = await adapter.proxy_http(sid, request)
    await _consume_body(response)
    await _await_response_background(response)

    h = transport.received_headers
    assert h.get("x-forwarded-proto") == "http"
    assert h.get("x-forwarded-host") == "gw.example"
    assert h.get("x-forwarded-prefix") == f"/editor/{sid}"

    await client.aclose()

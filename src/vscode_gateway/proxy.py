from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket

from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import SessionId

HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Gateway-specific request headers that must never be forwarded
# upstream. The editor never needs them and forwarding them would leak
# gateway auth/plumbing material (Plan §16.2).
GATEWAY_REQUEST_HEADER_BLOCKLIST: frozenset[str] = frozenset(
    {
        "x-csrf-token",
    }
)

# Gateway session cookie name(s). Upstream editor cookies are preserved;
# only the gateway's own auth cookie is stripped (Plan §16.2).
GATEWAY_COOKIE_NAMES: frozenset[str] = frozenset({"gateway_session"})


class ProxyRegistry:
    def __init__(self) -> None:
        self._targets: dict[SessionId, int] = {}
        self._lock = asyncio.Lock()

    def add(self, session_id: SessionId, local_port: int) -> None:
        if session_id in self._targets:
            raise GatewayError(
                ErrorCode.CONFLICT,
                "Session already registered in proxy",
                status_code=409,
            )
        self._targets[session_id] = local_port

    def remove(self, session_id: SessionId) -> None:
        self._targets.pop(session_id, None)

    def get(self, session_id: SessionId) -> int:
        port = self._targets.get(session_id)
        if port is None:
            raise GatewayError(
                ErrorCode.ALIAS_NOT_FOUND,
                "Session not found in proxy registry",
                status_code=404,
            )
        return port

    def lookup(self, session_id: SessionId) -> int | None:
        return self._targets.get(session_id)


class ProxyAdapter:
    def __init__(
        self,
        registry: ProxyRegistry,
        http_client: httpx.AsyncClient,
        session_service: Any = None,
    ) -> None:
        self._registry = registry
        self._http = http_client
        self._session_service = session_service

    def parse_session_id(self, session_id_str: str) -> SessionId:
        try:
            return uuid.UUID(session_id_str)
        except (ValueError, TypeError) as exc:
            raise GatewayError(
                ErrorCode.ALIAS_NOT_FOUND,
                "Invalid session ID",
                status_code=404,
            ) from exc

    def get_upstream_url(self, session_id: SessionId, path: str, query: str = "") -> str:
        port = self._registry.get(session_id)
        url = f"http://127.0.0.1:{port}{path}"
        if query:
            url = f"{url}?{query}"
        return url

    def filter_request_headers(
        self,
        headers: Any,
    ) -> list[tuple[str, str]]:
        """Return multi-value request headers safe to forward upstream.

        Drops hop-by-hop headers, gateway-only headers, and the gateway
        auth cookie from the ``Cookie`` header. Other cookies (editor
        cookies etc.) are preserved (Plan §16.2).
        """
        out: list[tuple[str, str]] = []
        # Starlette's ``Headers.items()`` returns each repeated header as
        # its own ``(name, value)`` pair (multi-value aware) so the
        # downstream `Cookie` plus other repeated headers are preserved.
        for key, value in headers.items():
            lk = key.lower()
            if lk in HOP_BY_HOP_HEADERS:
                continue
            if lk in GATEWAY_REQUEST_HEADER_BLOCKLIST:
                continue
            # httpx recomputes Content-Length or uses chunked encoding
            # based on the streamed request body; the downstream value
            # could conflict with the chosen transfer encoding and must
            # not be forwarded.
            if lk == "content-length":
                continue
            if lk == "cookie":
                value = self._strip_gateway_cookies(value)
                if not value:
                    continue
            out.append((key, value))
        return out

    @staticmethod
    def _strip_gateway_cookies(cookie_header: str) -> str:
        parts: list[str] = []
        for part in cookie_header.split(";"):
            part = part.strip()
            if not part:
                continue
            name = part.split("=", 1)[0].strip()
            if name in GATEWAY_COOKIE_NAMES:
                continue
            parts.append(part)
        return "; ".join(parts)

    @staticmethod
    def filter_upstream_response_raw_headers(
        raw_headers: list[tuple[bytes, bytes]],
    ) -> list[tuple[bytes, bytes]]:
        """Drop hop-by-hop upstream response headers, preserving repeated
        headers (e.g. several ``Set-Cookie``) by working on the raw
        ``(name, value)`` byte-pair list rather than collapsing into a
        dict.
        """
        return [
            (name, value)
            for name, value in raw_headers
            if name.decode("latin-1").lower() not in HOP_BY_HOP_HEADERS
        ]

    @staticmethod
    def _strip_base_trailing_slash(session_id: SessionId, path: str) -> str:
        # OpenVSCode's ``--server-base-path /editor/{id}`` matches the editor
        # root at the exact base path WITHOUT a trailing slash; a request to
        # ``/editor/{id}/`` would otherwise yield an upstream 404. The gateway
        # route is ``/editor/{id}/{rest_of_path:path}`` and the dashboard links
        # use the trailing-slash form, so normalize it here before forwarding.
        base = f"/editor/{session_id}"
        if path == f"{base}/":
            return base
        return path

    async def proxy_http(
        self,
        session_id: SessionId,
        request: Request,
    ) -> StreamingResponse:
        port = self._registry.get(session_id)
        path = self._strip_base_trailing_slash(session_id, request.url.path)
        query = request.url.query

        upstream_url = f"http://127.0.0.1:{port}{path}"
        if query:
            upstream_url = f"{upstream_url}?{query}"

        method = request.method or "GET"

        forwarded = self.filter_request_headers(request.headers)
        forwarded.append(("x-forwarded-proto", request.url.scheme))
        forwarded.append(("x-forwarded-host", request.headers.get("host", "")))
        forwarded.append(("x-forwarded-prefix", f"/editor/{session_id}"))

        # Stream the downstream request body straight into the upstream
        # request (Plan §16.2). httpx wraps the async generator in an
        # ``AsyncIteratorByteStream`` and emits a chunked
        # ``Transfer-Encoding`` header because the total length is
        # unknown, so large uploads are forwarded without an in-memory
        # copy on the gateway side.
        req = self._http.build_request(
            method=method,
            url=upstream_url,
            headers=forwarded,
            content=request.stream(),
        )

        try:
            upstream_resp = await self._http.send(req, stream=True)
        except httpx.RequestError as exc:
            raise GatewayError(
                ErrorCode.EDITOR_UNHEALTHY,
                f"Proxy request failed: {exc}",
                status_code=502,
            ) from exc

        # ``raw`` is the original ordered list of (name, value) byte
        # pairs from the upstream response; using it preserves repeated
        # headers (e.g. multiple ``Set-Cookie``) instead of collapsing
        # them into a dict.
        raw_response_headers = self.filter_upstream_response_raw_headers(
            list(upstream_resp.headers.raw)
        )
        content_type = upstream_resp.headers.get("content-type")

        # A guaranteed background finalizer releases the upstream
        # connection back to the pool even if the downstream client
        # disconnects mid-stream before the body iterator is fully
        # consumed (Plan §16.2; HI-06). ``aclose`` is idempotent in
        # httpx, so the belt-and-suspenders close inside the body
        # iterator (for graceful completion) is safe alongside this.
        background = BackgroundTask(_aclose_response, upstream_resp)

        response = StreamingResponse(
            content=_iter_upstream(upstream_resp),
            status_code=upstream_resp.status_code,
            media_type=content_type,
            background=background,
        )
        # Preserve raw multi-value upstream headers verbatim. Starlette's
        # ``init_headers`` collapses a mapping's ``.items()`` so repeated
        # headers (multiple ``Set-Cookie``) would be lost; assigning the
        # raw list directly keeps them intact.
        response.raw_headers = raw_response_headers
        return response

    async def proxy_websocket(
        self,
        session_id: SessionId,
        downstream: WebSocket,
    ) -> None:
        port = self._registry.get(session_id)
        path = self._strip_base_trailing_slash(session_id, downstream.url.path)
        query = downstream.url.query

        ws_url = f"ws://127.0.0.1:{port}{path}"
        if query:
            ws_url = f"{ws_url}?{query}"

        from websockets.asyncio.client import connect as ws_connect
        from websockets.typing import Subprotocol

        # Starlette's downstream.headers is a Mapping; pull the
        # sec-websocket-protocol header safely.
        proto_header = downstream.headers.get("sec-websocket-protocol", "")
        subprotocols: list[Subprotocol] = [
            Subprotocol(p.strip()) for p in proto_header.split(",") if p.strip()
        ]

        try:
            upstream = await ws_connect(
                ws_url,
                subprotocols=subprotocols or None,
                max_size=2**20,
            )
        except Exception:
            await downstream.close(code=1011, reason="Failed to connect to upstream")
            return

        # The upstream's `.response` attribute exists once a handshake has
        # completed; guard for the cases where it does not.
        upstream_response = getattr(upstream, "response", None)
        upstream_headers: list[tuple[bytes, bytes]] = []
        if upstream_response is not None:
            for key, value in upstream_response.headers.items():
                lk = key.lower()
                # Starlette's ``accept()`` produces its own WebSocket handshake
                # headers (Upgrade, Connection, Sec-WebSocket-Accept, ...).
                # Forwarding the upstream's copies would result in duplicates
                # ("'Upgrade' header must not appear more than once"), so drop
                # all hop-by-hop and ``sec-websocket-*`` headers here and let
                # Starlette own the downstream handshake. We only forward
                # end-to-end headers (e.g. ``Set-Cookie``) added by upstream.
                if lk in HOP_BY_HOP_HEADERS or lk.startswith("sec-websocket-"):
                    continue
                upstream_headers.append((key.encode("latin-1"), value.encode("latin-1")))

        await downstream.accept(
            subprotocol=upstream.subprotocol,
            headers=upstream_headers,
        )

        # Increment presence only after authorization and successful
        # upstream connection + downstream accept (plan §16.3 step 8).
        # The matching decrement is performed by the route handler in its
        # ``finally`` block, exactly once.
        if self._session_service is not None:
            self._session_service.on_client_connected(session_id)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._relay_downstream_to_upstream(downstream, upstream))
                tg.create_task(self._relay_upstream_to_downstream(upstream, downstream))
        except Exception:
            pass
        finally:
            with suppress(Exception):
                await upstream.close()

    async def _relay_downstream_to_upstream(
        self,
        downstream: WebSocket,
        upstream: Any,
    ) -> None:

        try:
            while True:
                message = await downstream.receive()
                if message["type"] == "websocket.receive":
                    if "text" in message:
                        await upstream.send(message["text"])
                    elif "bytes" in message:
                        await upstream.send(message["bytes"])
                elif message["type"] == "websocket.disconnect":
                    await upstream.close(
                        code=message.get("code", 1000),
                        reason=message.get("reason", ""),
                    )
                    break
        except Exception:
            pass

    async def _relay_upstream_to_downstream(
        self,
        upstream: Any,
        downstream: WebSocket,
    ) -> None:
        try:
            async for message in upstream:
                if isinstance(message, str):
                    await downstream.send_text(message)
                else:
                    await downstream.send_bytes(message)
        except Exception:
            pass


async def _iter_upstream(resp: httpx.Response) -> AsyncIterator[bytes]:
    """Yield upstream response bytes lazily, releasing the upstream on
    graceful exhaustion (Plan §16.2; HI-06)."""
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    finally:
        with suppress(Exception):
            await resp.aclose()


async def _aclose_response(resp: httpx.Response) -> None:
    """Background finalizer that returns the upstream connection to the
    pool even when the downstream client disconnects mid-stream."""
    with suppress(Exception):
        await resp.aclose()

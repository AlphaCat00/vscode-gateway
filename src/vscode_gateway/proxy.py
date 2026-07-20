from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import Any

import httpx
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

    def filter_request_headers(self, headers: dict[str, str]) -> dict[str, str]:
        return {
            k: v
            for k, v in headers.items()
            if k.lower() not in HOP_BY_HOP_HEADERS and k.lower() != "cookie"
        }

    def filter_response_headers(self, headers: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}

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

        headers = self.filter_request_headers(dict(request.headers))
        headers["x-forwarded-proto"] = request.url.scheme
        headers["x-forwarded-host"] = request.headers.get("host", "")
        path_prefix = f"/editor/{session_id}"
        headers["x-forwarded-prefix"] = path_prefix

        body = await request.body()
        content = body if body else None

        try:
            upstream_resp = await self._http.request(
                method=method,
                url=upstream_url,
                headers=headers,
                content=content,
                timeout=httpx.Timeout(
                    connect=self._http.timeout.connect,
                    read=self._http.timeout.read,
                    write=self._http.timeout.write,
                    pool=self._http.timeout.pool,
                ),
            )
        except httpx.RequestError as exc:
            raise GatewayError(
                ErrorCode.EDITOR_UNHEALTHY,
                f"Proxy request failed: {exc}",
                status_code=502,
            ) from exc

        response_headers = self.filter_response_headers(dict(upstream_resp.headers))

        return StreamingResponse(
            content=upstream_resp.aiter_bytes(),
            status_code=upstream_resp.status_code,
            headers=response_headers,
            media_type=response_headers.get("content-type"),
        )

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

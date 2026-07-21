from __future__ import annotations

import uuid
from contextlib import suppress
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.responses import Response
from starlette.websockets import WebSocket

from vscode_gateway.auth import (
    LoginThrottle,
    clear_session,
    create_session,
    get_csrf_token,
    is_authenticated,
    load_password_hash_from_file,
    session_generation_matches,
    verify_csrf,
    verify_password,
)
from vscode_gateway.db import get_session, get_session_by_alias
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.models import (
    SSH_KEY_TYPES,
    CatalogResponse,
    HostKeyChallenge,
    KeyUploadResponse,
    SessionState,
    SshConfigResponse,
    SshConfigUpdateRequest,
    SshKeyInventory,
    SshKeySlot,
    SshKeyType,
    TrustHostKeyRequest,
    VersionResponse,
)
from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry
from vscode_gateway.readiness import Readiness, ReadinessPhase
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import (
    SshCatalog,
    compute_config_revision,
    validate_and_save_config,
)
from vscode_gateway.ssh_keys import SshKeyService


def _debug_headers(request: Request) -> dict[str, str]:
    return {"x-debug-url-path": request.url.path}


# --- Dependencies ---
async def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    settings: Settings = request.app.state.settings
    if not session_generation_matches(request, settings):
        request.session.clear()
        raise HTTPException(status_code=401, detail="Session no longer valid")


async def require_csrf(request: Request) -> None:
    if not verify_csrf(request):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _require_ready(request: Request) -> None:
    """Reject mutations and editor traffic while the gateway is not
    ``ready``. Read-only routes (dashboard, lists, /healthz, the
    top-level /readyz) bypass this check.

    The check returns 503 with a problem+json body so load balancers
    stop routing mutations and editors during startup and degraded
    recovery.
    """
    try:
        readiness: Readiness = request.app.state.readiness
    except AttributeError:
        raise HTTPException(status_code=503, detail="Service is not ready") from None
    if readiness.phase != ReadinessPhase.READY:
        state = readiness.snapshot()
        body = state.as_response_dict()
        raise HTTPException(
            status_code=503,
            detail=body.get("reason") or "Service is not ready",
        )


async def require_ready(request: Request) -> None:
    _require_ready(request)


def _problem(exc: GatewayError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": f"urn:vscode-gateway:error:{exc.code.value}",
            "title": exc.safe_message or exc.code.value.replace("_", " ").title(),
            "status": exc.status_code,
            "detail": exc.detail or exc.safe_message,
            "code": exc.code.value,
            "requestId": request_id,
        },
        media_type="application/problem+json",
    )


def _host_key_payload(challenge: HostKeyChallenge) -> dict[str, str | int]:
    return {
        "role": challenge.role,
        "host": challenge.host,
        "port": challenge.port,
        "algorithm": challenge.algorithm,
        "fingerprint": challenge.fingerprint,
        "publicKey": challenge.public_key,
    }


def create_routes(
    settings: Settings,
    session_service: SessionService,
    catalog: SshCatalog,
    proxy_adapter: ProxyAdapter,
    proxy_registry: ProxyRegistry,
    readiness: Readiness,  # wired explicitly; routes read state from request.app.state
    *,
    key_service: SshKeyService,
    host_trust_service: HostTrustService,
) -> APIRouter:
    _ = readiness
    router = APIRouter()
    templates = _load_templates()

    login_throttle = LoginThrottle(
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_window_seconds,
        lockout_seconds=settings.login_lockout_seconds,
    )

    def _client_key(request: Request) -> str:
        client = request.client
        return client.host if client is not None else "unknown"

    # --- Auth routes ---
    @router.get("/login")
    async def login_page(request: Request) -> HTMLResponse:
        template = templates.get_template("login.html")
        csrf = get_csrf_token(request)
        return HTMLResponse(template.render(csrf_token=csrf, request=request))

    @router.post("/login")
    async def login_submit(
        request: Request,
        password: str = Form(...),
        csrf_token: str = Form(...),
    ) -> Response:
        key = _client_key(request)
        allowed, retry_after = login_throttle.check(key)
        if not allowed:
            return Response(
                content="Too many login attempts",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        if not verify_csrf(request, csrf_token):
            return HTMLResponse("Invalid CSRF token", status_code=403)

        password_hash = load_password_hash_from_file(settings)
        if not verify_password(password, password_hash):
            login_throttle.record_failure(key)
            return HTMLResponse("Invalid password", status_code=401)

        login_throttle.record_success(key)
        create_session(request, settings)
        return RedirectResponse(url="/", status_code=303)

    @router.post("/logout", dependencies=[Depends(require_auth)])
    async def logout(request: Request, csrf_token: str | None = Form(None)) -> Response:
        if not verify_csrf(request, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        clear_session(request)
        return RedirectResponse(url="/login", status_code=303)

    # --- Dashboard ---
    @router.get("/", dependencies=[Depends(require_auth)])
    async def dashboard(request: Request) -> HTMLResponse:
        template = templates.get_template("dashboard.html")
        csrf = get_csrf_token(request)
        return HTMLResponse(
            template.render(
                csrf_token=csrf,
                request=request,
                canonical_origin=settings.canonical_origin,
            )
        )

    # --- API routes ---
    @router.get("/api/sessions", dependencies=[Depends(require_auth)])
    async def list_sessions(request: Request) -> JSONResponse:
        workspaces = await session_service.get_workspaces_full()
        ws_data: list[dict[str, object]] = []
        for ws in workspaces:
            workspace: dict[str, object] = {
                "alias": ws.alias,
                "state": ws.state,
                "sessionId": str(ws.session_id) if ws.session_id else None,
                "editorUrl": ws.editor_url,
                "connectedClients": ws.connected_clients,
                "disconnectDeadline": ws.disconnect_deadline.isoformat()
                if ws.disconnect_deadline
                else None,
                "stage": ws.stage,
                "errorCode": ws.error_code,
                "errorMessage": ws.error_message,
                "canOpen": ws.can_open,
                "canClose": ws.can_close,
                "canRetry": ws.can_retry,
                "catalogMissing": ws.catalog_missing,
            }
            if ws.ssh_host_key is not None:
                workspace["sshHostKey"] = _host_key_payload(ws.ssh_host_key)
            ws_data.append(workspace)
        return JSONResponse({"workspaces": ws_data})

    @router.get("/api/sessions/{alias:path}")
    async def get_session_by_alias_route(
        request: Request,
        alias: str,
    ) -> JSONResponse:
        await require_auth.__call__(request)
        view = await session_service.get_session_view(alias)
        if view is None:
            raise HTTPException(status_code=404, detail="Session not found")
        response: dict[str, object] = {
            "id": str(view.id),
            "alias": view.alias,
            "state": view.state.value,
            "stage": view.stage.value if view.stage else None,
            "connectedClients": view.connected_clients,
            "disconnectDeadline": view.disconnect_deadline.isoformat()
            if view.disconnect_deadline
            else None,
            "editorUrl": view.editor_url,
            "errorCode": view.error_code,
            "errorMessage": view.error_message,
        }
        if view.ssh_host_key is not None:
            response["sshHostKey"] = _host_key_payload(view.ssh_host_key)
        return JSONResponse(response)

    @router.post("/api/sessions/{alias:path}/open", dependencies=[Depends(require_ready)])
    async def open_session(
        request: Request,
        alias: str,
    ) -> JSONResponse:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)
        try:
            view = await session_service.open(alias)
            return JSONResponse(
                {
                    "alias": view.alias,
                    "status": "open_initiated",
                    "session_id": str(view.id),
                },
                status_code=202,
            )
        except GatewayError as exc:
            return _problem(exc, str(uuid.uuid4()))

    @router.post("/api/sessions/{alias:path}/close", dependencies=[Depends(require_ready)])
    async def close_session(
        request: Request,
        alias: str,
    ) -> Response:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)
        try:
            await session_service.close(alias)
            return Response(status_code=204)
        except GatewayError as exc:
            return _problem(exc, str(uuid.uuid4()))

    @router.post("/api/sessions/{alias:path}/retry", dependencies=[Depends(require_ready)])
    async def retry_session(
        request: Request,
        alias: str,
    ) -> JSONResponse:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)
        try:
            view = await session_service.retry(alias)
            return JSONResponse(
                {
                    "alias": view.alias,
                    "status": "retry_initiated",
                    "session_id": str(view.id),
                },
                status_code=202,
            )
        except GatewayError as exc:
            return _problem(exc, str(uuid.uuid4()))

    # --- SSH Config routes ---
    @router.get("/settings/ssh", dependencies=[Depends(require_auth)])
    async def ssh_config_page(request: Request) -> HTMLResponse:
        template = templates.get_template("ssh_config.html")
        csrf = get_csrf_token(request)
        config_text = ""
        with suppress(OSError):
            config_text = settings.ssh_config_path.read_text(encoding="utf-8")
        return HTMLResponse(
            template.render(
                csrf_token=csrf,
                config_text=config_text,
                revision=compute_config_revision(config_text),
                request=request,
            )
        )

    @router.get("/api/ssh/config", dependencies=[Depends(require_auth)])
    async def get_ssh_config(request: Request) -> JSONResponse:
        try:
            text = settings.ssh_config_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        return JSONResponse(
            SshConfigResponse(
                text=text,
                revision=compute_config_revision(text),
            ).model_dump()
        )

    @router.put("/api/ssh/config", dependencies=[Depends(require_ready)])
    async def put_ssh_config(
        request: Request,
        body: SshConfigUpdateRequest,
    ) -> JSONResponse:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)
        try:
            snapshot = await validate_and_save_config(
                settings,
                body.text,
                body.expected_revision,
            )
            catalog.set_snapshot(snapshot)
            return JSONResponse(
                SshConfigResponse(
                    text=body.text,
                    revision=snapshot.revision,
                ).model_dump()
            )
        except GatewayError as exc:
            return _problem(exc, str(uuid.uuid4()))

    @router.get("/api/ssh/catalog", dependencies=[Depends(require_auth)])
    async def get_ssh_catalog(request: Request) -> JSONResponse:
        snapshot = catalog.snapshot
        if snapshot is None:
            return JSONResponse(
                CatalogResponse(
                    revision="",
                    aliases=[],
                    error="Catalog not loaded",
                ).model_dump()
            )
        return JSONResponse(
            CatalogResponse(
                revision=snapshot.revision,
                aliases=list(snapshot.aliases),
                error=snapshot.error,
            ).model_dump()
        )

    # --- SSH Keys routes ---
    @router.get("/settings/keys", dependencies=[Depends(require_auth)])
    async def ssh_keys_page(request: Request) -> HTMLResponse:
        template = templates.get_template("keys.html")
        csrf = get_csrf_token(request)
        return HTMLResponse(template.render(csrf_token=csrf, request=request))

    @router.get("/api/ssh/keys", dependencies=[Depends(require_auth)])
    async def list_keys(request: Request) -> JSONResponse:
        metadata = await key_service.list_metadata()
        slots: dict[SshKeyType, SshKeySlot] = {}
        for key_type in SSH_KEY_TYPES:
            key = metadata[key_type]
            if key is None:
                slots[key_type] = SshKeySlot(present=False)
            else:
                slots[key_type] = SshKeySlot(
                    present=True,
                    name=key.name,
                    algorithm=key.algorithm,
                    fingerprint=key.fingerprint,
                )
        return JSONResponse(SshKeyInventory(keys=slots).model_dump(exclude_none=True))

    @router.post("/api/ssh/keys", dependencies=[Depends(require_ready)])
    async def create_key(
        request: Request,
        name: Annotated[str, Form(...)],
        private_key: Annotated[UploadFile, File(...)],
    ) -> JSONResponse:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)
        try:
            private_key_bytes = await private_key.read(settings.ssh_key_upload_max_bytes + 1)
            if len(private_key_bytes) > settings.ssh_key_upload_max_bytes:
                raise GatewayError(
                    ErrorCode.SSH_KEY_INVALID,
                    f"Private key exceeds {settings.ssh_key_upload_max_bytes} bytes",
                    status_code=400,
                )

            metadata = await key_service.import_upload(
                name=name,
                private_key_bytes=private_key_bytes,
            )
            public_key = await key_service.get_public_key_text(metadata.type)
            return JSONResponse(
                KeyUploadResponse(
                    name=metadata.name,
                    type=metadata.type,
                    algorithm=metadata.algorithm,
                    fingerprint=metadata.fingerprint,
                    publicKey=public_key,
                ).model_dump(),
                status_code=201,
            )
        except GatewayError as exc:
            return _problem(exc, str(uuid.uuid4()))

    @router.get("/api/ssh/keys/{type}/public")
    async def get_key_public(request: Request, type: SshKeyType) -> Response:
        await require_auth.__call__(request)
        try:
            content = await key_service.get_public_key_text(type)
            return PlainTextResponse(content, media_type="text/plain")
        except GatewayError as exc:
            return _problem(exc, str(uuid.uuid4()))

    @router.delete("/api/ssh/keys/{type}", dependencies=[Depends(require_ready)])
    async def delete_key(request: Request, type: SshKeyType) -> Response:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)
        try:
            await key_service.delete_key(type)
            return Response(status_code=204)
        except GatewayError as exc:
            return _problem(exc, str(uuid.uuid4()))

    @router.post("/api/ssh/hosts/trust", dependencies=[Depends(require_ready)])
    async def trust_host_key(
        request: Request,
        body: TrustHostKeyRequest,
    ) -> Response:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)
        try:
            session = await get_session_by_alias(request.app.state.db, body.alias)
            if session is None:
                raise GatewayError(
                    ErrorCode.ALIAS_NOT_FOUND,
                    f"No session exists for alias '{body.alias}'",
                    status_code=404,
                )

            challenge = await host_trust_service.get_challenge(session.id)
            if challenge is None:
                raise GatewayError(
                    ErrorCode.SSH_HOST_TRUST_MISMATCH,
                    "No pending host-key challenge for this session",
                    status_code=404,
                )
            if (
                challenge.alias != body.alias
                or challenge.host != body.host
                or challenge.port != body.port
                or challenge.public_key != body.publicKey
            ):
                raise GatewayError(
                    ErrorCode.SSH_HOST_TRUST_MISMATCH,
                    "Submitted host/port/public key does not match the pending challenge",
                    status_code=409,
                )

            await host_trust_service.trust(
                session_id=session.id,
                host=body.host,
                port=body.port,
                public_key=body.publicKey,
                replace=body.replace,
            )
            return Response(status_code=204)
        except GatewayError as exc:
            return _problem(exc, str(uuid.uuid4()))

    # --- Operations ---
    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @router.get("/api/version")
    async def version() -> JSONResponse:
        from vscode_gateway import __version__

        return JSONResponse(VersionResponse(version=__version__).model_dump())

    # --- Editor proxy routes ---
    @router.api_route(
        "/editor/{session_id}/{rest_of_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
        dependencies=[Depends(require_ready)],
    )
    async def proxy_http_route(request: Request, session_id: str, rest_of_path: str):
        await require_auth.__call__(request)
        sid = proxy_adapter.parse_session_id(session_id)
        return await proxy_adapter.proxy_http(sid, request)

    @router.websocket("/editor/{session_id}/{rest_of_path:path}")
    async def proxy_ws_route(ws: WebSocket, session_id: str, rest_of_path: str):
        # Reject editor traffic while the gateway is not ready.
        readiness: Readiness | None = getattr(ws.app.state, "readiness", None)
        if readiness is None or readiness.phase != ReadinessPhase.READY:
            await ws.close(code=4503)
            return

        # Authenticate before accepting or contacting the upstream editor.
        if not is_authenticated(ws):
            await ws.close(code=4401)
            return

        # Exact browser Origin validation. Browsers always send ``Origin``
        # on WebSocket handshakes; a missing Origin is treated as a
        # non-browser client and rejected by default per the documented
        # policy.
        origin = ws.headers.get("origin")
        if origin is None or origin != settings.canonical_origin:
            await ws.close(code=4403)
            return

        try:
            sid = proxy_adapter.parse_session_id(session_id)
        except GatewayError:
            await ws.close(code=4004)
            return

        # Resolve through the in-memory registry first; reject missing /
        # stale targets before contacting the database or upstream.
        if proxy_registry.lookup(sid) is None:
            await ws.close(code=4004)
            return

        # Re-read the durable session and require READY. A stale URL for
        # an old run must fail after reopen even if a registry entry
        # somehow lingered.
        record = await get_session(ws.app.state.db, str(sid))
        if record is None or record.state != SessionState.READY:
            await ws.close(code=4004)
            return

        # ``proxy_websocket`` increments presence exactly once after the
        # upstream handshake + downstream accept succeed. The matching
        # decrement is performed here, exactly once, regardless of which
        # failure path occurred (presence is balanced because increment
        # only happens after successful establishment).
        try:
            await proxy_adapter.proxy_websocket(sid, ws)
        finally:
            session_service.on_client_disconnected(sid)

    # --- Return router ---
    return router


def _load_templates():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    template_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env

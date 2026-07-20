from __future__ import annotations

import uuid
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
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
from vscode_gateway.db import get_session
from vscode_gateway.errors import GatewayError
from vscode_gateway.models import (
    CatalogResponse,
    SessionState,
    SshConfigResponse,
    SshConfigUpdateRequest,
    VersionResponse,
)
from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry
from vscode_gateway.readiness import Readiness, ReadinessPhase
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh import SshCatalog, compute_config_revision, validate_and_save_config


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
    recovery (HI-04, Plan §10.3 / §15).
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


def create_routes(
    settings: Settings,
    session_service: SessionService,
    catalog: SshCatalog,
    proxy_adapter: ProxyAdapter,
    proxy_registry: ProxyRegistry,
    readiness: Readiness,  # wired explicitly; routes read state from request.app.state
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
        ws_data: list[dict[str, object]] = [
            {
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
            for ws in workspaces
        ]
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
        return JSONResponse(
            {
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
        )

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
        keys_dir = settings.ssh_keys_dir
        keys: list[dict[str, object]] = []
        if keys_dir.exists():
            for p in sorted(keys_dir.iterdir()):
                if p.suffix == ".pub":
                    name = p.stem
                    try:
                        content = p.read_text(encoding="utf-8").strip()
                        parts = content.split()
                        algorithm = parts[0] if len(parts) > 0 else "unknown"
                        fingerprint = parts[1] if len(parts) > 1 else None
                    except OSError:
                        algorithm = "unknown"
                        fingerprint = None
                        content = None

                    keys.append(
                        {
                            "name": name,
                            "algorithm": algorithm,
                            "fingerprint": fingerprint,
                        }
                    )
        return JSONResponse({"keys": keys})

    @router.post("/api/ssh/keys", dependencies=[Depends(require_ready)])
    async def create_key(request: Request) -> JSONResponse:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)

        import secrets

        from vscode_gateway.ssh import run_process

        key_name = f"key_{secrets.token_hex(4)}"
        key_path = settings.ssh_keys_dir / key_name
        result = await run_process(
            [
                settings.ssh_keygen_executable,
                "-t",
                "ed25519",
                "-f",
                str(key_path),
                "-N",
                "",
                "-C",
                f"vscode-gateway-{key_name}",
            ],
            timeout=settings.subprocess_timeout,
        )
        if result.exit_code != 0:
            return JSONResponse(
                {
                    "error": "Key generation failed",
                    "detail": result.stderr.decode("utf-8", errors="replace")[:500],
                },
                status_code=500,
            )

        pub_content = ""
        with suppress(OSError):
            pub_content = (key_path.with_suffix(".pub")).read_text(encoding="utf-8").strip()

        return JSONResponse(
            {"name": key_name, "public_key": pub_content},
            status_code=201,
        )

    @router.get("/api/ssh/keys/{name}.pub")
    async def get_key_public(request: Request, name: str) -> Response:
        await require_auth.__call__(request)
        pub_path = settings.ssh_keys_dir / f"{name}.pub"
        if not pub_path.exists():
            raise HTTPException(status_code=404, detail="Key not found")
        content = pub_path.read_text(encoding="utf-8")
        return PlainTextResponse(content)

    @router.delete("/api/ssh/keys/{name}", dependencies=[Depends(require_ready)])
    async def delete_key(request: Request, name: str) -> Response:
        await require_auth.__call__(request)
        await require_csrf.__call__(request)
        priv_path = settings.ssh_keys_dir / name
        pub_path = settings.ssh_keys_dir / f"{name}.pub"
        deleted = False
        if priv_path.exists():
            priv_path.unlink()
            deleted = True
        if pub_path.exists():
            pub_path.unlink()
            deleted = True
        if not deleted:
            raise HTTPException(status_code=404, detail="Key not found")
        return Response(status_code=204)

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
        # Readiness gate (HI-04): reject editor traffic while the gateway
        # is starting, recovering, or degraded. Use a stable close code
        # so the browser cannot infer internals.
        readiness: Readiness | None = getattr(ws.app.state, "readiness", None)
        if readiness is None or readiness.phase != ReadinessPhase.READY:
            await ws.close(code=4503)
            return

        # Per plan §16.3: authenticate before ``accept`` and before any
        # upstream connection. We reject with a stable close code so no
        # internals leak and no upstream contact occurs.
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

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import aiosqlite
import httpx
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from vscode_gateway.auth import SecurityHeadersMiddleware
from vscode_gateway.db import open_database, run_migrations
from vscode_gateway.errors import GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.lockfile import ProcessLock, check_multi_worker_env
from vscode_gateway.models import CatalogSnapshot
from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry
from vscode_gateway.readiness import Readiness, ReadinessPhase, UnresolvedCounts
from vscode_gateway.routes import create_routes
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import SshCatalog
from vscode_gateway.ssh_connection import SshConnectionService
from vscode_gateway.ssh_keys import SshKeyService

BackgroundTaskSet = set[asyncio.Task[None]]
BackgroundSpawner = Callable[[Awaitable[None]], asyncio.Task[None]]


@dataclass(slots=True)
class _GatewayServices:
    db: aiosqlite.Connection
    http_client: httpx.AsyncClient
    catalog: SshCatalog
    catalog_snapshot: CatalogSnapshot
    key_service: SshKeyService
    host_trust_service: HostTrustService
    connection_service: SshConnectionService
    runtime: RuntimeService
    proxy_registry: ProxyRegistry
    session_service: SessionService
    proxy_adapter: ProxyAdapter


def configure_logging(settings: Settings) -> None:
    if settings.log_format == "json":
        processors = [
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            structlog.stdlib.filter_by_level,
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def _problem_response(
    *,
    status_code: int,
    error_type: str,
    title: object,
    detail: object,
    code: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": error_type,
            "title": title,
            "status": status_code,
            "detail": detail,
            "code": code,
            "requestId": str(uuid.uuid4()),
        },
        media_type="application/problem+json",
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    http_exc = cast(HTTPException, exc)
    return _problem_response(
        status_code=http_exc.status_code,
        error_type=f"urn:vscode-gateway:error:http_{http_exc.status_code}",
        title=http_exc.detail or "HTTP error",
        detail=http_exc.detail or "",
        code=f"http_{http_exc.status_code}",
    )


async def gateway_error_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    gateway_exc = cast(GatewayError, exc)
    return _problem_response(
        status_code=gateway_exc.status_code,
        error_type=f"urn:vscode-gateway:error:{gateway_exc.code.value}",
        title=gateway_exc.safe_message or gateway_exc.code.value.replace("_", " ").title(),
        detail=gateway_exc.detail or gateway_exc.safe_message,
        code=gateway_exc.code.value,
    )


async def _build_services(
    settings: Settings,
    bg_tasks: BackgroundTaskSet,
) -> _GatewayServices:
    db: aiosqlite.Connection | None = None
    http_client: httpx.AsyncClient | None = None
    session_service: SessionService | None = None
    try:
        db = await open_database(settings.state_dir / "gateway.db")
        migrations_dir = Path(__file__).parent / "migrations"
        await run_migrations(db, migrations_dir)

        # Loopback editor traffic must not use ambient proxy settings.
        http_client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(
                connect=settings.proxy_connect_timeout,
                read=settings.proxy_read_timeout,
                write=30.0,
                pool=30.0,
            ),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            follow_redirects=False,
        )

        catalog = SshCatalog(settings)
        catalog_snapshot = await catalog.refresh()
        key_service = SshKeyService(settings, db)
        host_trust_service = HostTrustService(settings, db)
        connection_service = SshConnectionService(settings, key_service, host_trust_service)
        runtime = RuntimeService(settings)
        proxy_registry = ProxyRegistry()
        session_service = SessionService(
            settings,
            db,
            catalog,
            runtime,
            proxy_registry,
            connection_service,
            host_trust_service,
        )
        proxy_adapter = ProxyAdapter(proxy_registry, http_client, session_service)
        session_service.bind_background(_make_spawner(bg_tasks))
        return _GatewayServices(
            db=db,
            http_client=http_client,
            catalog=catalog,
            catalog_snapshot=catalog_snapshot,
            key_service=key_service,
            host_trust_service=host_trust_service,
            connection_service=connection_service,
            runtime=runtime,
            proxy_registry=proxy_registry,
            session_service=session_service,
            proxy_adapter=proxy_adapter,
        )
    except BaseException:
        if session_service is not None:
            with suppress(Exception):
                await session_service.shutdown()
        if http_client is not None:
            with suppress(Exception):
                await http_client.aclose()
        if db is not None:
            with suppress(Exception):
                await db.close()
        raise


def _attach_services(
    app: FastAPI,
    services: _GatewayServices,
    bg_tasks: BackgroundTaskSet,
) -> None:
    app.state.db = services.db
    app.state.catalog = services.catalog
    app.state.session_service = services.session_service
    app.state.http_client = services.http_client
    app.state.proxy_adapter = services.proxy_adapter
    app.state.proxy_registry = services.proxy_registry
    app.state.runtime = services.runtime
    app.state.key_service = services.key_service
    app.state.host_trust_service = services.host_trust_service
    app.state.connection_service = services.connection_service
    app.state.bg_tasks = bg_tasks  # type: ignore[assignment]  # state is Any


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    logger = structlog.get_logger()
    readiness: Readiness = app.state.readiness  # type: ignore[assignment]

    bg_tasks: BackgroundTaskSet = set()
    process_lock: ProcessLock | None = None
    services: _GatewayServices | None = None

    # Startup failures leave the app degraded so /readyz remains available.
    try:
        # Acquire the singleton lock before opening mutable services.
        process_lock = ProcessLock(settings.state_dir)
        process_lock.acquire()
        app.state.process_lock = process_lock  # type: ignore[assignment]  # state is Any

        services = await _build_services(settings, bg_tasks)
        _attach_services(app, services, bg_tasks)

        router = create_routes(
            settings,
            services.session_service,
            services.catalog,
            services.proxy_adapter,
            services.proxy_registry,
            key_service=services.key_service,
            host_trust_service=services.host_trust_service,
        )
        app.include_router(router)

        static_dir = Path(__file__).parent / "static"
        if static_dir.is_dir():
            app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon() -> Response:
            return Response(status_code=204)

        logger.info("running_startup_recovery")
        readiness.begin_recovery()
        if services.catalog_snapshot.error:
            logger.error("startup_ssh_config_invalid", error=services.catalog_snapshot.error)
            readiness.mark_degraded(
                f"SSH config is invalid: {services.catalog_snapshot.error}", UnresolvedCounts()
            )
        else:
            try:
                report = await services.session_service.recover_all()
                logger.info(
                    "startup_recovery_complete",
                    recovered=report.recovered,
                    failed=report.failed,
                    cleaned=report.cleaned,
                    error_sessions_remaining=report.error_sessions_remaining,
                    orphaned_resources_remaining=report.orphaned_resources_remaining,
                )
                unresolved = UnresolvedCounts(
                    error_sessions=report.error_sessions_remaining,
                    orphaned_resources=report.orphaned_resources_remaining,
                )
                if unresolved.error_sessions > 0 or unresolved.orphaned_resources > 0:
                    readiness.mark_degraded("recovery left unresolved sessions", unresolved)
                    logger.warning(
                        "startup_recovery_degraded",
                        **unresolved.as_dict(),
                    )
                else:
                    readiness.mark_ready()
            except Exception as exc:
                logger.error("startup_recovery_failed", error=str(exc))
                readiness.mark_degraded(f"recovery failed: {exc}", UnresolvedCounts())
    except Exception as exc:
        logger.error("startup_mandatory_failed", error=str(exc))
        readiness.fail(f"mandatory startup failed: {exc}")

    yield

    logger.info("shutting_down")
    for task in list(bg_tasks):
        task.cancel()
    if bg_tasks:
        await asyncio.gather(*bg_tasks, return_exceptions=True)
    if services is not None:
        await services.session_service.shutdown()
        await services.http_client.aclose()
        await services.db.close()
    if process_lock is not None:
        process_lock.release()


def _make_spawner(bg_tasks: BackgroundTaskSet) -> BackgroundSpawner:
    def _spawn(coro: Awaitable[None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coro)
        bg_tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            bg_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                structlog.get_logger().error("background_task_failed", error=str(exc))

        task.add_done_callback(_done)
        return task

    return _spawn


def _validate_secure_cookie_settings(settings: Settings) -> None:
    if not settings.secure_cookies:
        return
    scheme = urlparse(settings.canonical_origin).scheme.lower()
    if scheme != "https":
        msg = (
            "secure_cookies is True but canonical_origin is not HTTPS "
            f"(got {settings.canonical_origin!r}); refusing to start."
        )
        raise RuntimeError(msg)


def create_app() -> FastAPI:
    import os

    os.umask(0o077)

    settings = Settings()
    configure_logging(settings)
    logger = structlog.get_logger()

    _validate_secure_cookie_settings(settings)
    check_multi_worker_env()

    middleware = [
        Middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
            session_cookie="gateway_session",
            max_age=settings.session_max_age_seconds,
            https_only=settings.secure_cookies,
            same_site="lax",
            path="/",
            domain=None,
        ),
        Middleware(SecurityHeadersMiddleware),
    ]

    if settings.allowed_hostnames:
        middleware.append(
            Middleware(
                TrustedHostMiddleware,
                allowed_hosts=settings.allowed_hostnames,
            )
        )

    app = FastAPI(
        title="OpenVSCode SSH Gateway",
        version="0.1.0",
        lifespan=lifespan,
        middleware=middleware,
    )
    app.state.settings = settings
    # Readiness object lives on app.state so it is accessible from the
    # top-level /readyz route registered below (always reachable, even
    # when the lifespan startup sequence fails before routes mount).
    app.state.readiness = Readiness()

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        readiness: Readiness = request.app.state.readiness
        state = readiness.snapshot()
        status_code = 200 if state.phase == ReadinessPhase.READY else 503
        return JSONResponse(
            state.as_response_dict(),
            status_code=status_code,
            media_type="application/problem+json" if status_code != 200 else "application/json",
        )

    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(GatewayError, gateway_error_handler)

    logger.info("app_created")
    return app


def main() -> None:
    import uvicorn

    app = create_app()
    config = uvicorn.Config(
        app=app,
        host=app.state.settings.bind_host,
        port=app.state.settings.bind_port,
        workers=1,
        log_level=app.state.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()

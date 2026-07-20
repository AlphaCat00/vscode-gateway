from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

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

from vscode_gateway.db import open_database, run_migrations
from vscode_gateway.errors import GatewayError
from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry
from vscode_gateway.readiness import Readiness, ReadinessPhase, UnresolvedCounts
from vscode_gateway.routes import create_routes
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh import SshCatalog

BackgroundTaskSet = set[asyncio.Task[None]]
BackgroundSpawner = Callable[[Awaitable[None]], asyncio.Task[None]]


def configure_logging(settings: Settings) -> None:
    if settings.log_format == "json":
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.dev.ConsoleRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    logger = structlog.get_logger()
    readiness: Readiness = app.state.readiness  # type: ignore[assignment]

    bg_tasks: BackgroundTaskSet = set()
    http_client: httpx.AsyncClient | None = None
    db: aiosqlite.Connection | None = None

    # Phase starts as ``starting``. Any failure in the mandatory startup
    # sequence (DB open, migrations, catalog init, capacity rebuild,
    # recovery) reports a bounded ``degraded`` state and continues
    # serving 503 rather than crashing, so a load balancer sees the
    # failure signal and operators can inspect /readyz (HI-04).
    try:
        db = await open_database(settings.state_dir / "gateway.db")
        migrations_dir = Path(__file__).parent / "migrations"
        await run_migrations(db, migrations_dir)

        http_client = httpx.AsyncClient(
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
        await catalog.refresh()

        runtime = RuntimeService(settings)
        proxy_registry = ProxyRegistry()
        session_service = SessionService(settings, db, catalog, runtime, proxy_registry)
        proxy_adapter = ProxyAdapter(proxy_registry, http_client, session_service)

        spawner = _make_spawner(bg_tasks)
        session_service.bind_background(spawner)

        app.state.db = db
        app.state.catalog = catalog
        app.state.session_service = session_service
        app.state.http_client = http_client
        app.state.proxy_adapter = proxy_adapter
        app.state.proxy_registry = proxy_registry
        app.state.runtime = runtime
        app.state.bg_tasks = bg_tasks  # type: ignore[assignment]  # state is Any

        router = create_routes(
            settings, session_service, catalog, proxy_adapter, proxy_registry, readiness
        )
        app.include_router(router)

        static_dir = Path(__file__).parent / "static"
        if static_dir.is_dir():
            app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon() -> Response:
            return Response(status_code=204)

        logger.info("running_startup_recovery")
        await readiness.begin_recovery()
        try:
            report = await session_service.recover_all()
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
                await readiness.mark_degraded("recovery left unresolved sessions", unresolved)
                logger.warning(
                    "startup_recovery_degraded",
                    **unresolved.as_dict(),
                )
            else:
                await readiness.mark_ready()
        except Exception as exc:
            logger.error("startup_recovery_failed", error=str(exc))
            await readiness.mark_degraded(f"recovery failed: {exc}", UnresolvedCounts())
    except Exception as exc:
        # Mandatory pre-recovery setup failed (DB open, migrations, catalog
        # initialization). We cannot serve sessions, but we still serve
        # /readyz with 503 so operators see the failure rather than a
        # crashed process.
        logger.error("startup_mandatory_failed", error=str(exc))
        await readiness.fail(f"mandatory startup failed: {exc}")

    yield

    logger.info("shutting_down")
    for task in list(bg_tasks):
        task.cancel()
    if bg_tasks:
        await asyncio.gather(*bg_tasks, return_exceptions=True)
    if http_client is not None:
        await http_client.aclose()
    if db is not None:
        await db.close()


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


def create_app() -> FastAPI:
    settings = Settings()
    configure_logging(settings)
    logger = structlog.get_logger()

    middleware = [
        Middleware(
            SessionMiddleware,
            secret_key=settings.session_secret.decode("utf-8", errors="replace"),
            session_cookie="gateway_session",
            max_age=settings.session_max_age_seconds,
            https_only=False,
            same_site="lax",
        ),
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

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        import uuid

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": f"urn:vscode-gateway:error:http_{exc.status_code}",
                "title": exc.detail or "HTTP error",
                "status": exc.status_code,
                "detail": exc.detail or "",
                "code": f"http_{exc.status_code}",
                "requestId": str(uuid.uuid4()),
            },
            media_type="application/problem+json",
        )

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
        import uuid

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": f"urn:vscode-gateway:error:{exc.code.value}",
                "title": exc.safe_message or exc.code.value.replace("_", " ").title(),
                "status": exc.status_code,
                "detail": exc.detail or exc.safe_message,
                "code": exc.code.value,
                "requestId": str(uuid.uuid4()),
            },
            media_type="application/problem+json",
        )

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

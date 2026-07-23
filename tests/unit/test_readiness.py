"""Unit tests for readiness state and mutation gating."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport

from vscode_gateway.readiness import Readiness, ReadinessPhase, UnresolvedCounts
from vscode_gateway.routes import require_ready
from vscode_gateway.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        ssh_dir=tmp_path / "ssh",
        ssh_config_path=tmp_path / "ssh" / "config",
        ssh_known_hosts_path=tmp_path / "ssh" / "known_hosts",
        ssh_keys_dir=tmp_path / "ssh" / "keys",
        password_hash_path=tmp_path / "state" / "password.hash",
        session_secret_path=tmp_path / "state" / "session.secret",
    )


def _build_test_app(readiness: Readiness) -> FastAPI:
    """Minimal FastAPI app mirroring the real ``/readyz`` and mutation
    gating behavior so tests can drive the Readiness state directly."""
    app = FastAPI()
    app.state.readiness = readiness

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        state = request.app.state.readiness.snapshot()
        status_code = 200 if state.phase == ReadinessPhase.READY else 503
        return JSONResponse(
            state.as_response_dict(),
            status_code=status_code,
            media_type="application/problem+json" if status_code != 200 else "application/json",
        )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/api/sessions")
    async def list_sessions() -> JSONResponse:
        # Read-only and ungated by design.
        return JSONResponse({"workspaces": []})

    @app.post("/api/sessions/{alias}/open", dependencies=[Depends(require_ready)])
    async def open_session(alias: str) -> JSONResponse:
        return JSONResponse({"alias": alias, "status": "open_initiated"}, status_code=202)

    return app


@pytest.fixture
async def client_with_readiness(
    settings: Settings,
) -> AsyncIterator[tuple[httpx.AsyncClient, Readiness]]:
    readiness = Readiness()
    app = _build_test_app(readiness)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, readiness


# ---------------------------------------------------------------------------
# /readyz body and status by phase
# ---------------------------------------------------------------------------


async def test_readyz_returns_503_before_recovery(
    client_with_readiness: tuple[httpx.AsyncClient, Readiness],
) -> None:
    client, _ = client_with_readiness
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["phase"] == ReadinessPhase.STARTING.value
    assert "unresolved" in body


async def test_readyz_returns_503_during_recovering(
    client_with_readiness: tuple[httpx.AsyncClient, Readiness],
) -> None:
    client, readiness = client_with_readiness
    readiness.begin_recovery()
    response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["phase"] == ReadinessPhase.RECOVERING.value


async def test_readyz_returns_200_after_mark_ready(
    client_with_readiness: tuple[httpx.AsyncClient, Readiness],
) -> None:
    client, readiness = client_with_readiness
    readiness.begin_recovery()
    readiness.mark_ready()
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == ReadinessPhase.READY.value
    assert "unresolved" not in body


async def test_readyz_returns_503_with_unresolved_counts_after_degraded(
    client_with_readiness: tuple[httpx.AsyncClient, Readiness],
) -> None:
    client, readiness = client_with_readiness
    readiness.begin_recovery()
    readiness.mark_degraded(
        "recovery left unresolved sessions",
        UnresolvedCounts(error_sessions=2, orphaned_resources=1),
    )
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["phase"] == ReadinessPhase.DEGRADED.value
    assert body["reason"] == "recovery left unresolved sessions"
    assert body["unresolved"] == {"error_sessions": 2, "orphaned_resources": 1}


# ---------------------------------------------------------------------------
# Mutation gate
# ---------------------------------------------------------------------------


async def test_mutation_route_503_during_recovering(
    client_with_readiness: tuple[httpx.AsyncClient, Readiness],
) -> None:
    client, readiness = client_with_readiness
    readiness.begin_recovery()
    response = await client.post("/api/sessions/host-a/open")
    assert response.status_code == 503


async def test_mutation_route_503_during_degraded(
    client_with_readiness: tuple[httpx.AsyncClient, Readiness],
) -> None:
    client, readiness = client_with_readiness
    readiness.mark_degraded("mandatory failed", UnresolvedCounts())
    response = await client.post("/api/sessions/host-a/open")
    assert response.status_code == 503


async def test_mutation_route_202_after_ready(
    client_with_readiness: tuple[httpx.AsyncClient, Readiness],
) -> None:
    client, readiness = client_with_readiness
    readiness.mark_ready()
    response = await client.post("/api/sessions/host-a/open")
    assert response.status_code == 202


# ---------------------------------------------------------------------------
# Read-only routes still serve while not ready
# ---------------------------------------------------------------------------


async def test_readonly_route_200_during_recovering(
    client_with_readiness: tuple[httpx.AsyncClient, Readiness],
) -> None:
    client, readiness = client_with_readiness
    readiness.begin_recovery()
    sessions = await client.get("/api/sessions")
    assert sessions.status_code == 200
    assert sessions.json() == {"workspaces": []}

    health = await client.get("/healthz")
    assert health.status_code == 200


# ---------------------------------------------------------------------------
# Readiness state machine primitives
# ---------------------------------------------------------------------------


def test_readiness_transitions_replace_complete_snapshots() -> None:
    readiness = Readiness()
    assert readiness.phase == ReadinessPhase.STARTING
    assert readiness.is_ready is False
    starting = readiness.snapshot()

    readiness.begin_recovery()
    assert readiness.phase == ReadinessPhase.RECOVERING
    assert readiness.is_ready is False
    recovering = readiness.snapshot()
    assert recovering is not starting
    assert recovering.reason == ""
    assert recovering.unresolved == UnresolvedCounts()

    readiness.mark_degraded("left over", UnresolvedCounts(error_sessions=1))
    assert readiness.phase == ReadinessPhase.DEGRADED
    degraded = readiness.snapshot()
    assert degraded is not recovering
    assert degraded.reason == "left over"
    assert degraded.unresolved == UnresolvedCounts(error_sessions=1)

    # mark_ready clears prior reason/unresolved
    readiness.mark_ready()
    assert readiness.is_ready is True
    ready = readiness.snapshot()
    assert ready is not degraded
    assert ready.reason == ""
    assert ready.unresolved == UnresolvedCounts()
    assert degraded.reason == "left over"
    assert degraded.unresolved.error_sessions == 1


def test_readiness_fail_marks_degraded_with_empty_counts() -> None:
    readiness = Readiness()
    readiness.fail("database open error")
    assert readiness.phase == ReadinessPhase.DEGRADED
    snap = readiness.snapshot()
    assert snap.reason == "database open error"
    assert snap.unresolved.error_sessions == 0
    assert snap.unresolved.orphaned_resources == 0


# ---------------------------------------------------------------------------
# HTTPException detail transport through require_ready
# ---------------------------------------------------------------------------


async def test_require_ready_raises_http_exception_when_not_ready() -> None:
    readiness = Readiness()
    # Without "ready" phase, mutating route must yield 503.
    app = _build_test_app(readiness)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/sessions/host-a/open")
    assert response.status_code == 503
    body = response.json()
    # FastAPI HTTPException detail surfaces as ``detail``.
    assert "detail" in body


# ---------------------------------------------------------------------------
# recover_all reports unresolved counts (smoke against the model)
# ---------------------------------------------------------------------------


async def test_recovery_report_has_unresolved_count_fields() -> None:
    from vscode_gateway.models import RecoveryReport

    report = RecoveryReport(recovered=0, failed=1, cleaned=0, total=1)
    assert report.error_sessions_remaining == 0
    assert report.orphaned_resources_remaining == 0

    report = RecoveryReport(
        recovered=0,
        failed=1,
        cleaned=0,
        total=1,
        error_sessions_remaining=2,
        orphaned_resources_remaining=1,
    )
    assert report.error_sessions_remaining == 2
    assert report.orphaned_resources_remaining == 1

"""Unit tests for HI-07: process-singleton lock enforcement."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from vscode_gateway.app import lifespan
from vscode_gateway.lockfile import (
    LOCK_FILE_NAME,
    LockAcquisitionError,
    ProcessLock,
    check_multi_worker_env,
)
from vscode_gateway.readiness import Readiness, ReadinessPhase
from vscode_gateway.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        ssh_config_path=tmp_path / "ssh_config",
        ssh_keys_dir=tmp_path / "keys",
        password_hash_path=tmp_path / "state" / "password.hash",
        session_secret_path=tmp_path / "state" / "session.secret",
    )


def _build_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.readiness = Readiness()
    return app


# ---------------------------------------------------------------------------
# ProcessLock primitives
# ---------------------------------------------------------------------------


def test_acquire_creates_lock_file_in_state_dir(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock = ProcessLock(state_dir)
    assert not lock.is_held
    lock.acquire()
    try:
        assert lock.is_held
        assert lock.lock_path == state_dir / LOCK_FILE_NAME
        assert lock.lock_path.exists()
    finally:
        lock.release()
    assert not lock.is_held


def test_acquire_then_release_releases_to_second_lock(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock = ProcessLock(state_dir)
    lock.acquire()
    try:
        second = ProcessLock(state_dir)
        with pytest.raises(LockAcquisitionError):
            second.acquire()
    finally:
        lock.release()
    # After release, a new ProcessLock must be able to acquire.
    third = ProcessLock(state_dir)
    third.acquire()
    third.release()


def test_second_process_cannot_acquire_lock(tmp_path: Path) -> None:
    """Simulate a second process by holding a raw ``flock`` on the lock
    file from this test process; ``ProcessLock.acquire`` must fail fast
    with ``LockAcquisitionError`` (HI-07)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock_path = state_dir / LOCK_FILE_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LockAcquisitionError) as excinfo:
            ProcessLock(state_dir).acquire()
        assert "singleton lock" in str(excinfo.value).lower()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_symlink_lock_path_rejected(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = tmp_path / "real_lock"
    target.write_bytes(b"")
    symlink = state_dir / LOCK_FILE_NAME
    symlink.symlink_to(target)
    with pytest.raises(LockAcquisitionError) as excinfo:
        ProcessLock(state_dir).acquire()
    assert "symlink" in str(excinfo.value).lower()


def test_missing_state_dir_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(LockAcquisitionError):
        ProcessLock(missing).acquire()


def test_state_dir_not_a_directory_rejected(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir"
    file_path.write_bytes(b"")
    with pytest.raises(LockAcquisitionError):
        ProcessLock(file_path).acquire()


# ---------------------------------------------------------------------------
# lifespan integration
# ---------------------------------------------------------------------------


def test_lifespan_acquires_and_releases_lock(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = _build_app(settings)

    async def _drive() -> None:
        async with lifespan(app):
            lock: ProcessLock | None = getattr(app.state, "process_lock", None)
            assert lock is not None
            assert lock.is_held
            assert (tmp_path / "state" / LOCK_FILE_NAME).exists()

    asyncio.run(_drive())

    # After lifespan teardown the OS lock must be released: a fresh
    # ProcessLock on the same path must acquire without error.
    leftover = ProcessLock(tmp_path / "state")
    leftover.acquire()
    leftover.release()


def test_lifespan_second_invocation_fails_fast_when_lock_held(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = _build_app(settings)

    async def _hold_then_other() -> None:
        async with lifespan(app):
            # While the first lifespan is active, attempt a second
            # ProcessLock on the same state directory.
            second = ProcessLock(tmp_path / "state")
            with pytest.raises(LockAcquisitionError):
                second.acquire()
            # The first lifespan remains the owner.
            first: ProcessLock = app.state.process_lock  # type: ignore[assignment]
            assert first.is_held

    asyncio.run(_hold_then_other())


def test_lifespan_reports_degraded_when_lock_already_held(tmp_path: Path) -> None:
    """If the lock is held by a peer before lifespan runs, the lifespan
    must mark readiness degraded (HI-04 flow) and proceed to ``yield``
    so ``/readyz`` keeps reporting the failure."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock_path = state_dir / LOCK_FILE_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        settings = _settings(tmp_path)
        app = _build_app(settings)

        async def _drive() -> None:
            async with lifespan(app):
                readiness: Readiness = app.state.readiness  # type: ignore[assignment]
                assert readiness.phase == ReadinessPhase.DEGRADED
                snap = readiness.snapshot()
                assert "singleton" in snap.reason.lower() or "mandatory" in snap.reason.lower()
                # DB / http_client must NOT be initialized when the
                # singleton lock could not be acquired.
                assert getattr(app.state, "db", None) is None
                assert getattr(app.state, "http_client", None) is None

        asyncio.run(_drive())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_lifespan_does_not_release_lock_prematurely_on_inner_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure during recovery (after the lock is held) must NOT
    release the lock mid-lifespan; it stays held until shutdown."""
    settings = _settings(tmp_path)
    app = _build_app(settings)

    async def _boom_recover(self: Any) -> Any:
        raise RuntimeError("recovery exploded")

    monkeypatch.setattr("vscode_gateway.sessions.SessionService.recover_all", _boom_recover)

    async def _drive() -> None:
        async with lifespan(app):
            lock: ProcessLock = app.state.process_lock  # type: ignore[assignment]
            assert lock.is_held
            # The recovery error kept us DEGRADED, but the singleton lock
            # must still be held (this process is the legitimate owner).
            readiness: Readiness = app.state.readiness  # type: ignore[assignment]
            assert readiness.phase == ReadinessPhase.DEGRADED
            # A second ProcessLock on the same state dir must fail.
            with pytest.raises(LockAcquisitionError):
                ProcessLock(tmp_path / "state").acquire()

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Multi-worker env-var belt-and-suspenders check
# ---------------------------------------------------------------------------


def test_check_multi_worker_env_passes_with_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    check_multi_worker_env()


def test_check_multi_worker_env_rejects_uvicorn_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UVICORN_WORKERS", "4")
    with pytest.raises(LockAcquisitionError) as excinfo:
        check_multi_worker_env()
    assert "UVICORN_WORKERS" in str(excinfo.value)


def test_check_multi_worker_env_rejects_web_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(LockAcquisitionError):
        check_multi_worker_env()


def test_check_multi_worker_env_ignores_non_numeric_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UVICORN_WORKERS", "auto")
    check_multi_worker_env()


# ---------------------------------------------------------------------------
# create_app integration: second create_app against same state dir fails
# ---------------------------------------------------------------------------


def test_create_app_refuses_second_instance_via_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two lifespans against the same state directory: the first holds
    the lock for its whole lifetime, the second must fail fast and mark
    readiness degraded (HI-07, Plan §6.1 test requirement)."""
    from vscode_gateway.app import create_app

    state_dir = tmp_path / "state"
    monkeypatch.setenv("VSC_GATEWAY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VSC_GATEWAY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("VSC_GATEWAY_SSH_CONFIG_PATH", str(tmp_path / "ssh_config"))
    monkeypatch.setenv("VSC_GATEWAY_SSH_KEYS_DIR", str(tmp_path / "keys"))
    monkeypatch.setenv("VSC_GATEWAY_PASSWORD_HASH_PATH", str(state_dir / "pw.hash"))
    monkeypatch.setenv("VSC_GATEWAY_SESSION_SECRET_PATH", str(state_dir / "sess.secret"))
    monkeypatch.setenv("VSC_GATEWAY_SECURE_COOKIES", "false")
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    app1 = create_app()

    async def _drive_first() -> None:
        async with lifespan(app1):
            # The first app is legitimately holding the lock.
            first_lock: ProcessLock = app1.state.process_lock  # type: ignore[assignment]
            assert first_lock.is_held

            # A second lifespan against the same state dir fails fast.
            app2 = create_app()
            with pytest.raises(LockAcquisitionError):
                ProcessLock(app2.state.settings.state_dir).acquire()

    asyncio.run(_drive_first())

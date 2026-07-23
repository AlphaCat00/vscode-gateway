"""Unit tests for process-singleton lock enforcement."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import errno
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

_WORKER_ENV_VARS = ("UVICORN_WORKERS", "WEB_CONCURRENCY")


def _settings(tmp_path: Path) -> Settings:
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
    with ``LockAcquisitionError``."""
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


def test_acquire_closes_descriptor_when_flock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    closed: list[int] = []
    real_close = os.close

    def _fail_flock(fd: int, operation: int) -> None:
        raise OSError(errno.EIO, "flock failed")

    def _record_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(fcntl, "flock", _fail_flock)
    monkeypatch.setattr(os, "close", _record_close)

    lock = ProcessLock(state_dir)
    with pytest.raises(LockAcquisitionError):
        lock.acquire()

    assert not lock.is_held
    assert len(closed) == 1
    with pytest.raises(OSError):
        os.fstat(closed[0])


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
    must mark readiness degraded and proceed to ``yield``
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


def test_lifespan_skips_recovery_when_ssh_config_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    settings.ssh_config_path.write_text(
        "Host target\n    HostName target.example.test\nMatch exec touch /tmp/unsafe\n",
        encoding="utf-8",
    )
    app = _build_app(settings)
    recovery_called = False

    async def _fail_if_recovered(self: Any) -> Any:
        nonlocal recovery_called
        recovery_called = True
        raise AssertionError("recovery must not parse a rejected SSH config")

    monkeypatch.setattr("vscode_gateway.sessions.SessionService.recover_all", _fail_if_recovered)

    async def _drive() -> None:
        async with lifespan(app):
            readiness: Readiness = app.state.readiness  # type: ignore[assignment]
            assert readiness.phase == ReadinessPhase.DEGRADED
            assert "ssh config is invalid" in readiness.snapshot().reason.lower()
            assert recovery_called is False

    asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Multi-worker env-var belt-and-suspenders check
# ---------------------------------------------------------------------------


def test_check_multi_worker_env_passes_with_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in _WORKER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    check_multi_worker_env()


@pytest.mark.parametrize(
    ("env_var", "workers"),
    [("UVICORN_WORKERS", "4"), ("WEB_CONCURRENCY", "2")],
)
def test_check_multi_worker_env_rejects_multiple_workers(
    monkeypatch: pytest.MonkeyPatch, env_var: str, workers: str
) -> None:
    for var in _WORKER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(env_var, workers)
    with pytest.raises(LockAcquisitionError) as excinfo:
        check_multi_worker_env()
    assert env_var in str(excinfo.value)


def test_check_multi_worker_env_ignores_non_numeric_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in _WORKER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UVICORN_WORKERS", "auto")
    check_multi_worker_env()

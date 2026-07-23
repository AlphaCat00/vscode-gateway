"""Process-wide file lock enforcing the single-worker deployment model."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

import structlog

LOCK_FILE_NAME = "gateway.lock"
_WORKER_ENV_VARS = ("UVICORN_WORKERS", "WEB_CONCURRENCY")


class LockAcquisitionError(RuntimeError):
    """Raised when the process-singleton lock cannot be acquired."""


def _errno_name(exc: OSError) -> str:
    """Best-effort symbolic name for an ``OSError.errno`` value."""
    if exc.errno is None:
        return "unknown"
    return errno.errorcode.get(exc.errno, str(exc.errno))


def _validate_state_dir(state_dir: Path) -> None:
    if state_dir.is_dir():
        return
    reason = "does not exist" if not state_dir.exists() else "is not a directory"
    msg = f"State directory {state_dir!s} {reason}; refusing to start."
    raise LockAcquisitionError(msg)


class ProcessLock:
    """Exclusive process-wide lock held for the process lifetime.

    The lock is acquired on the file ``<state_dir>/gateway.lock`` using
    ``fcntl.flock(LOCK_EX | LOCK_NB)``. The descriptor is held open until
    ``release()`` or process exit; the OS releases the lock when the
    descriptor is closed (which happens automatically on exit).
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        lock_file_name: str = LOCK_FILE_NAME,
    ) -> None:
        self._state_dir = state_dir
        self._lock_path = state_dir / lock_file_name
        self._fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        logger = structlog.get_logger()
        if self._fd is not None:
            return
        _validate_state_dir(self._state_dir)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            fd = os.open(self._lock_path, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                logger.error(
                    "singleton_lock_symlink_rejected",
                    lock_file=str(self._lock_path),
                )
                msg = f"Lock file path {self._lock_path!s} is a symlink; refusing to start."
                raise LockAcquisitionError(msg) from exc
            logger.error(
                "singleton_lock_open_failed",
                lock_file=str(self._lock_path),
                error=_errno_name(exc),
            )
            msg = f"Failed to open singleton lock file {self._lock_path!s}: {_errno_name(exc)}."
            raise LockAcquisitionError(msg) from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                logger.error(
                    "singleton_lock_busy",
                    lock_file=str(self._lock_path),
                    error=_errno_name(exc),
                )
                msg = (
                    "Another gateway process holds the singleton lock at "
                    f"{self._lock_path!s}; refuse to start."
                )
                raise LockAcquisitionError(msg) from exc
            except OSError as exc:
                logger.error(
                    "singleton_lock_flock_failed",
                    lock_file=str(self._lock_path),
                    error=_errno_name(exc),
                )
                msg = (
                    f"Failed to acquire singleton lock at {self._lock_path!s}: {_errno_name(exc)}."
                )
                raise LockAcquisitionError(msg) from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        logger.info(
            "singleton_lock_acquired",
            lock_file=str(self._lock_path),
        )

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        structlog.get_logger().info(
            "singleton_lock_released",
            lock_file=str(self._lock_path),
        )


def check_multi_worker_env() -> None:
    """Belt-and-suspenders check: refuse to start when the deployment is
    explicitly configured for multiple workers.

    The OS file lock is the primary mechanism; this guard catches the
    common ``WEB_CONCURRENCY`` / ``UVICORN_WORKERS`` environment
    variables that operators or platform loader scripts set when they
    intend to run more than one worker sharing the same state directory.
    """
    logger = structlog.get_logger()
    for var in _WORKER_ENV_VARS:
        raw = os.environ.get(var)
        if not raw or not raw.strip():
            continue
        try:
            value = int(raw.strip())
        except ValueError:
            continue
        if value > 1:
            logger.error(
                "multi_worker_env_rejected",
                env_var=var,
                workers=value,
            )
            msg = (
                f"Environment variable {var}={value} requests multiple workers; "
                "the gateway is a single-process service."
            )
            raise LockAcquisitionError(msg)

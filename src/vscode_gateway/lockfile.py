"""Process-singleton file lock (HI-07 correction).

Per Plan §6.1 the gateway must run exactly one ASGI process against a
state directory. Alias locks, capacity ownership, tunnels, registry,
presence, and grace timers are process-local; a second worker sharing
SQLite while holding its own in-memory state breaks the architecture's
core invariants. The documented ``uvicorn ... --workers 1`` command is
advisory, so this module enforces the singleton invariant with an
exclusive operating-system file lock acquired **before** any mutable
service (DB, sessions, http client) is opened.

The lock is acquired non-blocking (``LOCK_EX | LOCK_NB``); a second
process gets ``BlockingIOError`` immediately and fails fast. The file
descriptor is held for the entire process lifetime and the OS releases
the lock automatically on process exit. ``release()`` is provided for
clean teardown in tests and an explicit shutdown sequence.

Security: the lock file path must not be a symlink. ``open(2)`` is
called with ``O_NOFOLLOW`` so a symlink last-step replacement is also
rejected at the syscall level. The state directory must exist as a
directory; minimal existence + writability is required to acquire the
lock. Full filesystem-mode and ownership enforcement overlaps with
ME-01 and is intentionally not duplicated here.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

import structlog

LOCK_FILE_NAME = "gateway.lock"


class LockAcquisitionError(RuntimeError):
    """Raised when the process-singleton lock cannot be acquired."""


def _errno_name(exc: OSError) -> str:
    """Best-effort symbolic name for an ``OSError.errno`` value."""
    if exc.errno is None:
        return "unknown"
    return errno.errorcode.get(exc.errno, str(exc.errno))


def _validate_state_dir(state_dir: Path) -> None:
    if not state_dir.exists():
        msg = f"State directory {state_dir!s} does not exist; refusing to start (HI-07)."
        raise LockAcquisitionError(msg)
    if not state_dir.is_dir():
        msg = f"State directory {state_dir!s} is not a directory; refusing to start (HI-07)."
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
        if self._lock_path.is_symlink():
            logger.error(
                "singleton_lock_symlink_rejected",
                lock_file=str(self._lock_path),
            )
            msg = f"Lock file path {self._lock_path!s} is a symlink; refusing to start (HI-07)."
            raise LockAcquisitionError(msg)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            fd = os.open(self._lock_path, flags, 0o600)
        except OSError as exc:
            logger.error(
                "singleton_lock_open_failed",
                lock_file=str(self._lock_path),
                error=_errno_name(exc),
            )
            msg = (
                f"Failed to open singleton lock file {self._lock_path!s}: "
                f"{_errno_name(exc)} (HI-07)."
            )
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
                    f"{self._lock_path!s}; refuse to start (HI-07)."
                )
                raise LockAcquisitionError(msg) from exc
            except OSError as exc:
                logger.error(
                    "singleton_lock_flock_failed",
                    lock_file=str(self._lock_path),
                    error=_errno_name(exc),
                )
                msg = (
                    f"Failed to acquire singleton lock at {self._lock_path!s}: "
                    f"{_errno_name(exc)} (HI-07)."
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
    for var in ("UVICORN_WORKERS", "WEB_CONCURRENCY"):
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
                "the gateway is a single-process service (HI-07, Plan §6.1)."
            )
            raise LockAcquisitionError(msg)

"""Manage uploaded SSH private keys and their display metadata."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
from pathlib import Path

import aiosqlite
import asyncssh

from vscode_gateway.db import (
    delete_ssh_key_metadata,
    get_ssh_key_metadata,
    insert_ssh_key_metadata,
    list_ssh_key_metadata,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import SSH_KEY_LOAD_ORDER, SSH_KEY_TYPES, SshKeyMetadata, SshKeyType
from vscode_gateway.settings import Settings

_PRIVATE_KEY_MODE = 0o600
_PUBLIC_KEY_MODE = 0o600

_ALGORITHM_TO_TYPE: dict[str, SshKeyType] = {
    "ssh-ed25519": "ed25519",
    "ssh-rsa": "rsa",
    "rsa-sha2-256": "rsa",
    "rsa-sha2-512": "rsa",
    "ecdsa-sha2-nistp256": "ecdsa",
    "ecdsa-sha2-nistp384": "ecdsa",
    "ecdsa-sha2-nistp521": "ecdsa",
}

_NAME_RE = re.compile(r"^[^\x00\r\n/\\]{1,128}$")


def _classify_key(key: asyncssh.SSHKey) -> SshKeyType:
    """Map an AsyncSSH key to the gateway type slot or raise ``KeyImportError``."""
    algo = key.get_algorithm()
    slot = _ALGORITHM_TO_TYPE.get(algo)
    if slot is None:
        raise asyncssh.KeyImportError(f"Unsupported key algorithm: {algo!r}")
    return slot


def _private_key_path(settings: Settings, type_: SshKeyType) -> Path:
    return settings.ssh_keys_dir / type_


def _public_key_path(settings: Settings, type_: SshKeyType) -> Path:
    return settings.ssh_keys_dir / f"{type_}.pub"


class SshKeyService:
    """Manages uploaded private keys for the gateway."""

    def __init__(self, settings: Settings, db: aiosqlite.Connection) -> None:
        self._settings = settings
        self._db = db
        self._mutation_lock = asyncio.Lock()

    def load_present_keys(
        self, order: tuple[SshKeyType, ...] = SSH_KEY_LOAD_ORDER
    ) -> list[asyncssh.SSHKey]:
        """Load present keys in deterministic authentication order."""
        keys: list[asyncssh.SSHKey] = []
        for type_ in order:
            path = _private_key_path(self._settings, type_)
            if not path.exists():
                continue
            try:
                key = asyncssh.import_private_key(path.read_bytes())
            except (OSError, asyncssh.KeyImportError) as exc:
                raise GatewayError(
                    ErrorCode.SSH_KEY_INVALID,
                    f"Failed to load stored {type_} key: {exc}",
                    status_code=500,
                ) from exc
            keys.append(key)
        return keys

    async def list_metadata(self) -> dict[SshKeyType, SshKeyMetadata | None]:
        """Return the fixed three-slot inventory for the API response."""
        present = await list_ssh_key_metadata(self._db)
        slots: dict[SshKeyType, SshKeyMetadata | None] = {}
        for type_ in SSH_KEY_TYPES:
            row = present.get(type_)
            if row is None:
                slots[type_] = None
            else:
                name, algorithm, fingerprint = row
                slots[type_] = SshKeyMetadata(
                    type=type_, name=name, algorithm=algorithm, fingerprint=fingerprint
                )
        return slots

    async def get_public_key_text(self, type_: SshKeyType) -> str:
        """Return the OpenSSH-formatted public key for the requested slot."""
        path = _public_key_path(self._settings, type_)
        if not path.exists():
            raise GatewayError(ErrorCode.SSH_KEY_NOT_FOUND, f"No {type_} key present")
        return path.read_text(encoding="utf-8")

    async def import_upload(self, *, name: str, private_key_bytes: bytes) -> SshKeyMetadata:
        if not _NAME_RE.match(name):
            raise GatewayError(
                ErrorCode.SSH_KEY_INVALID,
                "Display name must be 1-128 chars and contain no newlines or path separators",
                status_code=400,
            )

        if len(private_key_bytes) > self._settings.ssh_key_upload_max_bytes:
            raise GatewayError(
                ErrorCode.SSH_KEY_INVALID,
                f"Private key exceeds {self._settings.ssh_key_upload_max_bytes} bytes",
                status_code=400,
            )

        try:
            key = asyncssh.import_private_key(private_key_bytes)
        except asyncssh.KeyEncryptionError as exc:
            raise GatewayError(
                ErrorCode.SSH_KEY_INVALID,
                "Encrypted private keys are not supported in this version",
                status_code=400,
            ) from exc
        except asyncssh.KeyImportError as exc:
            raise GatewayError(
                ErrorCode.SSH_KEY_INVALID,
                f"Invalid private key: {exc}",
                status_code=400,
            ) from exc

        try:
            type_ = _classify_key(key)
        except asyncssh.KeyImportError as exc:
            raise GatewayError(
                ErrorCode.SSH_KEY_INVALID,
                str(exc),
                status_code=400,
            ) from exc

        algorithm = key.get_algorithm()
        fingerprint = key.get_fingerprint("sha256")
        public_key_text = key.export_public_key("openssh").decode("utf-8").strip()
        private_key_bytes_normalized = key.export_private_key("openssh")

        async with self._mutation_lock:
            existing = await get_ssh_key_metadata(self._db, type_)
            if existing is not None:
                raise GatewayError(
                    ErrorCode.SSH_KEY_EXISTS,
                    f"A {type_} key already exists; delete it before uploading a new one",
                    status_code=409,
                )
            if _private_key_path(self._settings, type_).exists():
                raise GatewayError(
                    ErrorCode.SSH_KEY_EXISTS,
                    f"A {type_} key file already exists on disk ({type_}); delete it first",
                    status_code=409,
                )

            await self._atomic_write_pair(type_, private_key_bytes_normalized, public_key_text)

            try:
                await insert_ssh_key_metadata(
                    self._db,
                    type_=type_,
                    name=name,
                    algorithm=algorithm,
                    fingerprint=fingerprint,
                )
            except Exception:
                self._unlink_pair(type_)
                raise

        return SshKeyMetadata(type=type_, name=name, algorithm=algorithm, fingerprint=fingerprint)

    async def delete_key(self, type_: SshKeyType) -> None:
        async with self._mutation_lock:
            existing = await get_ssh_key_metadata(self._db, type_)
            if existing is None and not _private_key_path(self._settings, type_).exists():
                raise GatewayError(
                    ErrorCode.SSH_KEY_NOT_FOUND, f"No {type_} key present", status_code=404
                )

            self._unlink_pair(type_)
            await delete_ssh_key_metadata(self._db, type_)

    async def _atomic_write_pair(
        self, type_: SshKeyType, private_key_bytes: bytes, public_key_text: str
    ) -> None:
        priv_path = _private_key_path(self._settings, type_)
        pub_path = _public_key_path(self._settings, type_)
        parent = priv_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)

        fd, tmp_priv_name = tempfile.mkstemp(prefix=f".{type_}.", suffix=".tmp", dir=str(parent))
        tmp_priv: Path | None = Path(tmp_priv_name)
        fd2, tmp_pub_name = tempfile.mkstemp(
            prefix=f".{type_}.pub.", suffix=".tmp", dir=str(parent)
        )
        tmp_pub: Path | None = Path(tmp_pub_name)

        try:
            os.fchmod(fd, _PRIVATE_KEY_MODE)
            with os.fdopen(fd, "wb") as f:
                f.write(private_key_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.fchmod(fd2, _PUBLIC_KEY_MODE)
            with os.fdopen(fd2, "wb") as f:
                f.write(public_key_text.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_priv, priv_path)
            tmp_priv = None
            os.replace(tmp_pub, pub_path)
            tmp_pub = None

            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            for p in (tmp_priv, tmp_pub):
                if p is not None and p.exists():
                    with contextlib.suppress(OSError):
                        p.unlink()
            raise

    def _unlink_pair(self, type_: SshKeyType) -> None:
        for p in (
            _private_key_path(self._settings, type_),
            _public_key_path(self._settings, type_),
        ):
            if p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()


__all__ = ["SshKeyService"]

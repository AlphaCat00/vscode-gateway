"""Persist host-key challenges and update gateway-owned known_hosts."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

import aiosqlite

from vscode_gateway.db import (
    delete_pending_host_key,
    get_pending_host_key,
    upsert_pending_host_key,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import HostKeyChallenge, HostKeyRole, SessionId
from vscode_gateway.settings import Settings

_KNOWN_HOSTS_MODE = 0o600


def known_host_token(host: str, port: int) -> str:
    """Return the canonical OpenSSH known_hosts token for a host and port."""
    return host if port == 22 else f"[{host}]:{port}"


class HostTrustService:
    """Records pending host-key challenges and applies trust decisions."""

    def __init__(self, settings: Settings, db: aiosqlite.Connection) -> None:
        self._settings = settings
        self._db = db

    def known_hosts_path(self) -> Path:
        return self._settings.ssh_known_hosts_path

    def _existing_entry_lines(self, host: str, port: int) -> tuple[list[str], list[str]]:
        """Return ``(matching, remaining)`` lines split by host and port.

        Lines for other hosts are kept verbatim.
        """
        path = self.known_hosts_path()
        if not path.exists():
            return [], []
        text = path.read_text(encoding="utf-8")
        matching: list[str] = []
        remaining: list[str] = []
        host_token = known_host_token(host, port)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                remaining.append(line)
                continue
            parts = stripped.split()
            if len(parts) < 3:
                remaining.append(line)
                continue
            host_field = parts[0]
            host_candidates = host_field.split(",")
            if host_token in host_candidates:
                matching.append(line)
            else:
                remaining.append(line)
        return matching, remaining

    def _append_entry(self, *, host: str, port: int, algorithm: str, public_key_b64: str) -> None:
        """Append one entry line, replacing any prior matching entry."""
        _, remaining = self._existing_entry_lines(host, port)
        host_token = known_host_token(host, port)
        new_line = f"{host_token} {algorithm} {public_key_b64}\n"
        self._atomic_write(remaining, new_line)

    def _atomic_write(self, remaining_lines: list[str], new_line: str) -> None:
        path = self.known_hosts_path()
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".kh.", suffix=".tmp", dir=str(parent))
        tmp: Path | None = Path(tmp_name)
        try:
            os.fchmod(fd, _KNOWN_HOSTS_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for line in remaining_lines:
                    f.write(line if line.endswith("\n") else line + "\n")
                f.write(new_line)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            tmp = None
            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            if tmp is not None and tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
            raise

    @staticmethod
    def _public_key_b64(public_key_text: str) -> tuple[str, str]:
        """Validate the OpenSSH public-key text and return ``(algorithm, b64)``.

        ``public_key_text`` is the canonical OpenSSH form
        ``"ssh-ed25519 AAAA... [comment]"``. We strip the optional
        comment and refuse anything that does not split into at least
        ``[algorithm, base64]``.
        """
        parts = public_key_text.strip().split()
        if len(parts) < 2:
            raise GatewayError(
                ErrorCode.SSH_HOST_TRUST_MISMATCH,
                "Public key is not in OpenSSH format",
                status_code=400,
            )
        algorithm, b64 = parts[0], parts[1]
        return algorithm, b64

    async def record_challenge(
        self,
        *,
        session_id: SessionId,
        role: HostKeyRole,
        alias: str,
        host: str,
        port: int,
        algorithm: str,
        fingerprint: str,
        public_key: str,
    ) -> None:
        await upsert_pending_host_key(
            self._db,
            session_id=str(session_id),
            role=role,
            alias=alias,
            host=host,
            port=port,
            algorithm=algorithm,
            fingerprint=fingerprint,
            public_key=public_key,
        )

    async def get_challenge(self, session_id: SessionId) -> HostKeyChallenge | None:
        row = await get_pending_host_key(self._db, str(session_id))
        if row is None:
            return None
        return HostKeyChallenge(
            session_id=session_id,
            role=row["role"],  # type: ignore[arg-type]
            alias=str(row["alias"]),
            host=str(row["host"]),
            port=int(row["port"]),  # type: ignore[arg-type]
            algorithm=str(row["algorithm"]),
            fingerprint=str(row["fingerprint"]),
            public_key=str(row["public_key"]),
        )

    async def clear_challenge(self, session_id: SessionId) -> None:
        await delete_pending_host_key(self._db, str(session_id))

    async def list_challenges(self) -> list[HostKeyChallenge]:
        from vscode_gateway.db import list_pending_host_keys

        rows = await list_pending_host_keys(self._db)
        challenges: list[HostKeyChallenge] = []
        for row in rows:
            challenges.append(
                HostKeyChallenge(
                    session_id=SessionId(row["session_id"]),  # type: ignore[arg-type]
                    role=row["role"],  # type: ignore[arg-type]
                    alias=str(row["alias"]),
                    host=str(row["host"]),
                    port=int(row["port"]),  # type: ignore[arg-type]
                    algorithm=str(row["algorithm"]),
                    fingerprint=str(row["fingerprint"]),
                    public_key=str(row["public_key"]),
                )
            )
        return challenges

    async def trust(
        self,
        *,
        session_id: SessionId,
        host: str,
        port: int,
        public_key: str,
        replace: bool,
    ) -> None:
        challenge = await self.get_challenge(session_id)
        if challenge is None:
            raise GatewayError(
                ErrorCode.SSH_HOST_TRUST_MISMATCH,
                "No pending host-key challenge for this session",
                status_code=404,
            )
        if challenge.host != host or challenge.port != port:
            raise GatewayError(
                ErrorCode.SSH_HOST_TRUST_MISMATCH,
                "Submitted host/port does not match the pending challenge",
                status_code=409,
            )
        if challenge.public_key != public_key:
            raise GatewayError(
                ErrorCode.SSH_HOST_TRUST_MISMATCH,
                "Submitted public key does not match the pending challenge",
                status_code=409,
            )

        algorithm, b64 = self._public_key_b64(public_key)
        if algorithm != challenge.algorithm:
            raise GatewayError(
                ErrorCode.SSH_HOST_TRUST_MISMATCH,
                "Submitted key algorithm does not match the pending challenge",
                status_code=409,
            )
        existing, _ = self._existing_entry_lines(host, port)
        if existing and not replace:
            raise GatewayError(
                ErrorCode.SSH_HOST_TRUST_MISMATCH,
                "A different host key is already recorded for this host; "
                "submit with replace=true to overwrite",
                status_code=409,
            )

        self._append_entry(host=host, port=port, algorithm=algorithm, public_key_b64=b64)
        await self.clear_challenge(session_id)


__all__ = ["HostTrustService", "known_host_token"]

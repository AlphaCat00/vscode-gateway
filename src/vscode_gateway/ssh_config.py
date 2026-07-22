"""Validate, persist, and publish the gateway SSH configuration."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import CatalogSnapshot
from vscode_gateway.settings import Settings

ALIAS_RE = re.compile(r"^\s*Host\s+(.+)$", re.IGNORECASE | re.MULTILINE)
ALIAS_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9._\-]{0,252}")
ALIAS_MAX_LENGTH = 253
WILDCARD_CHARS = frozenset("*?[")
CONFIG_MAX_BYTES = 1_000_000
CONFIG_MAX_LINES = 10_000
CONFIG_MAX_ALIASES = 1_000

# Reject directives that execute locally or override gateway-owned trust
# and identity settings. RemoteCommand is allowed because it runs remotely.
UNSAFE_DIRECTIVES: frozenset[str] = frozenset(
    {
        "include",
        "match",
        "proxycommand",
        "localcommand",
        "permitlocalcommand",
        "localforward",
        "remoteforward",
        "dynamicforward",
        "tunnel",
        "canonicalizehostname",
        "knownhostscommand",
        "pkcs11provider",
        "securitykeyprovider",
        # Gateway-controlled — never user-editable.
        "identityfile",
        "certificatefile",
        "identityagent",
        "userknownhostsfile",
        "globalknownhostsfile",
    }
)

_CONFIG_WRITE_LOCK = asyncio.Lock()

_DIRECTIVE_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)(?:\s+|=)")


def discover_aliases(config_text: str) -> list[str]:
    seen: set[str] = set()
    aliases: list[str] = []
    for line in config_text.splitlines():
        m = ALIAS_RE.match(line)
        if m is None:
            continue
        tokens = m.group(1).strip().split()
        for token in tokens:
            token = token.strip()
            if not token or token in seen:
                continue
            if token == "*":
                continue
            if token.startswith("!"):
                continue
            if any(c in token for c in WILDCARD_CHARS):
                continue
            try:
                token.encode("utf-8")
            except UnicodeEncodeError:
                continue
            if len(token) > ALIAS_MAX_LENGTH:
                continue
            seen.add(token)
            aliases.append(token)
    return aliases


def find_unsafe_directives(text: str) -> list[str]:
    """Return the list of unsafe directives found in ``text`` (case-insensitive).

    Only directive keywords at the start of a line are inspected. Values
    (including ``Host`` tokens) are not interpreted as directives.
    """
    found: list[str] = []
    for line in text.splitlines():
        m = _DIRECTIVE_LINE_RE.match(line)
        if m is None:
            continue
        keyword = m.group(1).lower()
        if keyword in UNSAFE_DIRECTIVES:
            found.append(keyword)
    return found


def _validate_config_text(text: str) -> tuple[bytes, list[str]]:
    if "\x00" in text:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config contains NUL bytes", status_code=400
        )

    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config is not valid UTF-8", status_code=400
        ) from exc

    if len(encoded) > CONFIG_MAX_BYTES:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config exceeds size limit", status_code=400
        )

    unsafe = find_unsafe_directives(text)
    if unsafe:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID,
            f"Config contains prohibited directives: {sorted(set(unsafe))}",
            status_code=400,
        )

    lines = text.splitlines()
    if len(lines) > CONFIG_MAX_LINES:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config exceeds line limit", status_code=400
        )

    aliases = discover_aliases(text)
    if len(aliases) > CONFIG_MAX_ALIASES:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config exceeds alias count limit", status_code=400
        )

    return encoded, aliases


def compute_config_revision(config_text: str) -> str:
    return f"sha256:{hashlib.sha256(config_text.encode('utf-8')).hexdigest()}"


async def validate_and_save_config(
    settings: Settings,
    text: str,
    expected_revision: str | None = None,
) -> CatalogSnapshot:
    config_path = settings.ssh_config_path
    encoded, _ = _validate_config_text(text)
    async with _CONFIG_WRITE_LOCK:
        if expected_revision is not None and config_path.exists():
            current_text = config_path.read_text(encoding="utf-8")
            current_revision = compute_config_revision(current_text)
            if expected_revision != current_revision:
                raise GatewayError(
                    ErrorCode.CONFLICT,
                    "Config was modified; refresh and retry",
                    status_code=409,
                )

        parent = config_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GatewayError(
                ErrorCode.CONFIG_EDIT_FAILED,
                f"Cannot access config directory: {exc}",
                status_code=500,
            ) from exc

        fd, tmp_name = tempfile.mkstemp(prefix=".cfg.", suffix=".tmp", dir=str(parent))
        tmp_path: Path | None = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, config_path)
            tmp_path = None  # ownership transferred

            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            if tmp_path is not None and tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            raise

    try:
        committed_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GatewayError(
            ErrorCode.CONFIG_EDIT_FAILED,
            f"Failed to re-read committed config: {exc}",
            status_code=500,
        ) from exc

    committed_aliases = discover_aliases(committed_text)
    return CatalogSnapshot(
        revision=compute_config_revision(committed_text),
        aliases=tuple(sorted(committed_aliases)),
        loaded_at=datetime.now(UTC),
    )


class SshCatalog:
    """In-memory cache of the published Host aliases derived from the gateway config."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._snapshot: CatalogSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> CatalogSnapshot | None:
        return self._snapshot

    def set_snapshot(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot

    async def refresh(self) -> CatalogSnapshot:
        async with self._lock:
            snapshot = await self._load_catalog()
            self._snapshot = snapshot
            return snapshot

    async def _load_catalog(self) -> CatalogSnapshot:
        config_path = self._settings.ssh_config_path
        try:
            text = config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return CatalogSnapshot(
                revision=compute_config_revision(""),
                aliases=(),
                loaded_at=datetime.now(UTC),
                error=str(exc),
            )

        try:
            _, candidates = _validate_config_text(text)
        except GatewayError as exc:
            return CatalogSnapshot(
                revision=compute_config_revision(text),
                aliases=(),
                loaded_at=datetime.now(UTC),
                error=str(exc),
            )

        return CatalogSnapshot(
            revision=compute_config_revision(text),
            aliases=tuple(sorted(candidates)),
            loaded_at=datetime.now(UTC),
        )

    def is_valid_alias(self, alias: str) -> bool:
        if self._snapshot is None:
            return False
        return alias in self._snapshot.aliases


__all__ = [
    "ALIAS_MAX_LENGTH",
    "ALIAS_RE",
    "ALIAS_TOKEN_RE",
    "UNSAFE_DIRECTIVES",
    "WILDCARD_CHARS",
    "SshCatalog",
    "compute_config_revision",
    "discover_aliases",
    "find_unsafe_directives",
    "validate_and_save_config",
]

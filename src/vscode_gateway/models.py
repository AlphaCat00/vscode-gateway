from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

SshAlias = str
SessionId = uuid.UUID

SshKeyType = Literal["ed25519", "rsa", "ecdsa"]
SSH_KEY_TYPES: tuple[SshKeyType, ...] = ("ed25519", "rsa", "ecdsa")
SSH_KEY_LOAD_ORDER: tuple[SshKeyType, ...] = ("ed25519", "ecdsa", "rsa")


class SessionState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    ERROR = "error"


class SessionStage(StrEnum):
    VALIDATE = "validate"
    INSTALL = "install"
    START_REMOTE = "start_remote"
    START_TUNNEL = "start_tunnel"
    VERIFY = "verify"
    RECOVER = "recover"
    STOP = "stop"


class CloseReason(StrEnum):
    USER_REQUESTED = "user_requested"
    ALIAS_REMOVED = "alias_removed"
    DISCONNECT_GRACE_EXPIRED = "disconnect_grace_expired"
    RETRY = "retry"


@dataclass
class RuntimeIdentity:
    pid: int
    port: int
    boot_id: str
    process_start_id: str
    executable: str
    session_dir: str | None = None


@dataclass
class RuntimeCapabilities:
    platform: str
    arch: str
    helper_version: str
    available: bool


@dataclass(frozen=True)
class SshKeyMetadata:
    type: SshKeyType
    name: str
    algorithm: str
    fingerprint: str


HostKeyRole = Literal["target", "jump"]


@dataclass(frozen=True)
class HostKeyChallenge:
    session_id: SessionId
    role: HostKeyRole
    alias: SshAlias
    host: str
    port: int
    algorithm: str
    fingerprint: str
    public_key: str


@dataclass
class SessionRecord:
    id: SessionId
    alias: str
    state: SessionState
    stage: SessionStage | None = None

    remote_pid: int | None = None
    remote_port: int | None = None
    remote_boot_id: str | None = None
    remote_process_start_id: str | None = None
    remote_executable: str | None = None

    local_port: int | None = None

    connected_clients: int = 0
    last_connected_at: datetime | None = None
    last_disconnected_at: datetime | None = None
    disconnect_deadline_at: datetime | None = None

    error_code: str | None = None
    error_message: str | None = None
    close_reason: str | None = None

    ssh_host_key: HostKeyChallenge | None = None

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class WorkspaceView:
    alias: str
    state: Literal["closed", "starting", "ready", "stopping", "error"]
    session_id: SessionId | None = None
    editor_url: str | None = None
    connected_clients: int = 0
    disconnect_deadline: datetime | None = None
    stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    can_open: bool = False
    can_close: bool = False
    can_retry: bool = False
    can_force_close: bool = False
    has_remote_identity: bool = False
    catalog_missing: bool = False
    ssh_host_key: HostKeyChallenge | None = None


@dataclass(frozen=True)
class CatalogSnapshot:
    revision: str
    aliases: tuple[str, ...]
    loaded_at: datetime
    error: str | None = None


@dataclass
class SessionView:
    id: SessionId
    alias: str
    state: SessionState
    stage: SessionStage | None = None
    connected_clients: int = 0
    disconnect_deadline: datetime | None = None
    editor_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    ssh_host_key: HostKeyChallenge | None = None


class SshConfigResponse(BaseModel):
    text: str
    revision: str


class SshConfigUpdateRequest(BaseModel):
    text: str
    expected_revision: str | None = Field(default=None, alias="expectedRevision")


class KeyUploadResponse(BaseModel):
    name: str
    type: SshKeyType
    algorithm: str
    fingerprint: str
    publicKey: str  # noqa: N815 - API field name


class SshKeySlot(BaseModel):
    present: bool
    name: str | None = None
    algorithm: str | None = None
    fingerprint: str | None = None


class SshKeyInventory(BaseModel):
    keys: dict[SshKeyType, SshKeySlot]


class CatalogResponse(BaseModel):
    revision: str
    aliases: list[str]
    error: str | None = None


class VersionResponse(BaseModel):
    version: str


class RecoveryReport(BaseModel):
    recovered: int
    failed: int
    cleaned: int
    total: int
    error_sessions_remaining: int = 0
    orphaned_resources_remaining: int = 0


class TrustHostKeyRequest(BaseModel):
    alias: str
    host: str
    port: int = Field(ge=1, le=65535)
    publicKey: str  # noqa: N815 - API field name
    replace: bool = False

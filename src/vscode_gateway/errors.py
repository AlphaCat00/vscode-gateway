from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    ALIAS_NOT_FOUND = "alias_not_found"
    CAPACITY_REACHED = "capacity_reached"
    SSH_UNREACHABLE = "ssh_unreachable"
    SSH_CONFIG_INVALID = "ssh_config_invalid"
    SSH_KEY_INVALID = "ssh_key_invalid"
    SSH_KEY_EXISTS = "ssh_key_exists"
    SSH_KEY_NOT_FOUND = "ssh_key_not_found"
    SSH_NO_UPLOADED_KEYS = "ssh_no_uploaded_keys"
    SSH_NO_UPLOADED_KEY_ACCEPTED = "ssh_no_uploaded_key_accepted"
    SSH_HOST_UNKNOWN = "ssh_host_unknown"
    SSH_HOST_CHANGED = "ssh_host_changed"
    SSH_HOST_TRUST_MISMATCH = "ssh_host_trust_mismatch"
    REMOTE_UNSUPPORTED = "remote_unsupported"
    RUNTIME_DOWNLOAD_FAILED = "runtime_download_failed"
    RUNTIME_DIGEST_MISMATCH = "runtime_digest_mismatch"
    RUNTIME_INSTALL_FAILED = "runtime_install_failed"
    REMOTE_START_FAILED = "remote_start_failed"
    REMOTE_IDENTITY_CONFLICT = "remote_identity_conflict"
    TUNNEL_START_FAILED = "tunnel_start_failed"
    TUNNEL_LOST = "tunnel_lost"
    EDITOR_UNHEALTHY = "editor_unhealthy"
    STARTUP_TIMEOUT = "startup_timeout"
    STOP_FAILED = "stop_failed"
    RECOVERY_FAILED = "recovery_failed"
    INTERNAL_ERROR = "internal_error"
    CONFLICT = "conflict"
    AUTHENTICATION_REQUIRED = "authentication_required"
    INVALID_CSRF = "invalid_csrf"
    CONFIG_EDIT_FAILED = "config_edit_failed"
    UNAUTHORIZED = "unauthorized"


class GatewayError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str = "",
        status_code: int = 500,
        detail: str = "",
    ) -> None:
        self.code = code
        self.safe_message = message
        self.status_code = status_code
        self.detail = detail
        super().__init__(message)

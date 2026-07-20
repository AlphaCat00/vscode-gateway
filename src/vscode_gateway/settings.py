from __future__ import annotations

import os
import secrets
from contextlib import suppress
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="VSC_GATEWAY_",
        extra="forbid",
    )

    canonical_origin: str = Field(
        default="http://localhost:8000",
        description="Canonical public origin for the gateway",
    )
    bind_host: str = Field(default="127.0.0.1")
    bind_port: int = Field(default=8000, ge=1, le=65535)

    state_dir: Path = Field(default=Path("state"))
    runtime_dir: Path = Field(default=Path("runtime"))

    ssh_config_path: Path = Field(default=Path("config/ssh_config"))
    ssh_keys_dir: Path = Field(default=Path("config/keys"))

    password_hash_path: Path = Field(default=Path("state/password.hash"))
    session_secret_path: Path = Field(default=Path("state/session.secret"))

    ssh_executable: str = Field(default="ssh")
    scp_executable: str = Field(default="scp")
    ssh_keygen_executable: str = Field(default="ssh-keygen")

    openvscode_version: str = Field(default="1.89.1")
    openvscode_linux_x64_url: str = Field(
        default="https://github.com/gitpod-io/openvscode-server/releases/download/openvscode-server-v1.89.1/openvscode-server-v1.89.1-linux-x64.tar.gz"
    )
    openvscode_linux_x64_sha256: str = Field(
        default="e5e027e38c058d24d9b0244c9ab28e7600809e20b7d396680ddb5663d563a995"
    )
    openvscode_linux_arm64_url: str = Field(default="")
    openvscode_linux_arm64_sha256: str = Field(default="")

    session_capacity: int = Field(default=10, ge=1)

    startup_timeout: float = Field(default=120.0, ge=5.0)
    stop_timeout: float = Field(default=60.0, ge=5.0)
    proxy_read_timeout: float = Field(default=300.0, ge=5.0)
    proxy_connect_timeout: float = Field(default=30.0, ge=1.0)
    subprocess_timeout: float = Field(default=60.0, ge=5.0)
    ssh_connect_timeout: float = Field(default=15.0, ge=1.0)

    disconnect_grace_period: float = Field(default=300.0, ge=0.0)

    trusted_proxy_networks: list[str] = Field(default_factory=lambda: ["127.0.0.0/8", "::1"])

    allowed_hostnames: list[str] = Field(default_factory=list)

    session_max_age_seconds: int = Field(default=86400, ge=60)

    secure_cookies: bool = Field(
        default=False,
        description=(
            "Force Secure/HttpOnly/SameSite on the session cookie. "
            "Requires an HTTPS canonical origin."
        ),
    )

    login_max_attempts: int = Field(default=5, ge=1)
    login_window_seconds: float = Field(default=60.0, ge=1.0)
    login_lockout_seconds: float = Field(default=300.0, ge=0.0)

    recovery_timeout: float = Field(default=60.0, ge=5.0)

    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")

    @model_validator(mode="after")
    def _ensure_state_dirs(self) -> Settings:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.ssh_keys_dir.mkdir(parents=True, exist_ok=True)
        self.ssh_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.ssh_config_path.touch(exist_ok=True)
        return self

    @property
    def session_secret(self) -> str:
        """Hex-encoded SessionMiddleware signing secret.

        The secret is stored on disk as a hex string so the file can be
        inspected and edited without the lossy text decoding that random
        bytes would require (HI-05, Plan §17.2). ``bytes.fromhex`` is the
        only parser used; malformed or short material fails loudly at
        startup rather than silently weakening the signer.
        """
        return self.session_secret_hex

    @property
    def session_secret_bytes(self) -> bytes:
        """Raw signing-key material as ``bytes`` (``>= 32`` bytes)."""
        return bytes.fromhex(self.session_secret_hex)

    @property
    def session_secret_hex(self) -> str:
        path = self.session_secret_path
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            secret = secrets.token_bytes(64)
            hex_text = secret.hex()
            path.write_text(hex_text, encoding="utf-8")
            with suppress(OSError):
                os.chmod(path, 0o600)
            return hex_text
        if not text:
            msg = f"Session secret at {path} is empty; regenerate the file (HI-05)."
            raise ValueError(msg)
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            msg = f"Session secret at {path} is not valid hex; regenerate the file (HI-05)."
            raise ValueError(msg) from exc
        if len(raw) < 32:
            msg = (
                f"Session secret at {path} is {len(raw)} bytes; minimum 32 bytes required (HI-05)."
            )
            raise ValueError(msg)
        return text

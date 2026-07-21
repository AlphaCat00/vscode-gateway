"""Focused tests for the gateway-owned SSH configuration service."""

from __future__ import annotations

from pathlib import Path
from stat import S_IMODE

import pytest

from tests.unit.ssh_backend_test_helpers import make_settings
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import (
    compute_config_revision,
    discover_aliases,
    find_unsafe_directives,
    validate_and_save_config,
)


def test_remote_command_is_allowed() -> None:
    config = """
Host production
    HostName production.example.test
    User developer
    RemoteCommand cd /workspace
"""

    assert find_unsafe_directives(config) == []


def test_ssh_paths_derive_from_overridden_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / "custom-state"

    settings = Settings(
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        password_hash_path=state_dir / "password.hash",
        session_secret_path=state_dir / "session.secret",
    )

    assert settings.ssh_dir == state_dir / "ssh"
    assert settings.ssh_config_path == state_dir / "ssh" / "config"
    assert settings.ssh_known_hosts_path == state_dir / "ssh" / "known_hosts"
    assert settings.ssh_keys_dir == state_dir / "ssh" / "keys"


@pytest.mark.parametrize(
    "directive",
    [
        "IdentityFile ~/.ssh/id_ed25519",
        "CertificateFile ~/.ssh/id_ed25519-cert.pub",
        "IdentityAgent SSH_AUTH_SOCK",
        "UserKnownHostsFile ~/.ssh/known_hosts",
        "GlobalKnownHostsFile ~/.ssh/known_hosts",
        "ProxyCommand ssh gateway %h",
        "Include ~/.ssh/conf.d/*",
        "LocalCommand touch /tmp/gateway-test",
    ],
)
def test_gateway_owned_and_command_directives_are_rejected(directive: str) -> None:
    found = find_unsafe_directives(f"Host production\n    {directive}\n")

    assert found


@pytest.mark.asyncio
async def test_rejected_directive_does_not_replace_config(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    original = "Host production\n    HostName production.example.test\n"
    await validate_and_save_config(settings, original)

    with pytest.raises(GatewayError) as exc_info:
        await validate_and_save_config(
            settings,
            "Host production\n    Include /etc/ssh/ssh_config\n",
        )

    assert exc_info.value.code == ErrorCode.SSH_CONFIG_INVALID
    assert settings.ssh_config_path.read_text(encoding="utf-8") == original


def test_alias_discovery_keeps_only_positive_literal_tokens() -> None:
    config = """
# Host commented-out
Host * !excluded literal-one literal-one
    HostName ignored.example.test
Host server-1 server_2
Host wildcard* question? bracket[1] !also-excluded
Host
"""

    assert discover_aliases(config) == ["literal-one", "server-1", "server_2"]


@pytest.mark.asyncio
async def test_save_is_atomic_and_revision_conflicts_preserve_active_bytes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first = "Host first\n    HostName first.example.test\n"
    first_snapshot = await validate_and_save_config(settings, first)

    assert settings.ssh_config_path.read_text(encoding="utf-8") == first
    assert first_snapshot.revision == compute_config_revision(first)
    assert S_IMODE(settings.ssh_config_path.stat().st_mode) == 0o600

    second = "Host second\n    HostName second.example.test\n"
    saved = await validate_and_save_config(settings, second, first_snapshot.revision)
    assert saved.revision == compute_config_revision(second)
    assert settings.ssh_config_path.read_bytes() == second.encode()

    with pytest.raises(GatewayError) as exc_info:
        await validate_and_save_config(settings, first, first_snapshot.revision)

    assert exc_info.value.code == ErrorCode.CONFLICT
    assert exc_info.value.status_code == 409
    assert settings.ssh_config_path.read_bytes() == second.encode()


@pytest.mark.asyncio
async def test_failed_atomic_replace_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    original = "Host stable\n    HostName stable.example.test\n"
    await validate_and_save_config(settings, original)

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("vscode_gateway.ssh_config.os.replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        await validate_and_save_config(settings, "Host changed\n")

    assert settings.ssh_config_path.read_text(encoding="utf-8") == original
    assert list(settings.ssh_config_path.parent.glob(".cfg.*.tmp")) == []

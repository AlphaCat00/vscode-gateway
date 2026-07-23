"""API integration coverage over a real localhost OpenSSH connection."""

# pyright: reportUnknownMemberType=false

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import pwd
import re
import shutil
import signal
import socket
import tarfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import asyncssh
import httpx
import pytest

from vscode_gateway import runtime as runtime_module
from vscode_gateway.app import create_app, lifespan
from vscode_gateway.auth import hash_password

pytestmark = pytest.mark.integration

_ALIAS = "localhost-integration"
_PROXY_ALIAS = "localhost-proxy-target"
_JUMP_ALIAS = "localhost-proxy-jump"
_PASSWORD = "integration-password"
_EDITOR_SCRIPT = b"""#!/usr/bin/env python3
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--server-base-path", required=True)
args, _ = parser.parse_known_args()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        status = 200 if self.path.startswith(args.server_base_path) else 404
        cookie = self.headers.get("Cookie", "")
        body = json.dumps(
            {
                "path": self.path,
                "forwardedPrefix": self.headers.get("X-Forwarded-Prefix"),
                "gatewayCookieForwarded": "gateway_session=" in cookie,
            }
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
print(f"Server bound to 127.0.0.1:{server.server_port}", flush=True)
server.serve_forever()
"""


@dataclass(frozen=True)
class LocalSshServer:
    port: int
    username: str
    user_key: asyncssh.SSHKey
    remote_state_dir: Path
    log_path: Path


@dataclass(frozen=True)
class GatewayApi:
    client: httpx.AsyncClient
    ssh: LocalSshServer
    real_editor: bool


@dataclass(frozen=True)
class RuntimeArtifact:
    runtime_dir: Path
    version: str
    url: str
    digest: str
    real_editor: bool


def _generate_key(algorithm: str) -> asyncssh.SSHKey:
    generator = cast(Callable[[str], asyncssh.SSHKey], asyncssh.generate_private_key)
    return generator(algorithm)


def _sshd_path() -> Path | None:
    discovered = shutil.which("sshd")
    if discovered is not None:
        return Path(discovered)
    standard_path = Path("/usr/sbin/sshd")
    return standard_path if standard_path.is_file() else None


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


async def _wait_for_tcp_server(
    process: asyncio.subprocess.Process,
    port: int,
    log_path: Path,
) -> None:
    deadline = asyncio.get_running_loop().time() + 10.0
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace")
            pytest.fail(f"sshd exited during startup ({process.returncode}):\n{log}")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    pytest.fail(f"sshd did not listen on 127.0.0.1:{port}")


async def _wait_for_log_entry(log_path: Path, text: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if text in log_path.read_text(encoding="utf-8", errors="replace"):
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"sshd log did not contain {text!r}:\n{log_path.read_text(errors='replace')}")


async def _terminate_subprocess(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        process.kill()
        await process.wait()


def _same_process(pid: int, expected_start_id: str) -> bool:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return False
    return len(fields) > 21 and fields[21] == expected_start_id


def _matching_process_ids(command_fragment: str) -> list[int]:
    matches: list[int] = []
    for proc_path in Path("/proc").iterdir():
        if not proc_path.name.isdigit():
            continue
        try:
            command = proc_path.joinpath("cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if command_fragment.encode("utf-8") in command:
            matches.append(int(proc_path.name))
    return matches


async def _wait_for_process_absence(command_fragment: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if not _matching_process_ids(command_fragment):
            return
        await asyncio.sleep(0.05)
    pytest.fail(
        f"processes still reference {command_fragment!r}: {_matching_process_ids(command_fragment)}"
    )


async def _stop_leftover_editors(remote_state_dir: Path) -> None:
    for pid_path in remote_state_dir.glob("sessions/*/pid"):
        start_path = pid_path.with_name("proc_start_id")
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            start_id = start_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            continue
        if _same_process(pid, start_id):
            os.kill(pid, signal.SIGTERM)

    await asyncio.sleep(0.1)

    for pid_path in remote_state_dir.glob("sessions/*/pid"):
        start_path = pid_path.with_name("proc_start_id")
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            start_id = start_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            continue
        if _same_process(pid, start_id):
            os.kill(pid, signal.SIGKILL)


@pytest.fixture
async def localhost_ssh_server(tmp_path: Path) -> AsyncIterator[LocalSshServer]:
    sshd = _sshd_path()
    if sshd is None:
        pytest.skip("OpenSSH sshd is required for localhost integration tests")
    if not Path("/run/sshd").is_dir():
        pytest.skip("OpenSSH privilege-separation directory /run/sshd is unavailable")
    for command in ("pgrep", "setsid", "sha256sum", "ss", "tar"):
        if shutil.which(command) is None:
            pytest.skip(f"remote helper dependency {command!r} is unavailable")

    server_dir = tmp_path / "sshd"
    server_dir.mkdir(mode=0o700)
    remote_state_dir = tmp_path / "remote-state"
    remote_state_dir.mkdir(mode=0o700)

    username = pwd.getpwuid(os.geteuid()).pw_name

    host_key = _generate_key("ssh-ed25519")
    host_key_path = server_dir / "host_key"
    host_key_path.write_bytes(host_key.export_private_key("openssh"))
    host_key_path.chmod(0o600)

    user_key = _generate_key("ssh-ed25519")
    authorized_keys_path = server_dir / "authorized_keys"
    authorized_keys_path.write_bytes(user_key.export_public_key("openssh"))
    authorized_keys_path.chmod(0o600)

    port = _unused_loopback_port()
    config_path = server_dir / "sshd_config"
    log_path = server_dir / "sshd.log"
    config_path.write_text(
        "\n".join(
            (
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                "AddressFamily inet",
                f"HostKey {host_key_path}",
                f"PidFile {server_dir / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized_keys_path}",
                f"AllowUsers {username}",
                "AuthenticationMethods publickey",
                "PubkeyAuthentication yes",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "UsePAM no",
                "PermitRootLogin prohibit-password",
                "StrictModes no",
                "AllowTcpForwarding yes",
                "GatewayPorts no",
                "X11Forwarding no",
                "PermitTTY no",
                "AcceptEnv GATEWAY_STATE_DIR",
                "Subsystem sftp internal-sftp",
                "LogLevel ERROR",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    process = await asyncio.create_subprocess_exec(
        str(sshd),
        "-D",
        "-e",
        "-f",
        str(config_path),
        "-E",
        str(log_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await _wait_for_tcp_server(process, port, log_path)
    try:
        yield LocalSshServer(
            port=port,
            username=username,
            user_key=user_key,
            remote_state_dir=remote_state_dir,
            log_path=log_path,
        )
    finally:
        await _stop_leftover_editors(remote_state_dir)
        await _terminate_subprocess(process)


@pytest.fixture
async def localhost_jump_server(
    tmp_path: Path,
    localhost_ssh_server: LocalSshServer,
) -> AsyncIterator[LocalSshServer]:
    sshd = _sshd_path()
    if sshd is None:
        pytest.skip("OpenSSH sshd is required for localhost integration tests")

    server_dir = tmp_path / "sshd-jump"
    server_dir.mkdir(mode=0o700)
    remote_state_dir = tmp_path / "jump-remote-state"
    remote_state_dir.mkdir(mode=0o700)

    host_key = _generate_key("ssh-ed25519")
    host_key_path = server_dir / "host_key"
    host_key_path.write_bytes(host_key.export_private_key("openssh"))
    host_key_path.chmod(0o600)

    authorized_keys_path = server_dir / "authorized_keys"
    authorized_keys_path.write_bytes(localhost_ssh_server.user_key.export_public_key("openssh"))
    authorized_keys_path.chmod(0o600)

    port = _unused_loopback_port()
    config_path = server_dir / "sshd_config"
    log_path = server_dir / "sshd.log"
    config_path.write_text(
        "\n".join(
            (
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                "AddressFamily inet",
                f"HostKey {host_key_path}",
                f"PidFile {server_dir / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized_keys_path}",
                f"AllowUsers {localhost_ssh_server.username}",
                "AuthenticationMethods publickey",
                "PubkeyAuthentication yes",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "UsePAM no",
                "PermitRootLogin prohibit-password",
                "StrictModes no",
                "AllowTcpForwarding yes",
                "GatewayPorts no",
                "X11Forwarding no",
                "PermitTTY no",
                "Subsystem sftp internal-sftp",
                "LogLevel DEBUG1",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    process = await asyncio.create_subprocess_exec(
        str(sshd),
        "-D",
        "-e",
        "-f",
        str(config_path),
        "-E",
        str(log_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await _wait_for_tcp_server(process, port, log_path)
    try:
        yield LocalSshServer(
            port=port,
            username=localhost_ssh_server.username,
            user_key=localhost_ssh_server.user_key,
            remote_state_dir=remote_state_dir,
            log_path=log_path,
        )
    finally:
        await _stop_leftover_editors(remote_state_dir)
        await _terminate_subprocess(process)


def _install_synthetic_runtime(runtime_dir: Path) -> str:
    artifacts_dir = runtime_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    source_archive = runtime_dir / "integration-editor.tar.gz"
    with tarfile.open(source_archive, mode="w:gz") as archive:
        entry = tarfile.TarInfo("openvscode-integration/bin/openvscode-server")
        entry.mode = 0o755
        entry.mtime = 0
        entry.size = len(_EDITOR_SCRIPT)
        archive.addfile(entry, io.BytesIO(_EDITOR_SCRIPT))

    digest = hashlib.sha256(source_archive.read_bytes()).hexdigest()
    source_archive.replace(artifacts_dir / f"{digest}.tar.gz")
    return digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_digest(value: str) -> str:
    digest = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        pytest.fail("VSC_GATEWAY_TEST_OPENVSCODE_SHA256 must be 64 hexadecimal characters")
    return digest


@pytest.fixture(
    params=(
        pytest.param("synthetic", id="synthetic-editor"),
        pytest.param("real", id="real-editor", marks=pytest.mark.real_editor),
    )
)
def runtime_artifact(request: pytest.FixtureRequest, tmp_path: Path) -> RuntimeArtifact:
    runtime_dir = tmp_path / "gateway-runtime"
    mode = cast(str, request.param)
    if mode == "synthetic":
        digest = _install_synthetic_runtime(runtime_dir)
        return RuntimeArtifact(
            runtime_dir=runtime_dir,
            version="integration-test",
            url="https://integration.invalid/editor.tgz",
            digest=digest,
            real_editor=False,
        )

    archive_value = os.environ.get("VSC_GATEWAY_TEST_OPENVSCODE_ARCHIVE", "").strip()
    url = os.environ.get("VSC_GATEWAY_TEST_OPENVSCODE_URL", "").strip()
    digest_value = os.environ.get("VSC_GATEWAY_TEST_OPENVSCODE_SHA256", "").strip()
    version = os.environ.get("VSC_GATEWAY_TEST_OPENVSCODE_VERSION", "integration-real").strip()
    if not version:
        pytest.fail("VSC_GATEWAY_TEST_OPENVSCODE_VERSION must not be empty")

    if archive_value:
        archive_path = Path(archive_value).expanduser().resolve()
        if not archive_path.is_file():
            pytest.fail(f"OpenVSCode integration archive does not exist: {archive_path}")
        computed_digest = _sha256_file(archive_path)
        if digest_value and _validated_digest(digest_value) != computed_digest:
            pytest.fail("OpenVSCode integration archive does not match the configured SHA-256")
        digest = computed_digest
        artifacts_dir = runtime_dir / "artifacts"
        artifacts_dir.mkdir(parents=True)
        shutil.copyfile(archive_path, artifacts_dir / f"{digest}.tar.gz")
        url = url or "https://integration.invalid/preloaded-editor.tgz"
    elif url and digest_value:
        digest = _validated_digest(digest_value)
    else:
        pytest.skip(
            "real editor integration requires VSC_GATEWAY_TEST_OPENVSCODE_ARCHIVE "
            "or VSC_GATEWAY_TEST_OPENVSCODE_URL plus VSC_GATEWAY_TEST_OPENVSCODE_SHA256"
        )

    return RuntimeArtifact(
        runtime_dir=runtime_dir,
        version=version,
        url=url,
        digest=digest,
        real_editor=True,
    )


@pytest.fixture
async def gateway_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_artifact: RuntimeArtifact,
    localhost_ssh_server: LocalSshServer,
) -> AsyncIterator[GatewayApi]:
    state_dir = tmp_path / "gateway-state"
    ssh_dir = state_dir / "ssh"

    environment = {
        "VSC_GATEWAY_CANONICAL_ORIGIN": "http://testserver",
        "VSC_GATEWAY_STATE_DIR": str(state_dir),
        "VSC_GATEWAY_RUNTIME_DIR": str(runtime_artifact.runtime_dir),
        "VSC_GATEWAY_SSH_DIR": str(ssh_dir),
        "VSC_GATEWAY_SSH_CONFIG_PATH": str(ssh_dir / "config"),
        "VSC_GATEWAY_SSH_KNOWN_HOSTS_PATH": str(ssh_dir / "known_hosts"),
        "VSC_GATEWAY_SSH_KEYS_DIR": str(ssh_dir / "keys"),
        "VSC_GATEWAY_PASSWORD_HASH_PATH": str(state_dir / "password.hash"),
        "VSC_GATEWAY_SESSION_SECRET_PATH": str(state_dir / "session.secret"),
        "VSC_GATEWAY_OPENVSCODE_VERSION": runtime_artifact.version,
        "VSC_GATEWAY_OPENVSCODE_LINUX_X64_URL": runtime_artifact.url,
        "VSC_GATEWAY_OPENVSCODE_LINUX_X64_SHA256": runtime_artifact.digest,
        "VSC_GATEWAY_OPENVSCODE_LINUX_ARM64_URL": runtime_artifact.url,
        "VSC_GATEWAY_OPENVSCODE_LINUX_ARM64_SHA256": runtime_artifact.digest,
        "VSC_GATEWAY_DISCONNECT_GRACE_PERIOD": "60",
        "VSC_GATEWAY_LOG_LEVEL": "ERROR",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    helper_path = tmp_path / "remote-helper.sh"
    monkeypatch.setattr(runtime_module, "HELPER_PATH", str(helper_path))

    app = create_app()
    app.state.settings.password_hash_path.write_text(hash_password(_PASSWORD), encoding="utf-8")

    async with lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            follow_redirects=False,
        ) as client:
            yield GatewayApi(
                client=client,
                ssh=localhost_ssh_server,
                real_editor=runtime_artifact.real_editor,
            )


def _login_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if match is None:
        raise AssertionError("login page did not contain a CSRF token")
    return match.group(1)


def _authenticated_csrf(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if match is None:
        raise AssertionError("authenticated page did not contain a CSRF token")
    return match.group(1)


async def _wait_for_workspace_state(
    client: httpx.AsyncClient,
    alias: str,
    state: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = await client.get("/api/sessions")
        assert response.status_code == 200
        payload = cast(dict[str, Any], response.json())
        workspaces = cast(list[dict[str, Any]], payload["workspaces"])
        latest = next((item for item in workspaces if item["alias"] == alias), None)
        if latest is not None and latest["state"] == state:
            return latest
        await asyncio.sleep(0.1)
    raise AssertionError(f"workspace {alias!r} did not reach {state!r}; latest={latest!r}")


async def test_api_session_lifecycle_over_real_localhost_ssh(gateway_api: GatewayApi) -> None:
    client = gateway_api.client
    ssh = gateway_api.ssh

    assert (await client.get("/healthz")).status_code == 200
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["phase"] == "ready"

    for page_path in ("/", "/settings/ssh", "/settings/keys"):
        page_redirect = await client.get(page_path)
        assert page_redirect.status_code == 303
        assert page_redirect.headers["location"] == "/login"

    unauthorized = await client.get("/api/sessions")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["content-type"].startswith("application/problem+json")

    login_page = await client.get("/login")
    login = await client.post(
        "/login",
        data={"password": _PASSWORD, "csrf_token": _login_csrf(login_page.text)},
    )
    assert login.status_code == 303
    csrf = _authenticated_csrf((await client.get("/")).text)

    current_config = await client.get("/api/ssh/config")
    assert current_config.status_code == 200
    config_payload = cast(dict[str, str], current_config.json())
    ssh_config = "\n".join(
        (
            f"Host {_ALIAS}",
            "    HostName 127.0.0.1",
            f"    Port {ssh.port}",
            f"    User {ssh.username}",
            f"    SetEnv GATEWAY_STATE_DIR={ssh.remote_state_dir}",
            "",
        )
    )
    saved_config = await client.put(
        "/api/ssh/config",
        headers={"X-CSRF-Token": csrf},
        json={"text": ssh_config, "expectedRevision": config_payload["revision"]},
    )
    assert saved_config.status_code == 200
    catalog = await client.get("/api/ssh/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["aliases"] == [_ALIAS]

    empty_keys = await client.get("/api/ssh/keys")
    assert empty_keys.status_code == 200
    assert all(not slot["present"] for slot in empty_keys.json()["keys"].values())
    uploaded = await client.post(
        "/api/ssh/keys",
        headers={"X-CSRF-Token": csrf},
        data={"name": "localhost integration key"},
        files={
            "private_key": (
                "localhost-key",
                ssh.user_key.export_private_key("openssh"),
                "application/octet-stream",
            )
        },
    )
    assert uploaded.status_code == 201
    assert uploaded.json()["fingerprint"] == ssh.user_key.get_fingerprint("sha256")

    opened = await client.post(
        f"/api/sessions/{_ALIAS}/open",
        headers={"X-CSRF-Token": csrf},
    )
    assert opened.status_code == 202
    first_session_id = opened.json()["session_id"]

    host_error = await _wait_for_workspace_state(client, _ALIAS, "error")
    assert host_error["errorCode"] == "ssh_host_unknown"
    challenge = cast(dict[str, Any], host_error["sshHostKey"])
    assert challenge["port"] == ssh.port
    assert challenge["algorithm"] == "ssh-ed25519"

    trusted = await client.post(
        "/api/ssh/hosts/trust",
        headers={"X-CSRF-Token": csrf},
        json={
            "alias": _ALIAS,
            "host": challenge["host"],
            "port": challenge["port"],
            "publicKey": challenge["publicKey"],
            "replace": False,
        },
    )
    assert trusted.status_code == 204

    retried = await client.post(
        f"/api/sessions/{_ALIAS}/retry",
        headers={"X-CSRF-Token": csrf},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["session_id"] != first_session_id

    ready_timeout = 180.0 if gateway_api.real_editor else 30.0
    workspace = await _wait_for_workspace_state(
        client,
        _ALIAS,
        "ready",
        timeout=ready_timeout,
    )
    editor_url = cast(str, workspace["editorUrl"])
    proxied = await client.get(editor_url)
    assert proxied.status_code == 200
    if gateway_api.real_editor:
        assert proxied.headers["content-type"].startswith("text/html")
        assert len(proxied.content) > 1000
    else:
        proxy_payload = cast(dict[str, Any], proxied.json())
        assert proxy_payload == {
            "path": editor_url.rstrip("/"),
            "forwardedPrefix": editor_url.rstrip("/"),
            "gatewayCookieForwarded": False,
        }

    session = await client.get(f"/api/sessions/{_ALIAS}")
    assert session.status_code == 200
    assert session.json()["state"] == "ready"
    remote_session_id = cast(str, session.json()["id"])
    remote_session_dir = ssh.remote_state_dir.joinpath("sessions", remote_session_id)
    profile_id = hashlib.sha256(_ALIAS.encode("utf-8")).hexdigest()
    profile_dir = ssh.remote_state_dir.joinpath("profiles", profile_id)
    assert remote_session_dir.is_dir()
    assert remote_session_dir.joinpath("logs").is_dir()
    assert not remote_session_dir.joinpath("user-data").exists()
    assert not remote_session_dir.joinpath("server-data").exists()
    assert profile_dir.joinpath("user-data").is_dir()
    assert profile_dir.joinpath("server-data").is_dir()
    user_marker = profile_dir / "user-data" / "gateway-profile-marker"
    server_marker = profile_dir / "server-data" / "gateway-profile-marker"
    user_marker.write_text("persistent user data", encoding="utf-8")
    server_marker.write_text("persistent server data", encoding="utf-8")
    assert _matching_process_ids(remote_session_id)

    closed = await client.post(
        f"/api/sessions/{_ALIAS}/close",
        headers={"X-CSRF-Token": csrf},
    )
    assert closed.status_code == 204
    await _wait_for_workspace_state(client, _ALIAS, "closed")
    await _wait_for_process_absence(remote_session_id)
    assert not remote_session_dir.exists()
    assert user_marker.read_text(encoding="utf-8") == "persistent user data"
    assert server_marker.read_text(encoding="utf-8") == "persistent server data"

    reopened = await client.post(
        f"/api/sessions/{_ALIAS}/open",
        headers={"X-CSRF-Token": csrf},
    )
    assert reopened.status_code == 202
    reopened_session_id = cast(str, reopened.json()["session_id"])
    assert reopened_session_id != remote_session_id
    await _wait_for_workspace_state(client, _ALIAS, "ready", timeout=ready_timeout)
    assert user_marker.read_text(encoding="utf-8") == "persistent user data"
    assert server_marker.read_text(encoding="utf-8") == "persistent server data"

    reclosed = await client.post(
        f"/api/sessions/{_ALIAS}/close",
        headers={"X-CSRF-Token": csrf},
    )
    assert reclosed.status_code == 204
    await _wait_for_workspace_state(client, _ALIAS, "closed")
    await _wait_for_process_absence(reopened_session_id)
    assert profile_dir.is_dir()

    deleted = await client.delete(
        "/api/ssh/keys/ed25519",
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 204
    assert (await client.get("/api/ssh/keys")).json() == empty_keys.json()

    logout = await client.post("/logout", data={"csrf_token": csrf})
    assert logout.status_code == 303
    assert (await client.get("/api/sessions")).status_code == 401


@pytest.mark.parametrize("runtime_artifact", ["synthetic"], indirect=True)
async def test_api_proxy_jump_lifecycle_over_real_localhost_ssh(
    localhost_jump_server: LocalSshServer,
    gateway_api: GatewayApi,
) -> None:
    client = gateway_api.client
    target = gateway_api.ssh
    jump = localhost_jump_server

    login_page = await client.get("/login")
    login = await client.post(
        "/login",
        data={"password": _PASSWORD, "csrf_token": _login_csrf(login_page.text)},
    )
    assert login.status_code == 303
    csrf = _authenticated_csrf((await client.get("/")).text)

    current_config = await client.get("/api/ssh/config")
    assert current_config.status_code == 200
    config_payload = cast(dict[str, str], current_config.json())
    ssh_config = "\n".join(
        (
            f"Host {_PROXY_ALIAS}",
            "    HostName 127.0.0.1",
            f"    Port {target.port}",
            f"    User {target.username}",
            f"    ProxyJump {_JUMP_ALIAS}",
            f"    SetEnv GATEWAY_STATE_DIR={target.remote_state_dir}",
            "",
            f"Host {_JUMP_ALIAS}",
            "    HostName 127.0.0.1",
            f"    Port {jump.port}",
            f"    User {jump.username}",
            "",
        )
    )
    saved_config = await client.put(
        "/api/ssh/config",
        headers={"X-CSRF-Token": csrf},
        json={"text": ssh_config, "expectedRevision": config_payload["revision"]},
    )
    assert saved_config.status_code == 200
    catalog = await client.get("/api/ssh/catalog")
    assert catalog.status_code == 200
    assert catalog.json()["aliases"] == sorted([_JUMP_ALIAS, _PROXY_ALIAS])

    uploaded = await client.post(
        "/api/ssh/keys",
        headers={"X-CSRF-Token": csrf},
        data={"name": "localhost proxy integration key"},
        files={
            "private_key": (
                "localhost-proxy-key",
                target.user_key.export_private_key("openssh"),
                "application/octet-stream",
            )
        },
    )
    assert uploaded.status_code == 201
    assert uploaded.json()["fingerprint"] == target.user_key.get_fingerprint("sha256")

    opened = await client.post(
        f"/api/sessions/{_PROXY_ALIAS}/open",
        headers={"X-CSRF-Token": csrf},
    )
    assert opened.status_code == 202
    first_session_id = opened.json()["session_id"]

    jump_error = await _wait_for_workspace_state(client, _PROXY_ALIAS, "error")
    assert jump_error["errorCode"] == "ssh_host_unknown"
    jump_challenge = cast(dict[str, Any], jump_error["sshHostKey"])
    assert jump_challenge["role"] == "jump"
    assert jump_challenge["port"] == jump.port

    trusted_jump = await client.post(
        "/api/ssh/hosts/trust",
        headers={"X-CSRF-Token": csrf},
        json={
            "alias": _PROXY_ALIAS,
            "host": jump_challenge["host"],
            "port": jump_challenge["port"],
            "publicKey": jump_challenge["publicKey"],
            "replace": False,
        },
    )
    assert trusted_jump.status_code == 204

    retried_target = await client.post(
        f"/api/sessions/{_PROXY_ALIAS}/retry",
        headers={"X-CSRF-Token": csrf},
    )
    assert retried_target.status_code == 202, retried_target.text
    second_session_id = retried_target.json()["session_id"]
    assert second_session_id != first_session_id

    target_error = await _wait_for_workspace_state(client, _PROXY_ALIAS, "error")
    assert target_error["errorCode"] == "ssh_host_unknown"
    target_challenge = cast(dict[str, Any], target_error["sshHostKey"])
    assert target_challenge["role"] == "target"
    assert target_challenge["port"] == target.port

    trusted_target = await client.post(
        "/api/ssh/hosts/trust",
        headers={"X-CSRF-Token": csrf},
        json={
            "alias": _PROXY_ALIAS,
            "host": target_challenge["host"],
            "port": target_challenge["port"],
            "publicKey": target_challenge["publicKey"],
            "replace": False,
        },
    )
    assert trusted_target.status_code == 204

    retried_ready = await client.post(
        f"/api/sessions/{_PROXY_ALIAS}/retry",
        headers={"X-CSRF-Token": csrf},
    )
    assert retried_ready.status_code == 202, retried_ready.text
    third_session_id = retried_ready.json()["session_id"]
    assert third_session_id != second_session_id

    workspace = await _wait_for_workspace_state(client, _PROXY_ALIAS, "ready")
    editor_url = cast(str, workspace["editorUrl"])
    proxied = await client.get(editor_url)
    assert proxied.status_code == 200
    proxy_payload = cast(dict[str, Any], proxied.json())
    assert proxy_payload == {
        "path": editor_url.rstrip("/"),
        "forwardedPrefix": editor_url.rstrip("/"),
        "gatewayCookieForwarded": False,
    }
    await _wait_for_log_entry(jump.log_path, "direct-tcpip")

    closed = await client.post(
        f"/api/sessions/{_PROXY_ALIAS}/close",
        headers={"X-CSRF-Token": csrf},
    )
    assert closed.status_code == 204
    await _wait_for_workspace_state(client, _PROXY_ALIAS, "closed")

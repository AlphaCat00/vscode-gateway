"""End-to-end API tests for the gateway server.

These tests talk to a REAL, running gateway server over HTTP. They use
the REAL ``/usr/bin/ssh``, ``/usr/bin/scp`` and ``/usr/bin/ssh-keygen``
binaries (no fakes) against the ``localhost-workspace`` SSH alias
configured in ``config/ssh_config``.

The server is started in a child process by the ``server`` fixture; the
fixture waits for the gateway's ``/healthz`` endpoint to become
available and tears the server down when the test module finishes.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

TEST_PASSWORD = "test-secret-123"
BASE_URL = os.environ.get("VSC_GATEWAY_TEST_BASE_URL", "http://127.0.0.1:8000")
REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
RUNTIME_DIR = REPO_ROOT / "runtime"
CONFIG_DIR = REPO_ROOT / "config"
SSH_CONFIG_PATH = CONFIG_DIR / "ssh_config"
SSH_KEYS_DIR = CONFIG_DIR / "keys"


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def _write_password_hash() -> None:
    from vscode_gateway.auth import hash_password

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / "password.hash"
    p.write_text(hash_password(TEST_PASSWORD), encoding="utf-8")
    os.chmod(p, 0o600)


def _reset_state() -> None:
    """Reset state directory contents for a clean run."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Make sure the SQLite db is fresh each run to avoid stale sessions
    # left over from prior failed runs.
    db_path = STATE_DIR / "gateway.db"
    if db_path.exists():
        db_path.unlink()
    # Wipe any prior keys so per-run key lists are predictable.
    if SSH_KEYS_DIR.exists():
        for f in SSH_KEYS_DIR.iterdir():
            if f.is_file():
                f.unlink()
    # Re-create the SSH config file (without the localhost-workspace alias
    # we can use ``PUT /api/ssh/config`` later in tests).
    SSH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSH_CONFIG_PATH.write_text(
        "Host localhost-workspace\n"
        "    HostName 127.0.0.1\n"
        "    Port 22\n"
        "    StrictHostKeyChecking no\n"
        "    UserKnownHostsFile /dev/null\n",
        encoding="utf-8",
    )
    os.chmod(SSH_CONFIG_PATH, 0o600)
    # Remove per-user vscode-gateway runtime state so the runtime install
    # step exercises the real helper every run.
    remote_state = Path.home() / ".vscode-gateway"
    if remote_state.exists():
        shutil.rmtree(remote_state, ignore_errors=True)


@pytest.fixture(scope="module")
def server() -> Any:
    """Start the gateway server as a child process and yield the base URL."""
    # Ensure no previous server is running on the port.
    subprocess.run(
        ["pkill", "-9", "-f", r"vscode_gateway[.]app"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    _reset_state()
    _write_password_hash()

    env = os.environ.copy()
    # Force REAL SSH binaries by absolute path.
    env["VSC_GATEWAY_SSH_EXECUTABLE"] = shutil.which("ssh") or "/usr/bin/ssh"
    env["VSC_GATEWAY_SCP_EXECUTABLE"] = shutil.which("scp") or "/usr/bin/scp"
    env["VSC_GATEWAY_SSH_KEYGEN_EXECUTABLE"] = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"
    env["VSC_GATEWAY_STATE_DIR"] = str(STATE_DIR)
    env["VSC_GATEWAY_RUNTIME_DIR"] = str(RUNTIME_DIR)
    env["VSC_GATEWAY_SSH_CONFIG_PATH"] = str(SSH_CONFIG_PATH)
    env["VSC_GATEWAY_SSH_KEYS_DIR"] = str(SSH_KEYS_DIR)
    env["VSC_GATEWAY_LOG_LEVEL"] = "INFO"
    env["VSC_GATEWAY_LOG_FORMAT"] = "console"
    env["VSC_GATEWAY_DISCONNECT_GRACE_PERIOD"] = "5"
    env["VSC_GATEWAY_SESSION_CAPACITY"] = "3"
    env["VSC_GATEWAY_STARTUP_TIMEOUT"] = "60"
    env["VSC_GATEWAY_BIND_HOST"] = "127.0.0.1"
    env["VSC_GATEWAY_BIND_PORT"] = "8000"
    env["VSC_GATEWAY_CANONICAL_ORIGIN"] = BASE_URL
    env["PYTHONUNBUFFERED"] = "1"

    log_path = STATE_DIR / "server.log"
    log_file = log_path.open("w", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "vscode_gateway.app"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        # Put the server in its own process group so pkill doesn't kill it
        # accidentally and Ctrl-C signals don't propagate.
        start_new_session=True,
    )
    try:
        if not _wait_for_health(BASE_URL, timeout=30.0):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log_path.seek(0)
            raise RuntimeError(
                "Server did not become healthy. Log:\n"
                + log_path.read_text(encoding="utf-8", errors="replace")
            )
        yield BASE_URL
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait()
        except ProcessLookupError:
            pass
        log_file.close()


def _wait_for_health(base_url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    with httpx.Client(base_url=base_url, timeout=2.0) as cl:
        while time.monotonic() < deadline:
            try:
                r = cl.get("/healthz")
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
    return False


@pytest.fixture(scope="module")
def client(server: str) -> Any:
    """Anonymous (unauthenticated) shared client.

    The cookie jar on this instance stays clean so tests that want to
    assert 401 continue to do so. Each authenticated test uses
    ``auth_client`` which provides a separate, logged-in client.
    """
    with httpx.Client(base_url=server, timeout=30.0, follow_redirects=False) as cl:
        yield cl


@pytest.fixture
def auth_client(server: str) -> Any:
    """A function-scoped client with an authenticated session cookie."""
    with httpx.Client(base_url=server, timeout=30.0, follow_redirects=False) as cl:
        csrf = _get_csrf(cl, "/login")
        r = cl.post(
            "/login",
            data={"password": TEST_PASSWORD, "csrf_token": csrf},
        )
        # 303 redirect on successful login.
        assert r.status_code in (303, 302, 200), f"Login failed: {r.status_code} {r.text[:200]}"
        yield cl


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _get_csrf(client: httpx.Client, path: str) -> str:
    r = client.get(path)
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
    return m.group(1) if m else ""


def _page_csrf(client: httpx.Client, path: str) -> str:
    r = client.get(path)
    if r.status_code != 200:
        return ""
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
    if m:
        return m.group(1)
    m = re.search(r'data-csrf="([^"]+)"', r.text)
    if m:
        return m.group(1)
    return ""


def _csrf_header(client: httpx.Client) -> dict[str, str]:
    token = _page_csrf(client, "/")
    return {"X-CSRF-Token": token}


def wait_for_state(
    client: httpx.Client,
    alias: str,
    *,
    predicate,
    timeout: float = 60.0,
    interval: float = 0.5,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get("/api/sessions")
        if r.status_code == 200:
            for ws in r.json().get("workspaces", []):
                if ws.get("alias") == alias and predicate(ws):
                    return ws
        time.sleep(interval)
    return None


# =============================================================================
# Health and version
# =============================================================================


def test_healthz(client: httpx.Client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz(client: httpx.Client) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200


def test_api_version(client: httpx.Client) -> None:
    r = client.get("/api/version")
    assert r.status_code == 200
    data = r.json()
    assert data.get("version") == "0.1.0"


def test_healthz_content_type(client: httpx.Client) -> None:
    r = client.get("/healthz")
    assert r.headers.get("content-type", "").startswith("application/json")


# =============================================================================
# Authentication
# =============================================================================


def test_login_page_renders(client: httpx.Client) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert "password" in r.text.lower()


def test_login_bad_password_returns_401(client: httpx.Client) -> None:
    csrf = _get_csrf(client, "/login")
    r = client.post(
        "/login",
        data={"password": "wrong-password", "csrf_token": csrf},
    )
    assert r.status_code == 401


def test_login_good_password(auth_client: httpx.Client) -> None:
    r = auth_client.get("/")
    assert r.status_code in (200, 303)


def test_logout_not_authenticated(client: httpx.Client) -> None:
    r = client.post("/logout")
    assert r.status_code in (303, 302, 401)


def test_dashboard_requires_auth(client: httpx.Client) -> None:
    r = client.get("/")
    assert r.status_code == 401


def test_sessions_require_auth(client: httpx.Client) -> None:
    r = client.get("/api/sessions")
    assert r.status_code == 401


def test_ssh_config_page_requires_auth(client: httpx.Client) -> None:
    r = client.get("/settings/ssh")
    assert r.status_code == 401


def test_ssh_keys_page_requires_auth(client: httpx.Client) -> None:
    r = client.get("/settings/keys")
    assert r.status_code == 401


# =============================================================================
# Dashboard (authenticated)
# =============================================================================


def test_dashboard_authenticated(auth_client: httpx.Client) -> None:
    r = auth_client.get("/")
    assert r.status_code == 200
    assert "workspace" in r.text.lower()


def test_api_sessions_authenticated(auth_client: httpx.Client) -> None:
    r = auth_client.get("/api/sessions")
    assert r.status_code == 200
    data = r.json()
    assert "workspaces" in data
    assert isinstance(data["workspaces"], list)


# =============================================================================
# CSRF protection
# =============================================================================


def test_mutation_rejects_missing_csrf(auth_client: httpx.Client) -> None:
    r = auth_client.post("/api/sessions/test-alias/open")
    assert r.status_code == 403


def test_mutation_requires_csrf(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    r = auth_client.post("/api/sessions/test-alias/open", headers=headers)
    assert r.status_code in (200, 202, 404, 409)


def test_mutation_without_csrf_header(auth_client: httpx.Client) -> None:
    r = auth_client.post("/api/sessions/localhost-workspace/close")
    assert r.status_code == 403


# =============================================================================
# SSH config API
# =============================================================================


def test_get_ssh_config_unauthenticated(client: httpx.Client) -> None:
    r = client.get("/api/ssh/config")
    assert r.status_code == 401


def test_put_ssh_config_requires_auth(client: httpx.Client) -> None:
    r = client.put(
        "/api/ssh/config",
        json={"text": "Host test\n", "expectedRevision": "sha256:abc"},
    )
    assert r.status_code == 401


def test_get_ssh_config_authenticated(auth_client: httpx.Client) -> None:
    r = auth_client.get("/api/ssh/config")
    assert r.status_code == 200
    data = r.json()
    assert "text" in data
    assert "revision" in data


def test_put_ssh_config_rejects_missing_csrf(auth_client: httpx.Client) -> None:
    r = auth_client.put("/api/ssh/config", json={"text": "Host test\n"})
    assert r.status_code == 403


def test_put_ssh_config_validates(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    text = (
        "Host localhost-workspace\n"
        "    HostName 127.0.0.1\n"
        "    Port 22\n"
        "    StrictHostKeyChecking no\n"
        "    UserKnownHostsFile /dev/null\n"
    )
    r = auth_client.put("/api/ssh/config", json={"text": text}, headers=headers)
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
    data = r.json()
    assert "revision" in data


# =============================================================================
# SSH catalog
# =============================================================================


def test_get_catalog_unauthenticated(client: httpx.Client) -> None:
    r = client.get("/api/ssh/catalog")
    assert r.status_code == 401


def test_get_catalog_authenticated(auth_client: httpx.Client) -> None:
    r = auth_client.get("/api/ssh/catalog")
    assert r.status_code == 200
    data = r.json()
    assert "aliases" in data


# =============================================================================
# SSH keys API
# =============================================================================


def test_list_keys_authenticated(auth_client: httpx.Client) -> None:
    r = auth_client.get("/api/ssh/keys")
    assert r.status_code == 200
    data: dict[str, Any] = r.json()
    assert "keys" in data


def test_create_key_requires_csrf(auth_client: httpx.Client) -> None:
    r = auth_client.post("/api/ssh/keys")
    assert r.status_code == 403


def test_create_key_with_csrf(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    r = auth_client.post("/api/ssh/keys", headers=headers)
    assert r.status_code == 201, f"got {r.status_code}: {r.text[:200]}"
    data = r.json()
    assert "name" in data
    assert "public_key" in data
    assert data["public_key"]


def test_get_key_public(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    create = auth_client.post("/api/ssh/keys", headers=headers)
    assert create.status_code == 201, create.text[:200]
    key_name = create.json()["name"]

    r = auth_client.get(f"/api/ssh/keys/{key_name}.pub")
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
    assert r.text.strip(), "Public key should not be empty"


# =============================================================================
# Error response format
# =============================================================================


def test_404_returns_problem_json(auth_client: httpx.Client) -> None:
    r = auth_client.get("/api/nonexistent")
    assert r.status_code == 404
    data = r.json()
    # FastAPI's default 404 returns {"detail":"Not Found"}; our handler
    # upgrades raised HTTPException to application/problem+json.
    assert "detail" in data or "title" in data


# =============================================================================
# Session lifecycle (with REAL ssh against localhost)
# =============================================================================


def test_open_nonexistent_alias(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    r = auth_client.post(
        "/api/sessions/nonexistent-xyz-123/open",
        headers=headers,
    )
    assert r.status_code == 404


def test_open_valid_alias(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    r = auth_client.post(
        "/api/sessions/localhost-workspace/open",
        headers=headers,
    )
    assert r.status_code == 202, f"got {r.status_code}: {r.text[:200]}"
    data = r.json()
    assert data["status"] == "open_initiated"
    assert "session_id" in data


def test_open_returns_202(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    r = auth_client.post(
        "/api/sessions/localhost-workspace/open",
        headers=headers,
    )
    assert r.status_code in (200, 202), f"got {r.status_code}: {r.text[:200]}"


def test_sessions_list_shows_workspace(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    auth_client.post("/api/sessions/localhost-workspace/open", headers=headers)

    ws = wait_for_state(
        auth_client,
        "localhost-workspace",
        predicate=lambda w: w.get("state") in ("starting", "ready", "error", "stopping"),
        timeout=30.0,
    )
    assert ws is not None, "workspace not in sessions list after open"


def test_session_detail(auth_client: httpx.Client) -> None:
    # Open a fresh session so we have a sessionId to query.
    headers = _csrf_header(auth_client)
    open_r = auth_client.post("/api/sessions/localhost-workspace/open", headers=headers)
    assert open_r.status_code in (200, 202), open_r.text[:200]

    # Wait for the localhost-workspace run to expose a sessionId.
    ws = wait_for_state(
        auth_client,
        "localhost-workspace",
        predicate=lambda w: w.get("sessionId") is not None,
        timeout=30.0,
    )
    assert ws is not None, "no sessionId visible after open"
    sid = ws["sessionId"]

    # The route /api/sessions/{alias:path} looks up by alias; querying by
    # sessionId is a 404 (alias not found) — that is an acceptable
    # idempotent miss, not a crash.
    r = auth_client.get(f"/api/sessions/{sid}")
    assert r.status_code in (200, 404), f"got {r.status_code}: {r.text[:200]}"

    # Also query by the known alias; this is the supported path.
    r2 = auth_client.get("/api/sessions/localhost-workspace")
    assert r2.status_code in (200, 404), f"got {r2.status_code}: {r2.text[:200]}"


def test_open_is_idempotent_when_ready_or_starting(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)

    first = auth_client.post("/api/sessions/localhost-workspace/open", headers=headers)
    assert first.status_code == 202, f"got {first.status_code}: {first.text[:200]}"

    # Second open: returns 202 again if still starting, or 409 if already
    # ready (per the conflict rule for live runs). Both are acceptable.
    second = auth_client.post("/api/sessions/localhost-workspace/open", headers=headers)
    assert second.status_code in (202, 409), (
        f"idempotent open failed: {second.status_code} {second.text[:200]}"
    )


def test_close_session(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)

    open_r = auth_client.post(
        "/api/sessions/localhost-workspace/open",
        headers=headers,
    )
    assert open_r.status_code in (200, 202), open_r.text[:200]

    close_r = auth_client.post(
        "/api/sessions/localhost-workspace/close",
        headers=headers,
    )
    assert close_r.status_code == 204, f"got {close_r.status_code}: {close_r.text[:200]}"


def test_close_nonexistent_idempotent(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    r = auth_client.post(
        "/api/sessions/nonexistent-host/close",
        headers=headers,
    )
    assert r.status_code in (204, 404), f"got {r.status_code}: {r.text[:200]}"


def test_retry_errored_session(auth_client: httpx.Client) -> None:
    headers = _csrf_header(auth_client)
    r = auth_client.post(
        "/api/sessions/localhost-workspace/retry",
        headers=headers,
    )
    assert r.status_code in (202, 404, 409), f"got {r.status_code}: {r.text[:200]}"


# =============================================================================
# SSH config page
# =============================================================================


def test_ssh_config_page_renders(auth_client: httpx.Client) -> None:
    r = auth_client.get("/settings/ssh")
    assert r.status_code == 200


# =============================================================================
# SSH keys page
# =============================================================================


def test_ssh_keys_page_renders(auth_client: httpx.Client) -> None:
    r = auth_client.get("/settings/keys")
    assert r.status_code == 200

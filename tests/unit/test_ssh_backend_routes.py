"""Focused route-contract tests for the rewritten SSH backend."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware

from tests.unit.ssh_backend_test_helpers import (
    add_session,
    generate_key,
    make_settings,
    migrated_database,
)
from vscode_gateway.app import gateway_error_handler
from vscode_gateway.auth import hash_password
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.models import CatalogSnapshot, SessionState
from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry
from vscode_gateway.routes import create_routes
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import SshCatalog
from vscode_gateway.ssh_connection import SshConnectionService
from vscode_gateway.ssh_keys import SshKeyService


def _csrf_from_html(text: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', text)
    if match is None:
        raise AssertionError("authenticated page did not contain a CSRF token")
    return match.group(1)


async def _route_app(
    database: aiosqlite.Connection, settings: Settings
) -> tuple[FastAPI, httpx.AsyncClient]:
    key_service = SshKeyService(settings, database)
    trust_service = HostTrustService(settings, database)
    catalog = SshCatalog(settings)
    catalog.set_snapshot(
        CatalogSnapshot(
            revision="test-revision",
            aliases=("production",),
            loaded_at=datetime.now(UTC),
        )
    )
    registry = ProxyRegistry()
    upstream_client = httpx.AsyncClient(trust_env=False)
    proxy_adapter = ProxyAdapter(registry, upstream_client)
    connection_service = SshConnectionService(settings, key_service, trust_service)
    session_service = SessionService(
        settings,
        database,
        catalog,
        RuntimeService(settings),
        registry,
        connection_service,
        trust_service,
    )
    app = FastAPI(
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key="route-test-secret-" * 4,
                session_cookie="gateway_session",
            )
        ]
    )
    app.state.settings = settings
    app.state.db = database
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.include_router(
        create_routes(
            settings,
            session_service,
            catalog,
            proxy_adapter,
            registry,
            key_service=key_service,
            host_trust_service=trust_service,
        )
    )
    return app, upstream_client


@pytest.mark.asyncio
async def test_global_gateway_error_handler_problem_contract() -> None:
    app = FastAPI()
    app.add_exception_handler(GatewayError, gateway_error_handler)

    @app.get("/error")
    async def error_route() -> None:
        raise GatewayError(
            ErrorCode.ALIAS_NOT_FOUND,
            "Alias is unavailable",
            status_code=409,
            detail="The requested alias is unavailable",
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/error")

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "urn:vscode-gateway:error:alias_not_found"
    assert body["title"] == "Alias is unavailable"
    assert body["detail"] == "The requested alias is unavailable"
    assert body["code"] == "alias_not_found"
    assert body["status"] == 409
    request_id = body["requestId"]
    assert isinstance(request_id, str)
    uuid.UUID(request_id)


@pytest.mark.asyncio
async def test_key_routes_keep_fixed_slots_and_multipart_contract(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.password_hash_path.write_text(hash_password("password"), encoding="utf-8")
    async with migrated_database(tmp_path) as database:
        app, upstream_client = await _route_app(database, settings)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                login_page = await client.get("/login")
                login_csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
                assert login_csrf is not None
                login = await client.post(
                    "/login",
                    data={"password": "password", "csrf_token": login_csrf.group(1)},
                    follow_redirects=False,
                )
                assert login.status_code == 303

                keys_page = await client.get("/settings/keys")
                assert keys_page.status_code == 200
                assert 'name="name"' in keys_page.text
                assert 'name="private_key"' in keys_page.text
                for key_type in ("ed25519", "rsa", "ecdsa"):
                    assert f'id="key-slot-{key_type}"' in keys_page.text
                assert "generate-key" not in keys_page.text
                assert "Generate" not in keys_page.text
                assert "data-csrf" not in keys_page.text
                csrf = _csrf_from_html(keys_page.text)

                config_page = await client.get("/settings/ssh")
                assert config_page.status_code == 200
                assert "automatically tried" in config_page.text
                assert "IdentityFile" in config_page.text
                assert "IdentityAgent" in config_page.text
                assert "RemoteCommand" in config_page.text
                assert "data-csrf" not in config_page.text

                empty = await client.get("/api/ssh/keys")
                assert empty.status_code == 200
                assert empty.json() == {
                    "keys": {
                        "ed25519": {"present": False},
                        "rsa": {"present": False},
                        "ecdsa": {"present": False},
                    }
                }

                key = generate_key("ssh-ed25519")
                uploaded = await client.post(
                    "/api/ssh/keys",
                    headers={"X-CSRF-Token": csrf},
                    data={"name": "route key"},
                    files={
                        "private_key": (
                            "ignored-client-filename",
                            key.export_private_key("openssh"),
                            "application/octet-stream",
                        )
                    },
                )
                assert uploaded.status_code == 201
                payload = uploaded.json()
                assert payload["name"] == "route key"
                assert payload["type"] == "ed25519"
                assert payload["algorithm"] == "ssh-ed25519"
                assert payload["fingerprint"] == key.get_fingerprint("sha256")
                assert payload["publicKey"] == key.export_public_key("openssh").decode().strip()

                inventory = await client.get("/api/ssh/keys")
                assert inventory.json()["keys"]["ed25519"]["present"] is True
                assert inventory.json()["keys"]["rsa"] == {"present": False}
                public = await client.get("/api/ssh/keys/ed25519/public")
                assert public.status_code == 200
                assert public.text == payload["publicKey"]

                deleted = await client.delete(
                    "/api/ssh/keys/ed25519",
                    headers={"X-CSRF-Token": csrf},
                )
                assert deleted.status_code == 204
                assert (await client.get("/api/ssh/keys")).json() == empty.json()
        finally:
            await upstream_client.aclose()


@pytest.mark.asyncio
async def test_unauthenticated_pages_redirect_and_api_stays_unauthorized(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        app, upstream_client = await _route_app(database, settings)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=False,
            ) as client:
                for path in ("/", "/settings/ssh", "/settings/keys"):
                    response = await client.get(path)
                    assert response.status_code == 303
                    assert response.headers["location"] == "/login"

                api_response = await client.get("/api/sessions")
                assert api_response.status_code == 401
        finally:
            await upstream_client.aclose()


@pytest.mark.asyncio
async def test_generation_mismatch_redirects_page_and_clears_session(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    settings.password_hash_path.write_text(hash_password("password"), encoding="utf-8")
    async with migrated_database(tmp_path) as database:
        app, upstream_client = await _route_app(database, settings)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=False,
            ) as client:
                login_page = await client.get("/login")
                login_csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
                assert login_csrf is not None
                login = await client.post(
                    "/login",
                    data={"password": "password", "csrf_token": login_csrf.group(1)},
                )
                assert login.status_code == 303
                assert (await client.get("/")).status_code == 200

                generation_path = settings.state_dir / "session.generation"
                generation_path.write_text(
                    str(int(generation_path.read_text(encoding="utf-8")) + 1),
                    encoding="utf-8",
                )

                page = await client.get("/")
                assert page.status_code == 303
                assert page.headers["location"] == "/login"
                assert (await client.get("/api/sessions")).status_code == 401
        finally:
            await upstream_client.aclose()


@pytest.mark.asyncio
async def test_session_host_key_and_trust_routes_serialize_and_clear_challenge(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    settings.password_hash_path.write_text(hash_password("password"), encoding="utf-8")
    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database, alias="production", state=SessionState.ERROR)
        public_key = generate_key("ssh-ed25519").export_public_key("openssh").decode().strip()
        app, upstream_client = await _route_app(database, settings)
        # The route factory owns this service, so use the application database
        # and a directly-created service for the same persisted challenge.
        challenge_service = HostTrustService(settings, database)
        await challenge_service.record_challenge(
            session_id=session_id,
            role="target",
            alias="production",
            host="production.example.test",
            port=22,
            algorithm="ssh-ed25519",
            fingerprint="SHA256:route-challenge",
            public_key=public_key,
        )
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                login_page = await client.get("/login")
                login_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
                assert login_match is not None
                await client.post(
                    "/login",
                    data={"password": "password", "csrf_token": login_match.group(1)},
                    follow_redirects=False,
                )
                csrf = _csrf_from_html((await client.get("/settings/keys")).text)

                sessions = await client.get("/api/sessions")
                assert sessions.status_code == 200
                workspace = sessions.json()["workspaces"][0]
                assert workspace["alias"] == "production"
                assert workspace["sshHostKey"] == {
                    "role": "target",
                    "host": "production.example.test",
                    "port": 22,
                    "algorithm": "ssh-ed25519",
                    "fingerprint": "SHA256:route-challenge",
                    "publicKey": public_key,
                }
                assert workspace["canForceClose"] is False
                assert workspace["hasRemoteIdentity"] is False

                trusted = await client.post(
                    "/api/ssh/hosts/trust",
                    headers={"X-CSRF-Token": csrf},
                    json={
                        "alias": "production",
                        "host": "production.example.test",
                        "port": 22,
                        "publicKey": public_key,
                        "replace": False,
                    },
                )
                assert trusted.status_code == 204
                known_hosts = settings.ssh_known_hosts_path.read_text(encoding="utf-8")
                assert f"production.example.test {public_key}" in known_hosts
                assert await challenge_service.get_challenge(session_id) is None
        finally:
            await upstream_client.aclose()


@pytest.mark.asyncio
async def test_force_close_route_requires_auth_csrf_and_explicit_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    settings.password_hash_path.write_text(hash_password("password"), encoding="utf-8")
    calls: list[tuple[str, bool]] = []

    async def _close(
        self: SessionService,
        alias: str,
        reason: object | None = None,
        *,
        force: bool = False,
    ) -> None:
        del self, reason
        calls.append((alias, force))

    monkeypatch.setattr(SessionService, "close", _close)

    async with migrated_database(tmp_path) as database:
        app, upstream_client = await _route_app(database, settings)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                unauthorized = await client.post("/api/sessions/production/close?force=true")
                assert unauthorized.status_code == 401

                login_page = await client.get("/login")
                login_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
                assert login_match is not None
                await client.post(
                    "/login",
                    data={"password": "password", "csrf_token": login_match.group(1)},
                    follow_redirects=False,
                )

                forbidden = await client.post("/api/sessions/production/close?force=true")
                assert forbidden.status_code == 403
                assert calls == []

                csrf = _csrf_from_html((await client.get("/")).text)
                forced = await client.post(
                    "/api/sessions/production/close?force=true",
                    headers={"X-CSRF-Token": csrf},
                )
                normal = await client.post(
                    "/api/sessions/production/close",
                    headers={"X-CSRF-Token": csrf},
                )

                assert forced.status_code == 204
                assert normal.status_code == 204
                assert calls == [("production", True), ("production", False)]
        finally:
            await upstream_client.aclose()

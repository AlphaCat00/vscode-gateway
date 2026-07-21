"""Focused route-contract tests for the rewritten SSH backend."""

from __future__ import annotations

import re
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
from vscode_gateway.auth import hash_password
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.models import CatalogSnapshot, SessionState
from vscode_gateway.proxy import ProxyAdapter, ProxyRegistry
from vscode_gateway.readiness import Readiness
from vscode_gateway.routes import create_routes
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import SshCatalog
from vscode_gateway.ssh_connection import SshConnectionService
from vscode_gateway.ssh_keys import SshKeyService


def _csrf_from_html(text: str) -> str:
    match = re.search(r'data-csrf="([^"]+)"', text)
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
    readiness = Readiness()
    await readiness.mark_ready()

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
    app.state.readiness = readiness
    app.state.db = database
    app.include_router(
        create_routes(
            settings,
            session_service,
            catalog,
            proxy_adapter,
            registry,
            readiness,
            key_service=key_service,
            host_trust_service=trust_service,
        )
    )
    return app, upstream_client


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
                csrf = _csrf_from_html(keys_page.text)
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

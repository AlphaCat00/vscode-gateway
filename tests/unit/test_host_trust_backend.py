"""Focused tests for persisted host-key challenges and trust decisions."""

from __future__ import annotations

from pathlib import Path
from stat import S_IMODE
from uuid import UUID

import pytest

from tests.unit.ssh_backend_test_helpers import (
    add_session,
    generate_key,
    make_settings,
    migrated_database,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.settings import Settings


def _public_key(algorithm: str) -> str:
    return generate_key(algorithm).export_public_key("openssh").decode().strip()


async def _record(
    service: HostTrustService,
    session_id: UUID,
    *,
    host: str = "production.example.test",
    port: int = 22,
    public_key: str,
) -> None:
    key_parts = public_key.split()
    await service.record_challenge(
        session_id=session_id,
        role="target",
        alias="production",
        host=host,
        port=port,
        algorithm=key_parts[0],
        fingerprint="SHA256:challenge",
        public_key=public_key,
    )


@pytest.mark.asyncio
async def test_pending_challenge_survives_service_recreation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        first = HostTrustService(settings, database)
        public_key = _public_key("ssh-ed25519")
        await _record(first, session_id, public_key=public_key)

        second = HostTrustService(settings, database)
        challenge = await second.get_challenge(session_id)

    assert challenge is not None
    assert challenge.session_id == session_id
    assert challenge.role == "target"
    assert challenge.alias == "production"
    assert challenge.host == "production.example.test"
    assert challenge.algorithm == "ssh-ed25519"
    assert challenge.public_key == public_key


@pytest.mark.asyncio
async def test_trust_requires_exact_submitted_public_key(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        service = HostTrustService(settings, database)
        public_key = _public_key("ssh-ed25519")
        await _record(service, session_id, public_key=public_key)

        with pytest.raises(GatewayError) as exc_info:
            await service.trust(
                session_id=session_id,
                host="production.example.test",
                port=22,
                public_key=f"{public_key} changed-comment",
                replace=False,
            )

        assert exc_info.value.code == ErrorCode.SSH_HOST_TRUST_MISMATCH
        assert await service.get_challenge(session_id) is not None
        assert not settings.ssh_known_hosts_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_unknown_key_is_appended_and_challenge_is_cleared(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        service = HostTrustService(settings, database)
        unrelated = _public_key("ssh-rsa")
        public_key = _public_key("ssh-ed25519")
        settings.ssh_known_hosts_path.write_text(
            f"other.example.test {unrelated}\n", encoding="utf-8"
        )
        await _record(service, session_id, public_key=public_key)

        await service.trust(
            session_id=session_id,
            host="production.example.test",
            port=22,
            public_key=public_key,
            replace=False,
        )

    text = settings.ssh_known_hosts_path.read_text(encoding="utf-8")
    assert unrelated in text
    assert f"production.example.test {public_key}" in text
    assert await _challenge_after_close(tmp_path, settings, session_id) is None
    assert S_IMODE(settings.ssh_known_hosts_path.stat().st_mode) == 0o600


async def _challenge_after_close(tmp_path: Path, settings: Settings, session_id: UUID) -> object:
    async with migrated_database(tmp_path) as database:
        return await HostTrustService(settings, database).get_challenge(session_id)


@pytest.mark.asyncio
async def test_changed_key_requires_replace_and_replace_removes_old_entry(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        service = HostTrustService(settings, database)
        old_key = _public_key("ssh-ed25519")
        new_key = _public_key("ssh-ed25519")
        unrelated = _public_key("ssh-rsa")
        settings.ssh_known_hosts_path.write_text(
            "\n".join(
                [
                    f"production.example.test {old_key}",
                    f"other.example.test {unrelated}",
                    "# keep this comment",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        await _record(service, session_id, public_key=new_key)

        with pytest.raises(GatewayError) as exc_info:
            await service.trust(
                session_id=session_id,
                host="production.example.test",
                port=22,
                public_key=new_key,
                replace=False,
            )
        assert exc_info.value.code == ErrorCode.SSH_HOST_TRUST_MISMATCH
        assert old_key in settings.ssh_known_hosts_path.read_text(encoding="utf-8")
        assert await service.get_challenge(session_id) is not None

        await service.trust(
            session_id=session_id,
            host="production.example.test",
            port=22,
            public_key=new_key,
            replace=True,
        )

        text = settings.ssh_known_hosts_path.read_text(encoding="utf-8")
        assert old_key not in text
        assert f"production.example.test {new_key}" in text
        assert unrelated in text
        assert "# keep this comment" in text
        assert await service.get_challenge(session_id) is None


@pytest.mark.asyncio
async def test_nondefault_port_uses_canonical_bracketed_host_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        service = HostTrustService(settings, database)
        public_key = _public_key("ssh-ed25519")
        await _record(
            service,
            session_id,
            host="production.example.test",
            port=2222,
            public_key=public_key,
        )

        await service.trust(
            session_id=session_id,
            host="production.example.test",
            port=2222,
            public_key=public_key,
            replace=False,
        )

    text = settings.ssh_known_hosts_path.read_text(encoding="utf-8")
    assert f"[production.example.test]:2222 {public_key}" in text
    assert "production.example.test:2222" not in text


@pytest.mark.asyncio
async def test_algorithm_change_is_changed_host_and_requires_replace(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        service = HostTrustService(settings, database)
        old_key = _public_key("ssh-rsa")
        new_key = _public_key("ssh-ed25519")
        settings.ssh_known_hosts_path.write_text(
            f"production.example.test {old_key}\n",
            encoding="utf-8",
        )
        await _record(service, session_id, public_key=new_key)

        with pytest.raises(GatewayError) as exc_info:
            await service.trust(
                session_id=session_id,
                host="production.example.test",
                port=22,
                public_key=new_key,
                replace=False,
            )
        assert exc_info.value.code == ErrorCode.SSH_HOST_TRUST_MISMATCH

        await service.trust(
            session_id=session_id,
            host="production.example.test",
            port=22,
            public_key=new_key,
            replace=True,
        )

    text = settings.ssh_known_hosts_path.read_text(encoding="utf-8")
    assert old_key not in text
    assert new_key in text


@pytest.mark.asyncio
async def test_clear_challenge_removes_only_pending_record(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        service = HostTrustService(settings, database)
        public_key = _public_key("ssh-ed25519")
        await _record(service, session_id, public_key=public_key)
        await service.clear_challenge(session_id)
        assert await service.get_challenge(session_id) is None

"""Focused tests for uploaded SSH key slots and filesystem storage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from stat import S_IMODE

import pytest

from tests.unit.ssh_backend_test_helpers import generate_key, make_settings, migrated_database
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import SSH_KEY_TYPES, SshKeyMetadata
from vscode_gateway.ssh_keys import SshKeyService

_ENCRYPTED_PRIVATE_KEY = b"""-----BEGIN ENCRYPTED PRIVATE KEY-----
MIGjMF8GCSqGSIb3DQEFDTBSMDEGCSqGSIb3DQEFDDAkBBDd6vQtuF3V+sdXdCCw
CURcAgIIADAMBggqhkiG9w0CCQUAMB0GCWCGSAFlAwQBKgQQTl+ZUE0A4O6VNPXn
VURkZQRAK17cef+Bts+1wPzb+5npeQM49hYKvgKFIvaci2hTv3tKy3HLE+MOvCjB
sMudIGg0fSn8HsK1sDK2EqGvcTssXg==
-----END ENCRYPTED PRIVATE KEY-----
"""


@pytest.mark.asyncio
async def test_imports_all_supported_types_into_fixed_inventory(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        service = SshKeyService(settings, database)
        expected = (
            ("ssh-ed25519", "ed25519", "Primary"),
            ("ssh-rsa", "rsa", "Build"),
            ("ecdsa-sha2-nistp256", "ecdsa", "Fallback"),
        )

        for algorithm, slot, name in expected:
            key = generate_key(algorithm)
            metadata = await service.import_upload(
                name=name,
                private_key_bytes=key.export_private_key("openssh"),
            )
            assert metadata.type == slot
            assert metadata.algorithm == algorithm
            assert metadata.fingerprint == key.get_fingerprint("sha256")

        inventory = await service.list_metadata()
        assert tuple(inventory) == SSH_KEY_TYPES
        assert all(inventory[slot] is not None for slot in SSH_KEY_TYPES)
        assert [key.get_algorithm() for key in service.load_present_keys()] == [
            "ssh-ed25519",
            "ecdsa-sha2-nistp256",
            "ssh-rsa",
        ]

        for slot in SSH_KEY_TYPES:
            assert (settings.ssh_keys_dir / slot).exists()
            assert (settings.ssh_keys_dir / f"{slot}.pub").exists()


@pytest.mark.asyncio
async def test_duplicate_slot_is_rejected_without_replacing_original(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        service = SshKeyService(settings, database)
        first = generate_key("ssh-ed25519")
        second = generate_key("ssh-ed25519")
        await service.import_upload(
            name="first",
            private_key_bytes=first.export_private_key("openssh"),
        )

        with pytest.raises(GatewayError) as exc_info:
            await service.import_upload(
                name="second",
                private_key_bytes=second.export_private_key("openssh"),
            )

        assert exc_info.value.code == ErrorCode.SSH_KEY_EXISTS
        assert (
            await service.get_public_key_text("ed25519")
            == first.export_public_key("openssh").decode().strip()
        )
        inventory = await service.list_metadata()
        assert inventory["ed25519"] is not None
        assert inventory["ed25519"].name == "first"


@pytest.mark.asyncio
async def test_concurrent_same_slot_uploads_publish_only_the_winner(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        service = SshKeyService(settings, database)
        candidates = {
            "first": generate_key("ssh-ed25519"),
            "second": generate_key("ssh-ed25519"),
        }

        results = await asyncio.gather(
            *(
                service.import_upload(
                    name=name,
                    private_key_bytes=key.export_private_key("openssh"),
                )
                for name, key in candidates.items()
            ),
            return_exceptions=True,
        )

        winner = next(result for result in results if isinstance(result, SshKeyMetadata))
        conflict = next(result for result in results if isinstance(result, GatewayError))
        assert conflict.code == ErrorCode.SSH_KEY_EXISTS
        assert sum(isinstance(result, SshKeyMetadata) for result in results) == 1
        assert sum(isinstance(result, GatewayError) for result in results) == 1
        assert await service.get_public_key_text("ed25519") == (
            candidates[winner.name].export_public_key("openssh").decode().strip()
        )
        inventory = await service.list_metadata()
        assert inventory["ed25519"] == winner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["encrypted", "malformed", "public-only", "unsupported"],
)
async def test_encrypted_malformed_public_and_unsupported_keys_are_rejected(
    tmp_path: Path, kind: str
) -> None:
    settings = make_settings(tmp_path)
    if kind == "encrypted":
        payload = _ENCRYPTED_PRIVATE_KEY
    elif kind == "malformed":
        payload = b"not an SSH private key"
    elif kind == "public-only":
        public_key = generate_key("ssh-ed25519").export_public_key("openssh")
        payload = public_key
    else:
        payload = generate_key("ssh-dss").export_private_key("openssh")

    async with migrated_database(tmp_path) as database:
        service = SshKeyService(settings, database)
        with pytest.raises(GatewayError) as exc_info:
            await service.import_upload(name=f"{kind} key", private_key_bytes=payload)

    assert exc_info.value.code == ErrorCode.SSH_KEY_INVALID
    assert await _metadata_count(tmp_path) == 0


async def _metadata_count(tmp_path: Path) -> int:
    async with (
        migrated_database(tmp_path) as database,
        database.execute("SELECT COUNT(*) FROM ssh_keys") as cursor,
    ):
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_public_retrieval_and_deletion_clear_the_slot(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        service = SshKeyService(settings, database)
        key = generate_key("ssh-rsa")
        await service.import_upload(
            name="delete me",
            private_key_bytes=key.export_private_key("openssh"),
        )

        assert (
            await service.get_public_key_text("rsa")
            == key.export_public_key("openssh").decode().strip()
        )
        await service.delete_key("rsa")
        assert await service.list_metadata() == {
            "ed25519": None,
            "rsa": None,
            "ecdsa": None,
        }
        assert not (settings.ssh_keys_dir / "rsa").exists()
        assert not (settings.ssh_keys_dir / "rsa.pub").exists()

        with pytest.raises(GatewayError) as public_exc:
            await service.get_public_key_text("rsa")
        assert public_exc.value.code == ErrorCode.SSH_KEY_NOT_FOUND
        with pytest.raises(GatewayError) as delete_exc:
            await service.delete_key("rsa")
        assert delete_exc.value.code == ErrorCode.SSH_KEY_NOT_FOUND


@pytest.mark.asyncio
async def test_upload_limit_is_checked_before_import(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, upload_limit=256)
    async with migrated_database(tmp_path) as database:
        service = SshKeyService(settings, database)
        with pytest.raises(GatewayError) as exc_info:
            await service.import_upload(name="too large", private_key_bytes=b"x" * 257)

        assert exc_info.value.code == ErrorCode.SSH_KEY_INVALID
        assert not (settings.ssh_keys_dir / "ed25519").exists()


@pytest.mark.asyncio
async def test_private_public_and_directory_permissions_are_owner_only(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        service = SshKeyService(settings, database)
        key = generate_key("ssh-ed25519")
        await service.import_upload(
            name="permissions",
            private_key_bytes=key.export_private_key("openssh"),
        )

    assert S_IMODE(settings.ssh_dir.stat().st_mode) == 0o700
    assert S_IMODE(settings.ssh_keys_dir.stat().st_mode) == 0o700
    assert S_IMODE((settings.ssh_keys_dir / "ed25519").stat().st_mode) == 0o600
    assert S_IMODE((settings.ssh_keys_dir / "ed25519.pub").stat().st_mode) == 0o600

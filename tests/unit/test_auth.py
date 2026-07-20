"""Tests for the auth module."""

from vscode_gateway.auth import generate_csrf_token, hash_password, verify_password


def test_password_hash_and_verify() -> None:
    pw = "test-password-123"
    hashed = hash_password(pw)
    assert hashed != pw
    assert verify_password(pw, hashed)
    assert not verify_password("wrong-password", hashed)


def test_csrf_token_generation() -> None:
    token1 = generate_csrf_token()
    token2 = generate_csrf_token()
    assert isinstance(token1, str)
    assert len(token1) == 64
    assert token1 != token2

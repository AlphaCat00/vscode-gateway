from __future__ import annotations

import secrets
from datetime import UTC, datetime

from pwdlib import PasswordHash  # type: ignore[import-untyped]
from starlette.requests import Request
from starlette.responses import Response

from vscode_gateway.settings import Settings

CSRF_TOKEN_KEY = "_csrf_token"
SESSION_KEY = "_session"
AUTH_FLAG = "authenticated"
CSRF_FIELD = "csrf_token"
ISSUED_AT = "issued_at"


_password_hasher = PasswordHash.recommended()


def hash_password(plaintext: str) -> str:
    return _password_hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    return _password_hasher.verify(plaintext, hashed)


def load_password_hash_from_file(settings: Settings) -> str:
    path = settings.password_hash_path
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    msg = f"Password hash file not found at {path}. Run scripts/create-password-hash.py"
    raise FileNotFoundError(msg)


def generate_csrf_token() -> str:
    return secrets.token_hex(32)


def get_csrf_token(request: Request) -> str:
    session = request.session
    if CSRF_TOKEN_KEY not in session:
        session[CSRF_TOKEN_KEY] = generate_csrf_token()
    return session[CSRF_TOKEN_KEY]


def verify_csrf(request: Request, token: str | None = None) -> bool:
    if token is None:
        token = _extract_csrf_from_request(request)
    if not token:
        return False
    return secrets.compare_digest(token, request.session.get(CSRF_TOKEN_KEY, ""))


def _extract_csrf_from_request(request: Request) -> str | None:
    header = request.headers.get("X-CSRF-Token")
    return header


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(AUTH_FLAG, False))


def create_session(request: Request, response: Response) -> None:
    session = request.session
    token = secrets.token_hex(32)
    session[AUTH_FLAG] = True
    session[ISSUED_AT] = datetime.now(UTC).isoformat()
    session[CSRF_TOKEN_KEY] = generate_csrf_token()
    response.set_cookie(
        key="gateway_session",
        value=token,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
    )


def clear_session(request: Request, response: Response) -> None:
    request.session.clear()
    response.delete_cookie("gateway_session", path="/")


def current_session_generation(settings: Settings) -> int:
    gen_file = settings.state_dir / "session.generation"
    try:
        return int(gen_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        gen_file.write_text("1", encoding="utf-8")
        return 1

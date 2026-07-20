from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pwdlib import PasswordHash  # type: ignore[import-untyped]
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response

from vscode_gateway.settings import Settings

CSRF_TOKEN_KEY = "_csrf_token"
AUTH_FLAG = "authenticated"
CSRF_FIELD = "csrf_token"
ISSUED_AT = "issued_at"
AUTH_GENERATION = "auth_generation"
SESSION_COOKIE_NAME = "gateway_session"

MIN_SECRET_BYTES = 32

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


def get_csrf_token(request: HTTPConnection) -> str:
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
    if header is not None:
        return header
    return None


def is_authenticated(request: HTTPConnection) -> bool:
    return bool(request.session.get(AUTH_FLAG, False))


def create_session(request: Request, settings: Settings) -> None:
    """Authorize the request by mutating the middleware-owned session dict.

    Only SessionMiddleware writes the ``gateway_session`` cookie; this
    helper never emits a manual ``Set-Cookie`` with the same name (HI-05,
    Plan §17.2).
    """
    session = request.session
    session[AUTH_FLAG] = True
    session[ISSUED_AT] = datetime.now(UTC).isoformat()
    session[CSRF_TOKEN_KEY] = generate_csrf_token()
    session[AUTH_GENERATION] = str(current_session_generation(settings))


def clear_session(request: Request) -> None:
    """Revoke the request session.

    SessionMiddleware observes the cleared dict and removes the cookie
    from the client; no second writer is used (HI-05, Plan §17.3).
    """
    request.session.clear()


def current_session_generation(settings: Settings) -> int:
    """Return the server-side auth generation, initializing it on first use.

    The generation lives next to the password hash in the state
    directory. Rotating it (via ``bump_session_generation``) invalidates
    every previously issued session because ``require_auth`` rejects any
    signed session whose stored generation differs (HI-05, Plan §17.3).
    """
    gen_file = settings.state_dir / "session.generation"
    try:
        return int(gen_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        gen_file.write_text("1", encoding="utf-8")
        return 1


def bump_session_generation(settings: Settings) -> int:
    """Increment and persist the auth generation, returning the new value."""
    gen_file = settings.state_dir / "session.generation"
    new_value = current_session_generation(settings) + 1
    gen_file.write_text(str(new_value), encoding="utf-8")
    return new_value


def session_generation_matches(request: Request, settings: Settings) -> bool:
    stored = request.session.get(AUTH_GENERATION)
    if not isinstance(stored, str) or not stored:
        return False
    try:
        stored_int = int(stored)
    except ValueError:
        return False
    return stored_int == current_session_generation(settings)


class LoginThrottle:
    """Fixed-window login throttle keyed by client IP.

    Tracks failure timestamps inside the configured window; once
    ``max_attempts`` failures accumulate within the window, subsequent
    attempts are rejected for ``lockout_seconds`` from the most recent
    failure. State is in-memory and bounded by the window because
    timestamps older than the window are discarded on every lookup
    (HI-05, Plan §17.5). Not thread-safe; intended for asyncio
    single-process use.
    """

    def __init__(
        self,
        max_attempts: int,
        window_seconds: float,
        lockout_seconds: float,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, ()) if t >= now - self._window_seconds]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def check(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)`` for the next attempt."""
        now = time.monotonic()
        recent = self._prune(key, now)
        if len(recent) >= self._max_attempts:
            window_end_at = recent[0] + self._window_seconds
            lock_end_at = recent[-1] + self._lockout_seconds
            unlock_at = max(window_end_at, lock_end_at)
            if unlock_at > now:
                return False, max(1, int(unlock_at - now) + 1)
        return True, 0

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        recent = self._prune(key, now)
        recent.append(now)
        self._failures[key] = recent

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add minimal security headers and a no-store policy on
    authenticated responses (HI-05, Plan §17.6).

    Basic browser hardening headers are emitted on every response.
    ``Cache-Control: no-store, private`` is only added when the request
    carries an authenticated session, to avoid forcing no-store on public
    assets that benefit from caching.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        if request.session.get(AUTH_FLAG, False):
            response.headers.setdefault("Cache-Control", "no-store, private")
        return response

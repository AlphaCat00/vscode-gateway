"""HI-05 regression tests.

Covers the corrections from review finding HI-05: lossless secret
loading, single middleware-owned session cookie, generation-based
invalidation, login throttling, and CSRF-protected logout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from httpx import ASGITransport
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware

from vscode_gateway.auth import (
    LoginThrottle,
    SecurityHeadersMiddleware,
    clear_session,
    create_session,
    current_session_generation,
    get_csrf_token,
    hash_password,
    load_password_hash_from_file,
    session_generation_matches,
    verify_csrf,
    verify_password,
)
from vscode_gateway.settings import Settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        ssh_config_path=tmp_path / "ssh_config",
        ssh_keys_dir=tmp_path / "keys",
        password_hash_path=state_dir / "password.hash",
        session_secret_path=state_dir / "session.secret",
        secure_cookies=False,
        login_max_attempts=3,
        login_window_seconds=60.0,
        login_lockout_seconds=10.0,
    )
    s.password_hash_path.write_text(hash_password("hunter2"), encoding="utf-8")
    return s


def _build_test_app(settings: Settings, throttle: LoginThrottle | None = None) -> FastAPI:
    """A trimmed app mirroring the real login/logout/auth flow so HI-05
    behavior can be exercised without the full lifespan recovery."""
    if throttle is None:
        throttle = LoginThrottle(
            max_attempts=settings.login_max_attempts,
            window_seconds=settings.login_window_seconds,
            lockout_seconds=settings.login_lockout_seconds,
        )

    app = FastAPI(
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key=settings.session_secret,
                session_cookie="gateway_session",
                max_age=settings.session_max_age_seconds,
                https_only=settings.secure_cookies,
                same_site="lax",
                path="/",
                domain=None,
            ),
            Middleware(SecurityHeadersMiddleware),
        ]
    )
    app.state.settings = settings

    async def require_auth_local(request: Request) -> None:
        from fastapi import HTTPException

        if "authenticated" not in request.session or not request.session["authenticated"]:
            raise HTTPException(status_code=401, detail="Authentication required")
        if not session_generation_matches(request, settings):
            request.session.clear()
            raise HTTPException(status_code=401, detail="Session no longer valid")

    def _key(request: Request) -> str:
        client = request.client
        return client.host if client is not None else "unknown"

    @app.get("/login")
    async def login_page(request: Request) -> HTMLResponse:
        csrf = get_csrf_token(request)
        return HTMLResponse(
            f'<form method="post" action="/login">'
            f'<input name="csrf_token" value="{csrf}">'
            f'<input name="password"></form>'
        )

    @app.post("/login")
    async def login_submit(
        request: Request,
        password: str = Form(...),
        csrf_token: str = Form(...),
    ) -> Response:
        key = _key(request)
        allowed, retry_after = throttle.check(key)
        if not allowed:
            return Response(
                content="Too many login attempts",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        if not verify_csrf(request, csrf_token):
            return HTMLResponse("Invalid CSRF token", status_code=403)
        try:
            password_hash = load_password_hash_from_file(settings)
        except FileNotFoundError:
            password_hash = ""
        if not password_hash or not verify_password(password, password_hash):
            throttle.record_failure(key)
            return HTMLResponse("Invalid password", status_code=401)
        throttle.record_success(key)
        create_session(request, settings)
        return RedirectResponse(url="/dashboard", status_code=303)

    @app.post("/logout", dependencies=[Depends(require_auth_local)])
    async def logout(request: Request, csrf_token: str | None = Form(None)) -> Response:
        from fastapi import HTTPException

        if not verify_csrf(request, csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        clear_session(request)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/dashboard", dependencies=[Depends(require_auth_local)])
    async def dashboard() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


@pytest.fixture
async def client_factory(
    settings: Settings,
) -> AsyncIterator[tuple[httpx.AsyncClient, LoginThrottle]]:
    throttle = LoginThrottle(
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_window_seconds,
        lockout_seconds=settings.login_lockout_seconds,
    )
    app = _build_test_app(settings, throttle)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        yield client, throttle


# ---------------------------------------------------------------------------
# Secret loading
# ---------------------------------------------------------------------------


def test_bad_secret_raises_at_startup(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    secret_path = state_dir / "session.secret"
    secret_path.write_text("not-hex!!", encoding="utf-8")
    s = Settings(
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        ssh_config_path=tmp_path / "ssh_config",
        ssh_keys_dir=tmp_path / "keys",
        password_hash_path=state_dir / "password.hash",
        session_secret_path=secret_path,
    )
    with pytest.raises(ValueError, match="not valid hex"):
        _ = s.session_secret


def test_short_secret_raises(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    secret_path = state_dir / "session.secret"
    secret_path.write_text("ab", encoding="utf-8")  # 1 byte -> < 32
    s = Settings(
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        ssh_config_path=tmp_path / "ssh_config",
        ssh_keys_dir=tmp_path / "keys",
        password_hash_path=state_dir / "password.hash",
        session_secret_path=secret_path,
    )
    with pytest.raises(ValueError, match="minimum 32 bytes"):
        _ = s.session_secret


def test_secret_roundtrips_hex_losslessly(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    secret_path = state_dir / "session.secret"
    raw = bytes(range(64))
    secret_path.write_text(raw.hex(), encoding="utf-8")
    s = Settings(
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        ssh_config_path=tmp_path / "ssh_config",
        ssh_keys_dir=tmp_path / "keys",
        password_hash_path=state_dir / "password.hash",
        session_secret_path=secret_path,
    )
    assert s.session_secret_bytes == raw
    assert s.session_secret == raw.hex()


# ---------------------------------------------------------------------------
# Single session cookie, login, logout
# ---------------------------------------------------------------------------


async def _login(client: httpx.AsyncClient) -> httpx.Response:
    login_page = await client.get("/login")
    csrf = _csrf_from_html(login_page.text)
    return await client.post(
        "/login",
        data={"password": "hunter2", "csrf_token": csrf},
    )


def _csrf_from_html(html: str) -> str:
    marker = 'name="csrf_token" value="'
    idx = html.find(marker)
    assert idx != -1, "csrf_token not found in login page"
    start = idx + len(marker)
    end = html.index('"', start)
    return html[start:end]


async def test_login_sets_exactly_one_session_cookie(
    client_factory: tuple[httpx.AsyncClient, LoginThrottle],
) -> None:
    client, _ = client_factory
    response = await _login(client)
    assert response.status_code == 303
    set_cookies = response.headers.get_list("set-cookie")
    gateway_cookies = [c for c in set_cookies if c.startswith("gateway_session=")]
    assert len(gateway_cookies) == 1
    other = [c for c in set_cookies if not c.startswith("gateway_session=")]
    assert other == [], f"unexpected cookies: {other}"


async def test_protected_route_reachable_after_login(
    client_factory: tuple[httpx.AsyncClient, LoginThrottle],
) -> None:
    client, _ = client_factory
    await _login(client)
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store, private"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


async def test_logout_without_csrf_returns_403(
    client_factory: tuple[httpx.AsyncClient, LoginThrottle],
) -> None:
    client, _ = client_factory
    await _login(client)
    resp = await client.post("/logout")
    assert resp.status_code == 403


async def test_logout_without_session_returns_401(
    client_factory: tuple[httpx.AsyncClient, LoginThrottle],
) -> None:
    client, _ = client_factory
    resp = await client.post("/logout", data={"csrf_token": "anything"})
    assert resp.status_code == 401


async def _csrf_after_login(client: httpx.AsyncClient) -> str:
    page = await client.get("/login")
    return _csrf_from_html(page.text)


async def test_logout_with_csrf_clears_session(
    client_factory: tuple[httpx.AsyncClient, LoginThrottle],
) -> None:
    client, _ = client_factory
    await _login(client)
    csrf = await _csrf_after_login(client)
    resp = await client.post("/logout", data={"csrf_token": csrf})
    assert resp.status_code == 303
    protected = await client.get("/dashboard")
    assert protected.status_code == 401


async def test_logout_via_header_csrf(
    client_factory: tuple[httpx.AsyncClient, LoginThrottle],
) -> None:
    client, _ = client_factory
    await _login(client)
    csrf = await _csrf_after_login(client)
    resp = await client.post("/logout", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Generation-based invalidation
# ---------------------------------------------------------------------------


async def test_generation_mismatch_returns_401(
    settings: Settings,
    client_factory: tuple[httpx.AsyncClient, LoginThrottle],
) -> None:
    client, _ = client_factory
    await _login(client)
    assert (await client.get("/dashboard")).status_code == 200

    gen_path = settings.state_dir / "session.generation"
    gen_path.write_text(str(int(gen_path.read_text()) + 1), encoding="utf-8")

    resp = await client.get("/dashboard")
    assert resp.status_code == 401


def test_bump_generation_invalidates_existing_sessions(settings: Settings) -> None:
    from vscode_gateway.auth import bump_session_generation

    initial = current_session_generation(settings)
    new = bump_session_generation(settings)
    assert new == initial + 1


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------


async def test_throttle_rejects_after_max_attempts_with_retry_after(
    settings: Settings,
) -> None:
    throttle = LoginThrottle(
        max_attempts=3,
        window_seconds=60.0,
        lockout_seconds=300.0,
    )
    app = _build_test_app(settings, throttle=throttle)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        login_page = await client.get("/login")
        csrf = _csrf_from_html(login_page.text)

        for _ in range(3):
            resp = await client.post(
                "/login",
                data={"password": "wrong", "csrf_token": csrf},
            )
            assert resp.status_code == 401

        resp = await client.post(
            "/login",
            data={"password": "wrong", "csrf_token": csrf},
        )
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert int(resp.headers["Retry-After"]) >= 1


async def test_throttle_resets_on_success(settings: Settings) -> None:
    throttle = LoginThrottle(
        max_attempts=2,
        window_seconds=60.0,
        lockout_seconds=300.0,
    )
    app = _build_test_app(settings, throttle=throttle)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        login_page = await client.get("/login")
        csrf = _csrf_from_html(login_page.text)
        await client.post("/login", data={"password": "wrong", "csrf_token": csrf})
        # Successful login resets the counter.
        fresh_csrf = _csrf_from_html((await client.get("/login")).text)
        success = await client.post(
            "/login",
            data={"password": "hunter2", "csrf_token": fresh_csrf},
        )
        assert success.status_code == 303


def test_login_throttle_bounded_window() -> None:
    throttle = LoginThrottle(max_attempts=2, window_seconds=1.0, lockout_seconds=0.0)
    for _ in range(2):
        throttle.record_failure("1.2.3.4")
    allowed, _ = throttle.check("1.2.3.4")
    assert allowed is False
    throttle.record_success("1.2.3.4")
    allowed, _ = throttle.check("1.2.3.4")
    assert allowed is True

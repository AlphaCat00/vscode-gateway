# AGENTS.md

## Repository status
This is a planned greenfield Python rewrite of the OpenVSCode SSH Gateway. No code exists yet. The design document is `Python Rewrite Plan.md` — read it before writing any code.

## Tech stack
- Python 3.13+ (uv + pyproject.toml for packaging)
- FastAPI + Starlette, Uvicorn (single worker only: `--workers 1`)
- Jinja2 templates, no SPA/frontend build pipeline
- SQLite via aiosqlite (no ORM; explicit SQL + PRAGMA user_version migrations)
- System OpenSSH via `asyncio.create_subprocess_exec` (never `shell=True`, never a Python SSH lib)
- httpx for HTTP proxy, `websockets` for upstream WebSocket client
- pydantic-settings, pwdlib[argon2], structlog, pytest + AnyIO, Playwright for E2E
- Ruff for lint/format, Pyright/mypy strict for type checking, Bandit + pip-audit for security
- Deliberately excluded: AsyncSSH, SQLAlchemy, Celery/Redis, React/SPA frameworks

## Architecture invariants
- One process, one origin, one SQLite file, one active-session table
- Single-user password auth (Argon2 hash in a private file)
- Workspaces derive from SSH config `Host` aliases — no separate workspace CRUD
- One active session per alias; five states: `starting`, `ready`, `stopping`, `error`, `closed` (implicit)
- Per-alias `asyncio.Lock` for concurrency; global `asyncio.Semaphore` for capacity
- OpenVSCode runs folderless, loopback-only, `--without-connection-token`, behind same-origin proxy at `/editor/{session_id}/...`
- Editor presence tracked by active WebSocket count, not HTTP requests
- Disconnect grace period triggers auto-close

## Commands
No commands are defined yet. Expected conventions (once code exists):
- `uv run ruff check . && uv run ruff format --check .` for lint/format
- `uv run pyright` or `uv run mypy --strict` for type checking
- `uv run pytest` for tests; use `-k` to target a single test

## Conventions from the design doc
- All subprocess calls use a single `run_process` helper with timeout, bounded output, no shell
- Dynamic values passed as process arguments, never interpolated into shell strings
- `ssh -F <dedicated_config>` for every invocation
- State changes and resource identity updates happen in one SQLite transaction
- Never hold a SQLite transaction open while awaiting SSH/network/subprocess work
- Database rows use `compare-and-set` pattern: `UPDATE ... WHERE id = ? AND state = ?`
- Error responses use `application/problem+json` with a `code` field from a small enum
- CSRF token required on all state-changing routes (signed session cookie for auth)
- Grace timers and tunnel watchers run in the lifespan task group, not a background worker
- Remote process identity verified (PID, boot ID, process start ID, executable) before signaling
- Secrets redacted in structured logs; full stderr bounded in length

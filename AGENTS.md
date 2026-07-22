# AGENTS.md

## Repository status

This repository is the Python OpenVSCode SSH Gateway. Application code lives in `src/vscode_gateway/`; `design.md` documents the implemented architecture and its known limitations. Read it before changing behavior.

The SSH backend uses AsyncSSH. Do not restore the deleted system `ssh`, `scp`, or `ssh-keygen` implementation or its fake executables. `tests/integration/test_api_localhost_ssh.py` launches a real localhost OpenSSH server for API lifecycle coverage; it does not replace the application transport with system SSH tools.

The backend key and host-trust APIs have been redesigned, while the checked-in key-management frontend still reflects the previous contract. Do not broaden work into frontend changes unless the user asks for them.

## Tech stack

- Python 3.13+ with uv and `pyproject.toml`
- FastAPI, Starlette, and Uvicorn with exactly one worker
- Jinja2 templates and checked-in JavaScript/CSS; no SPA build pipeline
- SQLite through aiosqlite; no ORM, only explicit SQL and `PRAGMA user_version` migrations
- AsyncSSH for connections, commands, SFTP, host trust, and local forwarding
- httpx for HTTP proxying and `websockets` for upstream WebSocket connections
- pydantic-settings, pwdlib[argon2], and structlog
- pytest with AnyIO, Ruff, and Pyright
- Deliberately excluded: system OpenSSH subprocesses, SQLAlchemy, Celery/Redis, and React/SPA frameworks

## Architecture invariants

- One process, one public origin, one SQLite file, and one active-session table
- Uvicorn must use `--workers 1`; in-memory coordination is not multi-process safe
- Single-user password authentication with an Argon2 hash in a private file
- Workspaces derive only from literal SSH config `Host` aliases; there is no workspace CRUD
- At most one active session per alias
- Durable states are `starting`, `ready`, `stopping`, and `error`; `closed` is implicit row absence
- Per-alias `asyncio.Lock` instances serialize lifecycle work; an ownership set enforces global capacity
- OpenVSCode runs folderless and loopback-only with `--without-connection-token`
- All editor traffic stays behind `/editor/{session_id}/...` on the gateway origin
- Editor presence is active WebSocket count, not HTTP requests
- The final WebSocket disconnect starts a persisted grace deadline and eventual auto-close

## SSH rules

- Gateway-owned SSH state is under `state_dir/ssh`: `config`, `known_hosts`, and `keys/`
- Only uploaded Ed25519, ECDSA, and RSA keys authenticate target and jump hosts; ambient keys, agents, and password methods are disabled
- Unknown and changed host keys require an exact persisted challenge and explicit trust request
- Remote command argv is encoded with `shlex.join()` and passed to AsyncSSH as one command string with byte output
- Use AsyncSSH SFTP and forwarding APIs; never invoke a local shell or system SSH tool
- `ProxyJump` routes are expanded and opened explicitly by `SshConnectionService`; uploaded keys, gateway-owned `known_hosts`, host-key challenge capture, and disabled ambient authentication apply to every hop
- Jump connections are owned with the target and cleaned up in reverse order; unknown jump and target keys can require sequential trust and retry
- Nested and multi-hop routes reject malformed endpoints, cycles, and excessive depth
- The service runs as `nobody` as defense in depth alongside the explicit SSH policy

## Data and lifecycle rules

- Never hold a SQLite transaction while awaiting SSH, network, proxy, or filesystem work
- Use compare-and-set updates for expected session state transitions
- Keep state changes and resource identity updates transactionally consistent when they are in SQLite
- Do not delete a session row until remote process absence has been confirmed
- Start OpenVSCode in a dedicated process group; verify PID, boot ID, process start ID, executable, and process group before signaling the complete group
- Per-session connections, listeners, workers, watchers, and timers are owned and drained by `SessionService`
- Lifespan owns startup recovery, task supervision, and shutdown; do not add an external background worker
- State-changing routes require CSRF protection and authentication
- API errors use `application/problem+json` with a stable `code`
- Keep secrets and private key material out of logs and API responses

## Editing guidance

- Prefer the smallest correct change and preserve the existing design unless the task changes it
- Keep public API field names and problem codes stable unless an explicit migration is requested
- Add SQLite schema changes as numbered migrations; do not add an ORM
- Use `apply_patch` for manual edits
- Do not change frontend files as a side effect of backend work
- Do not add compatibility paths for the deleted SSH backend or old key API without a concrete requirement

## Commands

- `uv run ruff check . && uv run ruff format --check .` for lint and format verification
- `uv run pyright` for type checking
- `uv run pytest` for tests; use `-k` for a focused run
- `uv run pytest tests/integration -m "not real_editor"` for the offline localhost SSH integration case
- `uv run pytest tests/integration -m real_editor` for the opt-in real OpenVSCode case; configure its archive or URL as documented in `README.md`
- `uv run bandit -r src -lll` for high-severity static security findings
- `uv run pip-audit` for dependency vulnerabilities

## Current priorities

- Preserve the completed AsyncSSH backend and session cleanup/recovery guarantees
- Keep `design.md` synchronized with implemented behavior and known limitations

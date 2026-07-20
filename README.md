# OpenVSCode SSH Gateway

Single-process gateway for remote OpenVSCode sessions, discovered from an OpenSSH config.

## Quick start

```bash
uv sync
cp deploy/vscode-gateway.example.env .env
uv run python scripts/create-password-hash.py
uv run uvicorn vscode_gateway.app:create_app --factory --workers 1
```

## Architecture

One process, one origin, one SQLite file. Workspaces are SSH config `Host` aliases.
OpenVSCode runs folderless, loopback-only, behind a same-origin proxy at `/editor/{session_id}/...`.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest
```

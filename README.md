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

The default integration case launches an ephemeral OpenSSH server on localhost and uses a
small synthetic editor artifact while exercising the real API, SSH, SFTP, forwarding, runtime,
and proxy paths:

```bash
uv run pytest tests/integration -m "not real_editor"
```

To run the same lifecycle against a real OpenVSCode archive, provide a local release archive:

```bash
VSC_GATEWAY_TEST_OPENVSCODE_ARCHIVE=/path/to/openvscode-server.tar.gz \
  VSC_GATEWAY_TEST_OPENVSCODE_VERSION=1.89.1 \
  uv run pytest tests/integration -m real_editor
```

Alternatively, set `VSC_GATEWAY_TEST_OPENVSCODE_URL` and
`VSC_GATEWAY_TEST_OPENVSCODE_SHA256`. The real-editor case is skipped when neither source is
configured.

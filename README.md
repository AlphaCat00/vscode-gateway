# OpenVSCode SSH Gateway

Single-process gateway for remote OpenVSCode sessions, discovered from an OpenSSH config.

## Quick start

```bash
uv sync
cp deploy/vscode-gateway.example.env .env
uv run python scripts/create-password-hash.py
uv run uvicorn vscode_gateway.app:create_app --factory --workers 1
```

## Using the gateway

- Workspaces come from literal SSH config `Host` aliases. Edit the config at
  `/settings/ssh`.
- Under `/settings/keys`, upload at most one unencrypted private key for each of
  Ed25519, ECDSA, and RSA. The gateway tries all uploaded keys automatically. Do not
  configure `IdentityFile` or `IdentityAgent`; `RemoteCommand` is supported.
- Click **Open** on a workspace card to start it. Unknown or changed host keys
  require explicit fingerprint verification on the card, followed by the
  existing **Retry** flow.
- If authentication fails, use the card links back to SSH Config and SSH Keys
  to correct the connection settings or manage uploaded keys.

## Architecture

One process, one origin, one SQLite file. Workspaces are SSH config `Host` aliases.
OpenVSCode runs folderless, loopback-only, behind a same-origin proxy at `/editor/{session_id}/...`.
Configured `ProxyJump` routes are expanded and opened explicitly by `SshConnectionService`.
Every hop uses uploaded keys and gateway-owned `known_hosts`, captures host-key challenges,
disables ambient authentication, and is owned and cleaned up with the session. Unknown jump and
target keys can require sequential Trust/Retry steps; nested and multi-hop routes reject malformed
endpoints, cycles, and excessive depth.

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

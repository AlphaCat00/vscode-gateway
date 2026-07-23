# OpenVSCode SSH Gateway Design

## Purpose

The gateway exposes OpenVSCode Server instances running on SSH hosts through one authenticated web origin. A workspace is an explicit `Host` alias in a gateway-owned SSH config. Opening a workspace starts or recovers one remote editor process, creates an SSH local forward, and publishes the editor below `/editor/{session_id}/`.

The implementation is intentionally a single Python process with one SQLite database. It has no external queue, worker, or frontend build pipeline.

## Runtime Shape

- Python 3.13+, FastAPI/Starlette, Uvicorn with exactly one worker
- AsyncSSH for SSH connections, commands, SFTP, host-key verification, and forwarding
- SQLite through aiosqlite with explicit SQL and `PRAGMA user_version` migrations
- httpx for upstream HTTP and `websockets` for upstream WebSockets
- Jinja2 templates and checked-in JavaScript/CSS
- Argon2 single-user password authentication
- structlog for structured logging

The systemd service runs as `nobody` as defense in depth alongside the explicit SSH policy.

## Local State

The default paths are derived from `VSC_GATEWAY_STATE_DIR=state`:

```text
state/
  gateway.db
  gateway.lock
  password.hash
  session.secret
  session.generation
  ssh/
    config
    known_hosts
    keys/
      ed25519
      ed25519.pub
      ecdsa
      ecdsa.pub
      rsa
      rsa.pub
runtime/
  openvscode-server-*.tar.gz
```

`ssh/` and `ssh/keys/` are mode `0700`; config, known-host, key, password, and secret files are mode `0600` where the platform permits it. SSH config, key pairs, and known-host updates use temporary files plus atomic replacement where applicable.

SQLite contains:

- `sessions`: durable session state, stages, remote process identity, proxy ports, presence, deadlines, and errors
- `ssh_keys`: display metadata for the three key slots; private material remains on disk
- `pending_host_keys`: the exact host-key challenge associated with a session

Pending host-key rows reference their session with `ON DELETE CASCADE`, so deleting a
session and its unresolved challenge is one SQLite transaction.

## Authentication And Request Security

The gateway has one password hash in `password.hash`. A signed session cookie authenticates browser requests. Login attempts are throttled in memory.

All state-changing routes require authentication and a CSRF token. WebSocket upgrades require authentication and an exact `Origin` match with `canonical_origin`. Host validation, trusted proxy handling, secure-cookie settings, and maximum session age are configurable.

Unauthenticated or generation-invalid browser navigation to `/`, `/settings/ssh`, and `/settings/keys` redirects to `/login`; API requests retain problem+json 401 responses.

API failures produced from `GatewayError` use `application/problem+json` and include a stable `code` plus a request ID.

## SSH Configuration And Catalog

`state/ssh/config` is the only workspace source. Literal aliases from `Host` declarations are published in the catalog; wildcard and negated patterns are not workspaces.

Config writes:

- require an optional SHA-256 revision for optimistic concurrency
- reject NULs, oversized content, excessive lines, and excessive aliases
- reject directives which can execute local programs, establish unmanaged forwards, or override gateway-owned identity and trust
- atomically replace the config and refresh the in-memory catalog

Startup applies the same content limits and prohibited-directive checks before publishing the catalog. An invalid externally provisioned config publishes no aliases, skips SSH recovery so AsyncSSH never parses it, and leaves readiness degraded until the file is corrected and the service is restarted.

Alias discovery is deliberately lightweight. AsyncSSH remains the authority when it resolves a selected alias and applies supported SSH config directives.

## Uploaded Keys

The gateway accepts unencrypted OpenSSH private keys in three fixed algorithm slots:

- `ed25519`
- `ecdsa`
- `rsa`

There can be at most one key per slot. Uploads are classified from parsed key material rather than filenames, normalized, and stored with their public key. SQLite stores only the display name, algorithm, and SHA-256 fingerprint. Authentication tries present keys in deterministic order: Ed25519, ECDSA, then RSA.

Uploads and deletions are serialized by an in-process lock. Replacing a key requires deleting the existing slot first. The current file-and-database update is not a cross-resource crash-atomic transaction.

Every hop explicitly disables password, keyboard-interactive, host-based, GSSAPI, agent, and default-key authentication. At least one uploaded key is required.

## Host Trust

All target and jump connections use `state/ssh/known_hosts`. Unknown or changed host keys are rejected, captured, and stored as a pending challenge containing the alias, host, port, algorithm, fingerprint, and full public key.

The session enters `error` with `ssh_host_unknown` or `ssh_host_changed`. A trust request must exactly match the pending challenge. Changed keys additionally require `replace=true`. Non-default ports use the OpenSSH `[host]:port` form.

After trust is recorded, the client explicitly retries the session. A route may therefore require sequential trust and retry for an unknown jump key and then an unknown target key. Trust is never granted automatically.

## SSH Connections

`SshConnectionService` owns AsyncSSH transport operations and expands each configured `ProxyJump` route explicitly, opening the resulting chain one hop at a time. A session uses one target connection for remote commands, SFTP, and local forwarding, plus any explicitly opened jump connections. Every hop receives the uploaded keys, gateway-owned `state/ssh/known_hosts`, host-key challenge capture, and disabled ambient authentication policy. Unknown jump and target keys can therefore stop the chain in sequence, requiring trust and retry before the next hop is opened. Nested and multi-hop routes reject malformed endpoints, cycles, and excessive depth.

The complete chain is owned by `SshConnection`; session cleanup and shutdown close the target and then the jumps in reverse order. Remote command arguments are converted with `shlex.join()` and passed as one command string; no local shell or system `ssh`, `scp`, or `ssh-keygen` process is used.

If an active forwarding listener closes unexpectedly, the gateway removes the dead proxy target, closes the old connection chain, and opens fresh SSH chains with exponential backoff until `recovery_timeout` expires. Each attempt inspects the existing session UUID rather than starting another process. Reattachment requires the helper to validate the live process and requires every persisted PID, port, boot ID, process start ID, and executable value to match the inspection result. A replacement local forward is health-checked before one transaction refreshes the identities, clears prior errors, and restores `ready`. The remote process is not stopped merely because its network path failed. Exhausted attempts leave the row in `error` with `tunnel_lost` so manual retry or cleanup remains possible.

## Remote Runtime

The gateway uploads `gateway-helper-v1.sh` to `/tmp/gateway-helper-v1.sh`. The helper manages state under `~/.vscode-gateway` and supports capability inspection, runtime inspection/installation, session start/status/stop, and cleanup.

For a new session the gateway:

1. Validates the alias and reserves per-alias and global capacity.
2. Creates a durable `starting` row.
3. Opens and verifies an AsyncSSH connection.
4. Uploads and probes the helper.
5. Downloads a pinned OpenVSCode archive locally if absent, verifies its SHA-256 hash, and uploads it with SFTP when the remote runtime is absent.
6. Starts OpenVSCode folderless on remote loopback with `--without-connection-token`.
7. Stores the remote PID, port, boot ID, process start ID, executable, and session directory.
8. Creates an AsyncSSH local forward on gateway loopback.
9. Verifies the proxied editor and changes the row to `ready`.

OpenVSCode starts in a dedicated process group. Before signaling it, the helper verifies the saved PID against boot ID, process start ID, executable, and process-group identity, then terminates the complete group so the launch wrapper cannot leave `server-main.js` or its workers orphaned. Normal cleanup does not remove the durable row until remote absence has been confirmed. Local forwarding stops accepting before remote cleanup; the SSH connection is closed before waiting for active forwarded channels to drain. A failed host-key or authentication attempt with no persisted remote or tunnel identity is known to precede remote startup and can be closed without reconnecting.

## Session Model

There is at most one active session per alias. Durable states are:

- `starting`
- `ready`
- `stopping`
- `error`

`closed` is represented by the absence of a row and is synthesized in workspace responses. Stages expose current work such as validation, installation, remote start, tunnel start, verification, recovery, and stop.

Concurrency rules:

- a per-alias `asyncio.Lock` serializes open, retry, and close
- an in-memory ownership set enforces global session capacity
- compare-and-set SQL updates protect expected state transitions
- SQLite transactions are never held while waiting for SSH, network, or proxy work
- owned connections, listeners, workers, watchers, and timers are closed or awaited during shutdown

Startup recovery uses the same validated reattachment path for `starting` and `ready` rows and for errored rows which do not record an outstanding close request. It reconnects to remote editors when identity remains valid, rebuilds and verifies local forwards, refreshes remote identity missing from a crash window, and restores disconnect timers. Sessions which cannot be recovered remain visible as errors until retry or successful cleanup.

Manual retry first attempts the same reattachment when the failed session may have reached the remote host. If the exact remote process remains alive, the existing row and session UUID return to `ready`; retry never calls the remote start operation in this case. An identity mismatch leaves the row and process untouched instead of falling through to destructive retry cleanup. Pre-remote host-key failures without resource identity retain the cleanup-and-open flow. A successful normal close confirms process absence, removes the remote session metadata, and deletes the durable row, so a later open always receives a new UUID rather than reusing a closed session.

Forward watchers are bound to the exact `_SessionTunnel` they observe, so a stale watcher cannot tear down a replacement tunnel. Reconnect backoff does not hold the per-alias lock; each actual attempt does, allowing a close request to move the row to `stopping` between attempts and preventing recovery from overriding close intent.

If normal cleanup cannot verify remote absence, an authenticated, CSRF-protected force close first attempts the same best-effort cleanup and then deletes the local row regardless of SSH or local resource-close errors. It also removes proxy and presence state and releases capacity. This can orphan a remote OpenVSCode process, never changes host trust, and logs the session ID plus whether persisted remote identity existed.

## Presence And Automatic Close

Editor presence is the number of active proxied WebSockets, not HTTP requests. The first connection clears a disconnect deadline. When the final WebSocket closes, the service persists a grace deadline. Expiry triggers normal session close and cleanup.

HTTP and WebSocket editor traffic is available only below `/editor/{session_id}/`. The browser never receives the remote editor port or a direct SSH endpoint.

## HTTP Surface

Primary routes are:

```text
GET    /login
POST   /login
POST   /logout
GET    /

GET    /api/sessions
GET    /api/sessions/{alias}
POST   /api/sessions/{alias}/open
POST   /api/sessions/{alias}/close
POST   /api/sessions/{alias}/retry

GET    /api/ssh/config
PUT    /api/ssh/config
GET    /api/ssh/catalog
GET    /api/ssh/keys
POST   /api/ssh/keys
GET    /api/ssh/keys/{type}/public
DELETE /api/ssh/keys/{type}
POST   /api/ssh/hosts/trust

GET    /healthz
GET    /readyz
GET    /api/version
*      /editor/{session_id}/{path}
WS     /editor/{session_id}/{path}
```

`POST /api/sessions/{alias}/close?force=true` performs the explicit force-close behavior. Closing an already absent session is idempotent for both normal and force requests.

Key upload is multipart form data with `name` and `private_key`. Trust requests submit the exact pending `alias`, `host`, `port`, and `publicKey`, plus `replace` for a changed key.

## Browser Frontend

The checked-in frontend is server-rendered and uses fixed Ed25519, RSA, and ECDSA key slots with one generic multipart upload form. The dashboard renders host-trust actions on workspace cards and retries the existing session after Trust or Replace; Cancel uses the existing session Close action. After normal cleanup reports `stop_failed`, the card offers Force close behind an explicit browser confirmation. The confirmation always warns about orphaning and is stronger when the API reports persisted remote identity. A changed-host response contains only the currently presented fingerprint, so the card explains that the previous fingerprint is unavailable. If a challenge is explicitly marked as a jump host, the card describes it defensively as a jump host used by the selected alias; route handling is owned by the backend as described above.

The SSH config page shows backend-authoritative config errors and adds best-effort client-side line hints for prohibited directives by inspecting the submitted text. The backend does not provide line metadata. Browser automation is not part of the current coverage.

## Lifespan And Deployment

Application lifespan acquires an exclusive process lock, opens and migrates SQLite, loads the SSH catalog, initializes services, runs recovery, and then reports ready. Timers and session workers belong to the lifespan task group. Shutdown rejects new work, closes sessions' local resources, drains tasks, closes shared HTTP resources and SQLite, and releases the process lock.

Run Uvicorn with one worker only. In-memory locks, connection ownership, capacity, presence, throttling, and task supervision make multi-worker deployment invalid.

## Verification

The required local checks are:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Unit tests use fake AsyncSSH connections and service doubles, including explicit jump-route expansion, per-hop policy and host-key challenges, route validation, reverse-order cleanup, temporary reconnect failure, same-UUID process reattachment, identity mismatch rejection, and stale-watcher protection. API integration tests launch ephemeral OpenSSH `sshd` instances on localhost and exercise authentication, CSRF, SSH config publication, key upload, host trust, retry, real SSH negotiation, SFTP runtime installation, forwarding, HTTP proxying, close, key deletion, and sequential jump/target trust. Their default runtime archive contains a small synthetic editor so the normal suite remains fast and offline. The real-editor case also verifies that API close leaves no process whose command references the closed session ID.

The same integration lifecycle can run against a real OpenVSCode release with the `real_editor` marker. Set `VSC_GATEWAY_TEST_OPENVSCODE_ARCHIVE` to a local archive, or set both `VSC_GATEWAY_TEST_OPENVSCODE_URL` and `VSC_GATEWAY_TEST_OPENVSCODE_SHA256`. `VSC_GATEWAY_TEST_OPENVSCODE_VERSION` optionally controls the runtime version tag.

## Current Limitations

- SSH config validation is a defensive directive scanner and alias extractor, not a complete parser.
- Key files and SQLite metadata cannot be committed atomically across a crash.
- Remote helper and archive staging currently use predictable shared `/tmp` locations or `/tmp` staging paths.
- Remote command output and SFTP operations do not yet have one uniform bounded-output and timeout policy.
- The local forwarding port is selected before AsyncSSH binds it, leaving a small allocation race.
- Automated coverage does not currently include proxied WebSockets or browser behavior. Real OpenVSCode coverage is opt-in rather than part of the offline default suite.

# Code Review Against `Plan.md`

**Repository:** `AlphaCat00/vscode-gateway`  
**Review baseline:** `master` as served by GitHub on 2026-07-20  
**Review type:** Static source review against the repository's `Plan.md`  
**Release recommendation:** **No-go for production**

## Scope and limitations

This review compares the current Python implementation with the behavioral, security, lifecycle, recovery, proxy, and test requirements in `Plan.md`.

The repository could not be cloned or executed in the review environment because outbound DNS/network access from the execution container failed. Therefore:

- findings are based on static inspection of the source presented by GitHub;
- the test suite, type checker, linter, and application startup were not executed;
- runtime-only failures may remain undiscovered;
- the branch is not pinned to a commit hash and may change after this review.

Source references use file and function names rather than unstable rendered-line numbers.

## Executive assessment

The implementation follows the plan's principal architecture: a Python/FastAPI service, one origin, one SQLite database, system OpenSSH, a remote helper, loopback-only editor/tunnel endpoints, a same-origin HTTP/WebSocket proxy, and one current session per SSH alias.

However, several implementation details violate explicit plan requirements and create production-blocking security and lifecycle failures. The most serious are:

1. editor WebSockets are not authenticated or Origin-checked;
2. `SessionService.retry()` deadlocks by reacquiring the same non-reentrant alias lock;
3. SSH configuration validation checks the live file instead of the candidate file and ignores validation failure;
4. capacity accounting is neither recovery-safe nor exception-safe;
5. readiness reports success even when recovery fails or is incomplete;
6. integration tests for core behavior are placeholders.

The service should not be exposed to untrusted networks or used as a production gateway until all Critical and High findings are corrected and verified by executable integration and end-to-end tests.

## Conformance summary

| Plan area | Assessment | Notes |
|---|---|---|
| Python/FastAPI, one process, one origin | Partial | Architecture matches, but the one-process invariant is not enforced. |
| OpenSSH compatibility | Partial | Uses system tools, but config candidate validation is broken and subprocess output is not bounded while reading. |
| Session lifecycle | Failing | Retry deadlock, cancellation gaps, close/start race, and capacity drift. |
| Startup recovery | Failing | Recovery exists, but capacity is not rebuilt and readiness is unconditional. |
| HTTP editor proxy | Partial | Authentication exists, but request/response bodies are buffered rather than streamed. |
| WebSocket editor proxy | Failing | Missing authentication, exact Origin validation, and READY-state enforcement. |
| Authentication/browser security | Failing | Duplicate/custom cookie behavior, insecure flags, no observed login throttle or generation enforcement. |
| SSH config editor | Failing | Candidate is not actually validated before replacement. |
| Artifact/runtime management | Partial | Digest checks exist, but cache/download concurrency and size controls are insufficient. |
| Automated tests | Failing | Critical integration scenarios are placeholders and unit coverage misses lifecycle algorithms. |

## Findings by severity

| ID | Severity | Finding |
|---|---|---|
| CR-01 | Critical | Editor WebSocket route is unauthenticated and does not validate Origin or READY state. |
| CR-02 | Critical | `retry()` deadlocks and can double-release capacity while cleanup races a new open. |
| CR-03 | Critical | SSH config candidate validation is ineffective and invalid content can replace the active config. |
| HI-01 | High | Capacity accounting leaks, drifts after restart, and is not tied to resource ownership. |
| HI-02 | High | Unexpected open failures and cancellation can leave durable `starting` rows and leaked resources. |
| HI-03 | High | Close/start cancellation uses a stale record and can miss a newly created remote process. |
| HI-04 | High | `/readyz` is always ready and startup swallows recovery failures. |
| HI-05 | High | Authentication/session cookie implementation contradicts the plan and weakens session correctness. |
| HI-06 | High | Editor HTTP proxy buffers both directions and does not fully preserve proxy semantics. |
| HI-07 | High | The single-process invariant is advisory rather than enforced. |
| HI-08 | High | Subprocess output limits are applied only after unbounded buffering. |
| HI-09 | High | Artifact caching/downloading lacks a hard size cap and per-digest concurrency control. |
| HI-10 | High | Core integration tests are placeholders; release criteria are not proven. |
| ME-01 | Medium | State/config/key filesystem permissions are not robustly enforced. |
| ME-02 | Medium | Routes and static mounts are registered inside lifespan startup. |
| ME-03 | Medium | Raw operational details can be returned to clients. |
| ME-04 | Medium | HTTP/WS proxy resolution does not consistently require a DB `ready` state. |

---

## Detailed findings

### CR-01 — Editor WebSocket route bypasses authentication

**Observed code**

- [`src/vscode_gateway/routes.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/routes.py), `proxy_ws_route`
- The HTTP proxy route calls `require_auth`.
- The WebSocket route parses the session UUID, checks only that a database record exists, increments presence, and proxies the socket.
- It does not authenticate the signed gateway session, validate the request `Origin`, require an in-memory registry target, or require the database state to be `ready`.

**Plan conflict**

- §16.1 requires authentication, registry lookup, and `ready` state for every HTTP and WebSocket request.
- §16.3 requires authentication before `accept`.
- §30 requires dashboard and editor authentication.

**Impact**

A party that learns or guesses a live session UUID can attempt an editor WebSocket connection without logging in. UUID entropy reduces guessing probability but is not an authorization control; URLs can leak through logs, history, screenshots, referrers, or support data. Missing exact Origin validation also leaves browser-based cross-site WebSocket abuse possible when cookies are present.

**Required correction**

1. Authenticate the signed session before opening the upstream socket or accepting downstream.
2. Validate `Origin` exactly against the configured canonical origin. Define an explicit policy for non-browser clients rather than silently accepting missing Origin.
3. Resolve through the registry and re-read the session; require `SessionState.READY`.
4. Increment presence only after successful authorization and upstream establishment, and decrement exactly once.
5. Return a stable close code for authentication, stale session, and unavailable upstream cases without leaking internals.

**Required tests**

- unauthenticated WebSocket rejected before upstream connection;
- authenticated wrong-Origin WebSocket rejected;
- unknown, stopping, starting, and error session IDs rejected;
- stale session URL rejected after reopen;
- presence count remains balanced on each failure path.

### CR-02 — `retry()` deadlocks and corrupts lifecycle accounting

**Observed code**

- [`src/vscode_gateway/sessions.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/sessions.py), `SessionService.retry`
- `retry()` enters `async with self._get_lock(alias)` and then calls `await self.open(alias)` before leaving that block.
- `open()` attempts to acquire the same `asyncio.Lock`, which is not reentrant.
- The old row is deleted before cleanup completes, `_do_close(session)` is spawned concurrently, and capacity is released both in `retry()` and potentially again in `_do_close()`.

**Plan conflict**

- §14.3 requires cleanup to establish safety before deleting the old row and creating a fresh run.
- §14.6 requires capacity to be released only after resources are absent and the row is deleted.

**Impact**

A retry request can hang indefinitely. If the deadlock is removed without redesign, concurrent cleanup and reopen can stop or remove resources belonging to the wrong operation, and double release can allow more sessions than the configured limit.

**Required correction**

Use one of these safe structures:

- implement `_open_locked(alias)` and call it while the existing alias lock is already held; or
- finish cleanup, release the alias lock, then call public `open()`.

In either design:

1. cancel and await the previous start task;
2. remove the registry target;
3. stop tunnel and remote resources synchronously or prove them absent;
4. retain the error row when cleanup cannot establish safety;
5. delete the old row only after safe cleanup;
6. release capacity exactly once through an ownership ledger;
7. create a new session ID only after the old run is closed.

**Required tests**

- retry completes under a timeout;
- retry creates a new ID;
- failed cleanup retains the old error row;
- retry never overlaps old cleanup with new startup;
- capacity remains exact after successful and failed retry.

### CR-03 — SSH config validation validates the wrong file and ignores failure

**Observed code**

- [`src/vscode_gateway/ssh.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/ssh.py), `validate_and_save_config` and `validate_alias`
- Candidate text is written to a temporary file, but alias validation uses `settings.ssh_config_path`, the current live file, rather than the temporary candidate path.
- A failed validation branch contains `pass`, so it does not reject the update.
- The temporary path is deterministic and there is no observed process-wide config-write lock.

**Plan conflict**

- §11.3 explicitly requires `ssh -F <temp> -G <alias>` for each candidate and rejection before replacement.
- §11.4 requires a conservative unsafe-directive policy.

**Impact**

Syntactically invalid or unsafe SSH content can replace the active gateway configuration. Concurrent saves can race on the same temporary path. The published catalog can describe data that was never validly committed.

**Required correction**

1. Serialize config writes with one application-level lock.
2. Create a unique mode-`0600` temporary file in the target directory.
3. Apply byte, line, UTF-8, NUL, alias, and prohibited-directive validation to candidate bytes.
4. Discover aliases from the candidate file.
5. Run `ssh -F <candidate-path> -G <alias>` and reject any nonzero exit or timeout.
6. `fsync` candidate, `os.replace`, then `fsync` parent directory.
7. Re-read and refresh from the committed target before publishing the snapshot.
8. Preserve the previous file and last-known-good catalog on every failure.

**Required tests**

- invalid candidate never changes active bytes;
- validator argv contains the candidate path;
- unsafe directives are rejected;
- concurrent saves yield one success and one revision conflict, not a mixed file;
- crash/failure before replace leaves the original intact.

### HI-01 — Capacity accounting is not exception-safe or recovery-safe

**Observed code**

- [`src/vscode_gateway/sessions.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/sessions.py), `_capacity_acquire`, `_capacity_release`, `open`, `retry`, `recover_all`, `_do_close`
- Capacity is an integer initialized to zero.
- `open()` increments it before database insertion; an insertion failure does not roll it back.
- Recovery adopts live sessions without reserving capacity for them.
- Recovery and retry contain release calls when the process-local counter may not own a slot.
- Release is not associated with a session identity.

**Plan conflict**

- §14.6 says resource-bearing states count against capacity and capacity is rebuilt during recovery.

**Impact**

The process can permanently lose capacity after an error, or undercount recovered sessions and admit more than `max_sessions`. Double release can conceal live resource ownership.

**Required correction**

Represent capacity ownership as `set[SessionId]` or an equivalent ledger, not an anonymous integer. Reserve by session ID; rollback on insert/start scheduling failure; release idempotently by session ID only after resources are absent. During recovery, inspect all rows and rebuild the ownership set before accepting mutations or reporting ready.

### HI-02 — Unexpected exceptions and cancellation can strand startup

**Observed code**

- [`src/vscode_gateway/sessions.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/sessions.py), `_run_open` and `_do_open`
- `_run_open` logs generic exceptions but does not mark the row error, clean partial resources, or release capacity.
- `_do_open` catches `GatewayError`, but not arbitrary exceptions or `asyncio.CancelledError` with a cleanup guarantee.
- Shutdown cancellation can interrupt after remote or tunnel creation.

**Plan conflict**

- §14.1 requires cancellation-safe cleanup.
- §3.3 and §30 require failed start and graceful shutdown not to abandon resources.

**Impact**

A programming error, database failure, cancellation, or unexpected library exception can leave a durable `starting` row, a live tunnel or remote server, and a permanently held capacity slot.

**Required correction**

Track acquired resources in an operation-local ledger. Add explicit `except asyncio.CancelledError` cleanup followed by re-raise, plus a generic exception path that performs best-effort cleanup, writes a sanitized `internal_error`, and releases capacity only when safe. Shield the minimum critical cleanup/commit sections from cancellation.

### HI-03 — Close/start race can miss the remote process

**Observed code**

- [`src/vscode_gateway/sessions.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/sessions.py), `close`, `_do_close`, `_cancel_start`
- `close()` reads a session record, cancels the start task, then passes the stale record to `_do_close()`.
- `_do_close()` conditionally stops the remote process only when the stale record already contains remote identity and is not `STARTING`.

**Plan conflict**

- §14.2 requires cleanup of owned resources, including partial-start resources.

**Impact**

The startup task may persist remote identity after the close snapshot but before cancellation is observed. Close can then skip remote stop, delete the row, and lose evidence of a managed remote process.

**Required correction**

Cancel and await the start task, then re-read the row and inspect remote managed state by session ID. Cleanup decisions must use current persisted identity plus the operation resource ledger, not a stale snapshot. Never delete the row until absence is established.

### HI-04 — Readiness is unconditional and recovery failure is swallowed

**Observed code**

- [`src/vscode_gateway/routes.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/routes.py), `readyz`
- [`src/vscode_gateway/app.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/app.py), lifespan recovery handling
- `/readyz` always returns `{"ready": true}`.
- Startup catches recovery exceptions and continues serving.

**Plan conflict**

- §10.3 and §15 require readiness to remain false until recovery reaches a safe result.
- §15.4 requires unresolved counts in readiness output.

**Impact**

A load balancer can route mutations and editor traffic before recovery, capacity reconstruction, catalog validation, or orphan handling are complete. Operators receive a false healthy signal after a fatal recovery error.

**Required correction**

Maintain an application readiness state with phases such as `starting`, `recovering`, `ready`, and `degraded`. Return HTTP 503 until migrations, catalog initialization, capacity reconstruction, and mandatory recovery complete. Either fail startup on unsafe recovery failure or report a bounded degraded state with unresolved counts and disabled unsafe mutations.

### HI-05 — Authentication and cookie handling contradict the plan

**Observed code**

- [`src/vscode_gateway/app.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/app.py)
- [`src/vscode_gateway/auth.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/auth.py)
- `SessionMiddleware` is configured without production-secure cookie enforcement.
- Random secret bytes are decoded to text with replacement semantics rather than stored/loaded in a deterministic lossless encoding.
- Login/session creation also manually sets a cookie with the middleware cookie name, creating competing cookie representations.
- Session-generation and login-throttle settings/functions are present but were not observed as enforced by the login route.
- Logout lacks the same authenticated-CSRF mutation treatment as other state-changing actions.

**Plan conflict**

- §17.2 requires one Starlette signed session cookie with secure production flags.
- §17.3 requires generation-based invalidation.
- §17.4 requires CSRF on every mutation.
- §17.5 requires login throttling.

**Impact**

Duplicate same-name cookies produce ambiguous client/server behavior. Lossy secret decoding can reduce or destabilize signing-key material. Missing throttling and incomplete invalidation weaken the authentication boundary.

**Required correction**

Use `SessionMiddleware` as the only cookie writer. Store the secret as raw bytes loaded through a lossless base64/hex representation. Make secure-cookie behavior an explicit validated production setting. Validate issued-at and session generation on every authenticated request. Apply bounded login throttling. Require authenticated CSRF-protected logout. Add security headers and no-store policy to authenticated responses.

### HI-06 — HTTP proxy buffers request and response bodies

**Observed code**

- [`src/vscode_gateway/proxy.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/proxy.py), HTTP proxy path
- The request body is read with `await request.body()`.
- The upstream request uses a non-streaming convenience call, so the response is buffered before creating `StreamingResponse`.
- Header conversion to a plain dictionary can collapse repeated headers.
- The process-wide client was not observed with `trust_env=False`.

**Plan conflict**

- §16.2 requires streaming in both directions, environment proxy use disabled, and conservative header preservation.

**Impact**

Large uploads, downloads, extension packages, and editor assets can produce avoidable memory spikes. Environment proxy variables may redirect loopback traffic. Collapsed repeated headers can break protocol behavior.

**Required correction**

Stream `request.stream()` to an HTTPX request and use `client.send(..., stream=True)`. Close the upstream response through a background finalizer. Set `trust_env=False`, explicit timeouts, and no redirects. Preserve raw multi-value response headers where Starlette permits, and add tested cookie/header behavior.

### HI-07 — Single-process deployment is not enforced

**Observed code**

- [`src/vscode_gateway/app.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/app.py)
- Documentation starts Uvicorn with one worker, but the application has no observed cross-process singleton lock.

**Plan conflict**

- §6.1 states that the service must fail startup in multi-worker mode.

**Impact**

An operator can start multiple workers or service instances sharing SQLite while maintaining independent alias locks, capacity counters, registries, tunnel handles, and presence counts. This breaks core invariants.

**Required correction**

Acquire an exclusive operating-system file lock in the state directory before opening mutable services; hold it for process lifetime and fail a second process with a clear startup error. Keep the documented one-worker Uvicorn command, but do not depend on it for correctness.

### HI-08 — Subprocess output bounds are applied after buffering

**Observed code**

- [`src/vscode_gateway/ssh.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/ssh.py), `run_process`
- The implementation uses `proc.communicate()` and slices stdout/stderr after completion.

**Plan conflict**

- §12.1 requires bounded captured output.

**Impact**

A remote helper, SSH failure, or malicious endpoint can emit unbounded output and exhaust gateway memory before truncation occurs.

**Required correction**

Read stdout and stderr concurrently with bounded collectors. Once a configured limit is exceeded, retain only a bounded prefix/tail as designed, terminate the child process group, and return an explicit oversized-output error. Continue draining safely enough to avoid pipe deadlock.

### HI-09 — Artifact cache is not bounded or concurrency-safe

**Observed code**

- [`src/vscode_gateway/runtime.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/src/vscode_gateway/runtime.py), artifact download/verification
- A deterministic temporary path is shared per digest without an observed per-digest lock.
- Download size is not capped.
- Existing cached files may be loaded wholly into memory for verification.

**Plan conflict**

- §13 requires verified, safe, idempotent artifact handling.
- §3.3 requires bounded operational behavior.

**Impact**

Concurrent opens can race on the same temporary artifact. A server can stream an unexpectedly large payload. Large cache verification can spike memory.

**Required correction**

Use one lock per artifact digest, a unique same-directory temporary file, a manifest-derived maximum byte limit, streaming SHA-256 verification, atomic replace, and cleanup of abandoned temporary files. Verify cached files incrementally.

### HI-10 — Tests do not prove the plan's release criteria

**Observed code**

- [`tests/unit/test_sessions.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/tests/unit/test_sessions.py) covers projection behavior but not lifecycle algorithms.
- [`tests/unit/test_auth.py`](https://github.com/AlphaCat00/vscode-gateway/blob/master/tests/unit/test_auth.py) covers only a small subset of the authentication contract.
- [`tests/integration`](https://github.com/AlphaCat00/vscode-gateway/tree/master/tests/integration) contains placeholder test bodies for open/close, proxy, and recovery scenarios.

**Plan conflict**

- §24 specifies unit, subprocess, proxy, helper, end-to-end, load, and soak coverage.
- §30 requires all suites to pass.

**Impact**

The most important security and lifecycle behavior has no executable evidence. The critical defects above are exactly the classes of defect the planned tests should catch.

**Required correction**

Treat placeholder or skipped core tests as a release failure. Add deterministic fake SSH/helper/upstream fixtures and test every Critical/High regression before broadening feature work.

### ME-01 — Private filesystem modes are not robustly enforced

State directories and files appear to rely partly on default `mkdir`/`touch` behavior, and existing ownership, permissions, and symlink conditions are not consistently validated. Enforce `0700` directories and `0600` sensitive files at creation and startup; reject insecure pre-existing paths and unexpected symlinks.

### ME-02 — Route registration occurs inside lifespan

Application routes/static mounts are registered during lifespan startup. Repeated lifespan cycles in tests can duplicate routes, and the route table is incomplete before startup. Construct routes/mounts once in `create_app`; store runtime dependencies in `app.state` or use stable dependency providers.

### ME-03 — Operational details can leak to clients

Problem responses and key-generation failures may include raw exception or stderr text. Separate a stable safe client message from internal diagnostic detail. Log the latter with a request ID and redaction; never return remote paths, commands, host details, or raw helper output by default.

### ME-04 — Proxy readiness checks are inconsistent

The proxy should require both an in-memory target and a current DB `READY` state. A registry entry is currently added before health verification during open, and the WebSocket route accepts any existing row. Register only after successful health verification/`mark_ready`, or make route resolution atomically validate both sources.

---

## Positive implementation observations

The following plan choices are present and should be retained while fixing the blockers:

- Python/FastAPI implementation with no frontend build requirement.
- One public application origin and editor path under `/editor/{session_id}/`.
- System `ssh`, `scp`, and `ssh-keygen` rather than a partial SSH reimplementation.
- SQLite operational state and one session per alias.
- Per-alias in-process locks.
- Remote helper operations and strong process identity fields.
- Loopback-only remote editor and local tunnel targets.
- Pinned artifact/digest model with local and remote verification intent.
- Explicit lifecycle states and diagnostic stages.
- Same-origin HTTP proxy authentication.
- CSRF checks on most control mutations.
- Gateway-cookie removal before upstream forwarding.

These are architectural assets; the corrective plan should harden them rather than introduce a second origin, distributed coordinator, generic job system, or broader framework abstraction.

## Required remediation order

### P0 — Security and state-corruption blockers

1. Fix WebSocket authentication, exact Origin validation, registry lookup, and READY-state enforcement.
2. Redesign Retry to avoid lock recursion and await safe cleanup.
3. Fix SSH candidate validation and atomic config-write serialization.
4. Replace anonymous capacity counter with session-ID ownership and rebuild it during recovery.
5. Add regression tests for all four items before further release work.

### P1 — Lifecycle, recovery, and proxy correctness

1. Make open and close cancellation-safe with explicit resource ownership.
2. Implement real readiness and fail/degrade safely on recovery errors.
3. Enforce process singleton lock.
4. Correct cookie/session generation/throttling/logout behavior.
5. Implement true bidirectional HTTP streaming and robust header handling.
6. Bound subprocess output during reads.
7. Make artifact download/cache bounded and concurrency-safe.

### P2 — Hardening and release evidence

1. Enforce file modes, ownership, and symlink policy.
2. Move route/mount construction out of lifespan.
3. Sanitize client-visible errors and add request-correlated diagnostics.
4. Complete subprocess, proxy, recovery, helper, browser, load, and soak suites.
5. Run Ruff, strict Pyright, pytest, Bandit, dependency audit, and Playwright in CI.

## Release gates

Production release is blocked until all of the following are true:

- no open Critical or High finding in this review;
- all core integration tests contain assertions and execute in CI;
- unauthenticated and wrong-Origin WebSocket tests prove no upstream connection occurs;
- Retry race/deadlock tests pass repeatedly under timeout;
- invalid SSH config tests prove original bytes are preserved;
- restart tests prove capacity is reconstructed before readiness;
- cancellation tests prove no untracked remote/tunnel process remains;
- large proxy transfer and oversized subprocess-output tests pass within memory limits;
- a clean end-to-end open, editor load, terminal WebSocket, close, reopen, grace cleanup, and restart recovery flow passes behind the supported reverse proxy;
- the application refuses a second process using the same state directory;
- static analysis, dependency audit, and the full test matrix pass from a clean checkout.

## Final verdict

The codebase is directionally aligned with `Plan.md`, but it has multiple exploitable or state-corrupting deviations in authentication, retry, config validation, capacity, recovery, and proxy behavior. The current implementation should be treated as an incomplete pre-production build. Apply the updated plan in `plan.md`, beginning with P0 remediation and executable regression tests.

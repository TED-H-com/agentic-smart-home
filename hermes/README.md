# Private Hermes runtime

This is the Plan v3 orchestrator adapter. The inference loop is the actual upstream
`run_agent.AIAgent`, pinned to the commit and verified archive in
[`hermes.lock.json`](hermes.lock.json). It does not launch Hermes Gateway, accept bot
tokens, execute coding workers inside this service, or acquire household approval
authority. It dispatches supported Host coding work through the fixed Controller
MCP adapter; Home coding remains unavailable pending its separate App launcher.

Build from the product root:

```sh
docker build -t agentic-home-hermes:0.6.1 hermes
pytest-disktmp uv run pytest -q tests/test_hermes_runtime.py --containers
```

Python 3.14 is required by this upstream revision. `requirements.in` records its
core and MCP dependencies; `requirements.txt` locks all 77 resolved packages and
hashes. Deliberate security overrides are recorded below and in
`hermes.lock.json`. The upstream source is unmodified; source extraction omits
convenience symlinks. This service and its dependencies are separate from the
Controller and household Runner images.

## Dependency security overrides

The 2026-10-11 review of this repository's open Dependabot alerts identified 20
alerts against the Hermes lock: 14 for PyJWT, five for httpx2 and one for
httpcore2. The lock changes only these three installed package versions; the
Hermes commit, MCP 2.0.0, other dependencies and existing cryptography 50.0.2
override remain unchanged.

| Package | Previous | Locked | Upstream evidence |
| --- | --- | --- | --- |
| PyJWT | 2.13.0 | 2.15.0 | [2.15.0 changelog](https://github.com/jpadilla/pyjwt/blob/2.15.0/CHANGELOG.rst), [OKP key consistency fix](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-x33g-cr3x-6449), [HMAC confusion fix](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-ffc3-869f-jxw9) |
| httpx2 | 2.7.0 | 2.12.0 | [Bounded response decompression](https://github.com/pydantic/httpx2/security/advisories/GHSA-8xx6-hgc6-gc2m) |
| httpcore2 | 2.7.0 | 2.12.0 | [SOCKS secure WebSocket TLS fix](https://github.com/pydantic/httpx2/security/advisories/GHSA-7mj9-2mp8-4m2p), [httpx2 2.12.0 package metadata](https://pypi.org/pypi/httpx2/2.12.0/json) |

The HTTP transport advisory first fixes httpcore2 in 2.10.0, but httpx2 2.12.0
requires exactly httpcore2 2.12.0. This is a required dependency change. The
[PyJWT options-mutation advisory](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-gvp8-978c-rx2q)
has no first-patched-version field in GitHub's advisory metadata; its published
affected range ends at 2.13.0. An installed-package regression probe verifies
that 2.15.0 preserves caller options and rejects an expired token after signature
verification is re-enabled. These checks establish this reviewed lock's behavior;
they are not a claim that every dependency is free of vulnerabilities.

Regenerate the hash lock while retaining compatible existing resolutions:

```sh
uv pip compile --python 3.14 --generate-hashes hermes/requirements.in -o hermes/requirements.txt
```

## Private API, protocol 1

The service listens only on `127.0.0.1:8096`. Every endpoint requires
`Authorization: Bearer <service-token>`; requests with an `Origin` header are
rejected. The browser uses Controller, which supplies the identity scope and
never returns this service bearer. Deployment supplies a unique read-only
`/hermes-link/service-token`, writable `/data` owned by UID 10003, read-only root
filesystem, dropped capabilities, and CPU/memory/PID limits.

| Endpoint | Request / result |
| --- | --- |
| `GET /v1/capabilities` | Runtime commit, selected model, auth state, execution state (`idle`, `busy`, `stop_unconfirmed`), fixed tools and adapter features |
| `POST /v1/auth` | `{provider:"openrouter",model,api_key}`; configured until an actual inference succeeds |
| `POST /v1/auth/device` | `{provider:"openai-codex",model}` and `Idempotency-Key`; asynchronous, separate Hermes device login |
| `GET /v1/auth/device/{auth_id}` | Fixed device URL, user code, expiry and durable login state; no token material |
| `POST /v1/auth/device/{auth_id}/cancel` | Fences the login process before marking cancelled or removing its store |
| `POST /v1/auth/logout` | Stops active run, removes credentials and all session history |
| `POST /v1/retention` | `{context_days:1..7}`; saves the limit and prunes expired context; returns `{context_days}` |
| `POST /v1/sessions` | `{scope_id}` plus `Idempotency-Key`; returns `session_id`, `scope_id`, `created_at` |
| `POST /v1/sessions/{session_id}/runs` | `{run_id,scope_id,case_id,message,mcp_token}` plus `Idempotency-Key`; immediate `202` handle |
| `GET /v1/runs/{run_id}?scope_id=…` | Durable state, answer, normalized error, event cursor and provider-reported token usage |
| `GET /v1/runs/{run_id}/events?scope_id=…&after=N` | Bounded ordered state events and cursor; polling does not call a model |
| `POST /v1/runs/{run_id}/cancel` | `{scope_id}`; `cancelled` only after the local process is stopped |

`scope_id` is an opaque Controller-computed 64-character lowercase hex digest.
Session/run IDs are 32-character lowercase hex strings. Case IDs are bounded
opaque identifiers. Fields not in the Pydantic contract are rejected. Error
bodies are `{detail:{code,message}}`; validation never echoes input credentials.

The run result is `{run_id,session_id,case_id,state,answer,error,created_at,
updated_at,event_cursor,usage}`. States are `queued`, `running`,
`cancel_requested`, `cancelled`, `succeeded`, `failed`, `interrupted`, or
`stop_unconfirmed`. The last state is not terminal and blocks replacement runs,
account changes and retention changes until the old environment is confirmed
stopped; its quarantine remains across an adapter restart.
Authentication states are `needs_auth`, `configured`, `ready`, `needs_reauth`.
Configured means a key was stored, not that the account was tested. Logout and
model/key rotation cancel work before revoking old sessions. There is no fallback
to another account/provider.

Codex device login uses the pinned upstream Hermes request/poll/exchange and
credential-store helpers. The verification page is fixed to
`https://auth.openai.com/codex/device`. Handles report `pending`, `configured`,
`failed`, `cancelled`, `expired`, `interrupted` or `stop_unconfirmed`. URL/code
may initially be null; the code is removed on completion. Starting a login
writes only provider/model/epoch metadata to `provider.json`, revokes old
contexts and uses a new private `/data/oauth/<epoch>/auth.json`. The adapter
does not import a CLI credential, copy refresh tokens, or share a writable home
with the Runner. `auth.adopt_external_logins` is disabled. Upstream token expiry,
revocation or model/account limits can still require a fresh sign-in.

This is a separate Hermes login after coding CLI sign-in. Completing its OAuth
exchange means `configured`; only a successful inference sets `ready` and
`auth_verified_at`. Capabilities advertises supported `auth_methods` and
`default_models`; `gpt-6-astra` follows the packaged Codex model catalog, and actual
account availability remains subject to inference verification. Other CLI
providers do not implicitly grant Hermes an account.

One run is admitted at a time, with a five-minute deadline, 12 model iterations,
32 KiB answer limit, 192 KiB retained context limit, and bounded process output.
Sessions expire at the configured context limit (default seven days, configurable
from one to seven) after creation even while actively used; runs and answers use
the same limit. Reducing the limit stops active inference before removing expired
context, so a finishing turn cannot restore erased history. Increasing it never
restores deleted content. The setting persists across runtime restarts and is
included as `context_days` in capabilities. Expired content is removed on startup
and API activity, plus an idle maintenance pass every 60 seconds. Minimal idempotency tombstones remain, preventing an expired
request from being replayed. At most 1,000 sessions and 10,000 run payloads are
retained. A restarted adapter marks unfinished runs `interrupted` and does not
automatically replay them. Cancellation checks all non-zombie processes in the
owned process group, freezes surviving descendants, then sends SIGKILL through
PID handles and verifies termination. A leader exiting is insufficient. Process
start times and PID handles prevent accidentally signaling a reused PID. Linux
parent-death fencing and the service container lifetime stop a turn if its adapter
dies; an unconfirmed group retains its scratch for later reconciliation.

## MCP and authority

Each turn starts a fresh process with a fresh private `HOME`; only
that session's bounded history is passed. API key and per-run MCP grant enter via
stdin, never argv or bridge records. The child uses the pinned Hermes official
HTTP MCP client against fixed `http://127.0.0.1:8097/mcp`. A deployment-time
`AGENTIC_MCP_URL` override is available to the trusted installer only. Controller
implements stateless JSON MCP `initialize`, `tools/list`, `tools/call`, and
`notifications/initialized`; standard optional `_meta` is permitted by MCP.
OpenRouter also gets a fresh `HERMES_HOME`. Native Codex uses its role-owned
OAuth home so upstream refreshes update one authoritative store without copying
single-use grants; inference remains serialized and memory/session persistence
is disabled. Hermes owns the reasoning loop through `codex_responses`, not the
optional Codex app-server runtime with its separate native tool authority.

Allowed tools are exactly `host.status`, `worker.capabilities`, `worker.submit`,
`worker.status`, `worker.cancel`, `worker.result`, `case.evidence.read`,
`home.observe`, `home.propose`, and `home.proposal_status`. Household observations
are policy filtered; proposals reuse the Controller compiler and exact Owner
approval flow. These tools cannot approve, apply, or call general HA services.
Discovery is filtered, `enabled_toolsets` is only `mcp-agentic`, the upstream
tool-search wrapper is disabled, and dispatch has an additional exact allowlist.
Built-in terminal, filesystem, browser, coding, delegate, memory, skills,
background review and plugin tools are not exposed. An unexpected model tool
cannot gain authority. Controller still checks the short-lived capability on
every MCP call and grants neither Owner approval nor general HA API access.

The process contains its own model credential and the scoped MCP token in memory;
they are not isolated from trusted upstream runtime code. The runtime's own
credential exposure is not claimed to be zero. Model-generated code execution
belongs in separately verified worker sandboxes, never in this orchestrator.

## Evidence and remaining acceptance

The packaged tests execute the real pinned Hermes agent and real official MCP
client against both a minimal MCP fixture and the actual Controller
`create_mcp`/`Coordinator`/`Store` path. The model endpoint and fixed host broker
observations are protocol fixtures. They check that an extra advertised
`terminal` tool is not exposed, that a diagnostic is called once with correctly
scoped evidence, and that its result feeds the next model turn. A provider `401`
fixture verifies the real upstream authentication error path. A separate image
startup check uses the default nonroot UID with no network and no cloud account.
Service tests verify identity scope, deduplication, continuation, retention,
secret-safe validation, auth revocation, restart interruption and actual local
subprocess cancellation.

The native Codex test uses the real Hermes Responses path and official credential
store with synthetic grants and a loopback model/MCP fixture. A separate offline
device-flow probe exercises actual upstream OAuth helpers through an HTTP mock,
checks 0600 auth storage and ensures device identifiers, verifier and tokens
never enter the public protocol. No real cloud account, household device, HAOS
App, laptop installation or E1/E2 coding acceptance is established by these
tests. A real Owner inference is still required to verify an account.

The trusted initial installer can run `/app/compatibility_probe.py` in an image
containing a candidate upstream tree. Supply a disk-backed writable `/work`,
UID 10003, read-only root, no capabilities, no external network, and
`HOME=/work HERMES_HOME=/work TMPDIR=/work PYTHONPATH=/opt/hermes:/app`.
The probe checks required upstream interfaces and completes a real AIAgent
tool roundtrip through the official MCP client and loopback protocol fixtures.
It does not authenticate an account or approve a deployment. Upstream changes
that fail the existing dependency lock, tool boundary or fixture stop activation.

The Host Runner's fixed bootstrap adapter is documented in
[BOOTSTRAP.md](../runner/agentic_runner/BOOTSTRAP.md). It invokes the selected
packaged CLI and returns validated advice; only trusted installation operations
may alter the runtime or Home Assistant.

Official references: [Hermes source](https://github.com/NousResearch/hermes-agent/tree/835c3dd038d903182a66ecc45e34bf5e0de65f00),
[MCP integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp),
[upstream API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server).
[Native provider authentication](https://hermes-agent.nousresearch.com/docs/integrations/providers)
and [Codex login](https://learn.chatgpt.com/docs/developer-commands#codex-login)
document the separate login flows.
The product API above wraps AIAgent directly; it does not claim to be the
upstream Gateway API or a remote implementation of `delegate_task`.

# OCS

> [!WARNING]
> This project is 100% vibe-coded. Review the code, tests, and behavior before relying on it.

OCS is an agent-friendly OpenCode session orchestration CLI. It gives scripts, operators, and agents stable commands for probing an OpenCode server, managing sessions, admitting steering input, executing blocking prompts, watching progress, resolving blockers, validating server behavior, and tracking local dependency-ordered serial worker runs.

The CLI requires Python 3.9+ and uses only the Python standard library. Run it from this checkout with `bin/ocs`, or install the package entry point `ocs`.

## Quickstart

```bash
bin/ocs capabilities --server http://127.0.0.1:4096
bin/ocs create .
bin/ocs steer ses_1 "Keep the current approach; focus the failing auth refresh test."
bin/ocs run_blocking --session ses_1 "Finish the worker task"
```

Server selection uses `--server URL`, `OPENCODE_SERVER_URL`, `OPENCODE_SERVER`, then `http://127.0.0.1:4096`.

Compact capability output includes the detected blocking execution route:

```text
health=ok version=1.2.3 session=/api/session prompt=/api/session/{sessionID}/prompt wait=/api/session/{sessionID}/wait events=/api/event execution=/session/{sessionID}/message legacy=unsupported
```

## Features

- Capability detection with compact and JSON output.
- Read-only OpenAPI route discovery and guarded GET diagnostics.
- Session lifecycle commands: `create`, `list`, `inspect`/`get`, `delete`, `abort`, `fork`, and `children`.
- Durable input admission with `steer`, including queue delivery through `--delivery queue`.
- Blocking prompt execution with `run_blocking`, using `/session/{sessionID}/message` or legacy run/reply fallback.
- Event watching with normalized compact output or JSON lines.
- OpenCode project, project-directory, and workspace metadata inventory.
- Permission and question blocker listing and resolution.
- Local `run` orchestration with dependency-ordered serial worker execution, persisted workers, dependencies, retries, timeouts, blockers, outputs, steering, and aborts.
- Deterministic smoke validation, opt-in live-provider validation, and disposable session cleanup.

## Command Map

- `capabilities`: probe OpenCode health, route support, event support, and execution support.
- `diagnostics routes|get`: inspect advertised routes and selected read-only API responses.
- `create`, `list`, `inspect`, `get`, `delete`, `abort`, `fork`, `children`: manage OpenCode sessions.
- `steer`: admit durable steer or queue input without waiting for an assistant reply.
- `run_blocking`: execute a prompt and wait for a terminal assistant result.
- `watch`: stream normalized events for one session until terminal state, abort, timeout, or stream end.
- `run init|worker|start|status|collect|steer|abort`: manage local orchestration runs and workers.
- `permission list|reply`: inspect and resolve permission blockers.
- `question list|answer|reject`: inspect and resolve question blockers.
- `project list|inspect|directories`, `workspace list`: inspect OpenCode project/workspace metadata.
- `project-copy cleanup`: dry-run or apply safe cleanup for metadata belonging to deleted project-copy directories.
- `smoke`, `live_validate`, `cleanup`: validate server behavior and clean disposable sessions.

`steer` is admission, not execution. It reports admission/progress state and intentionally does not use legacy run/reply fallback. `run_blocking` is execution and waits for an assistant reply or terminal failure.

The finalized short status terms are `queued`, `active`, `blocked`, `done`, `failed`, `aborted`, and `timeout`.

## Detailed Docs

- [Overview](docs/ocs/overview.md): mental model and common workflows.
- [Command Reference](docs/ocs/command-reference.md): commands, options, and output modes.
- [Server Compatibility](docs/ocs/server-compatibility.md): route detection and fallback behavior.
- [Sessions](docs/ocs/sessions.md): session inventory and lifecycle operations.
- [Prompt Control](docs/ocs/prompt-control.md): `steer`, `run_blocking`, and `watch`.
- [Orchestration](docs/ocs/orchestration.md): local run store, dependency-ordered serial worker execution, retries, and timeouts.
- [Blockers](docs/ocs/blockers.md): permission and question workflows.
- [Validation](docs/ocs/validation.md): smoke, live validation, cleanup, and E2E guidance.
- [Outputs And Exit Codes](docs/ocs/outputs-and-exit-codes.md): compact output, JSON contracts, statuses, and exit codes.

## Validation Safety

Live-provider validation is separate and opt-in only. It must not run as part of default smoke tests or mocked API tests. Run it only when you explicitly allow provider calls:

```bash
OCS_LIVE_VALIDATE=1 bin/ocs live_validate --directory /path/to/target --server http://127.0.0.1:4096
```

`live_validate` uses the minimal prompt `Reply exactly PONG.`. Expected token use is two minimal PONG prompts at most: one v2 steer admission and one blocking `run_blocking` execution. Disposable `ocs-live-` sessions are deleted before the command exits.

## Test Commands

Run the default deterministic unit suite without live server or model access. This command does not discover `tests/e2e/`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Optional E2E tests live under `tests/e2e/`. E2E runs every E2E test, including provider-consuming tests, and fails if the real server or default model is unavailable. Run them manually only when you have an OpenCode server and explicit approval to spend provider tokens.

```bash
PYTHONDONTWRITEBYTECODE=1 OCS_E2E_SERVER_URL=http://127.0.0.1 python3 -m unittest discover -s tests/e2e -p 'e2e_*.py'
```

E2E environment variables include `OCS_E2E_SERVER_URL`, `OCS_E2E_AGENT`, `OCS_E2E_MODEL`, and `OCS_E2E_TIMEOUT_SECONDS`.

## Exit Codes

- `0`: success.
- `64`: command usage error.
- `65`: data error.
- `66`: missing local run or blocker request.
- `69`: server/API unavailable or run failed before any worker completed.
- `70`: server reachable but missing required capabilities.
- `75`: run is blocked.
- `124`: run timed out.
- `130`: run was aborted.
- `1`: partial run failure after at least one worker completed.

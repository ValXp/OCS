# OCS

OCS is a lightweight OpenCode session orchestration CLI.

## Repository layout

- `bin/ocs`
  - A lightweight OpenCode session orchestration CLI. The first command probes server capabilities.
- `opencode_session/`
  - Python standard-library API client and capability detection code used by the CLI.
- `tests/`
  - Unit and UX-contract tests for the CLI and API client.
- `tests/e2e/`
  - Skipped-by-default subprocess E2E tests for an existing OpenCode server.

## Prerequisites

- A shell environment that can run the included Bash script
- `python3` for `bin/ocs` and its tests

## OpenCode session CLI

Probe a local or configured OpenCode server:

```bash
bin/ocs capabilities --server http://127.0.0.1:4096
```

Default output is compact:

```text
health=ok version=1.2.3 session=/api/session prompt=/api/session/{sessionID}/prompt wait=/api/session/{sessionID}/wait events=/api/event legacy=unsupported
```

Use `--json` for the stable capability contract:

```bash
bin/ocs capabilities --server http://127.0.0.1:4096 --json
```

Admit durable steering input to a session without promising an assistant reply:

```bash
bin/ocs steer ses_1 "Keep the current approach; focus the failure in auth refresh."
```

Compact `steer` output reports admission/progress state, not task completion:

```text
steer session=ses_1 message=msg_123 delivery=steer status=queued admitted=4 promoted=-
```

Queue delivery is exposed under `steer` rather than as a competing top-level command:

```bash
bin/ocs steer ses_1 "Run the benchmark after the current turn." --delivery queue
```

Execute a task and wait for an assistant reply or terminal failure with `run_blocking`:

```bash
bin/ocs run_blocking --session ses_1 "Finish the worker task"
```

Compact `run_blocking` output reports terminal state with short status terms:

```text
run_blocking session=ses_1 status=done user=msg_user_1 assistant=msg_assistant_1 cost=0.015 tokens=20 text="Worker finished."
```

Multi-item compact output uses a small table; single session or worker output stays one concise status line.

JSON output includes API path, fallback behavior, session ID, prompt/message ID, worker role where applicable, and terminal state.

Local orchestration runs are managed with `ocs run`. Workers can declare retry and timeout policy in local metadata before `start`:

```bash
bin/ocs run --store .ocs/runs worker demo builder --role build --prompt "Run tests" --retry-limit 2 --retryable api --retryable provider --timeout-seconds 600 --timeout-policy timeout
```

Retryable failure categories are `api`, `provider`, `timeout`, or `all`. Timeout policy can mark the worker `timeout`, `blocked`, `failed`, or `aborted`. JSON run status includes retry counts, retry limits, retryable categories, timeout metadata, failure category/reason, and `next_eligible_action`.

The finalized short status terms remain `queued`, `active`, `blocked`, `done`, `failed`, `aborted`, and `timeout`. Longer orchestration states map to those terms: pending is `queued`, running and retrying are `active` with `next_eligible_action`, complete is `done`, and timed out is `timeout`. Deleted session cleanup is reported in worker `cleanup.deleted` while the worker status remains the work outcome.

Live-provider validation is separate and opt-in only. It must not run as part of default smoke tests or mocked API tests.

Run optional live-provider validation only when you explicitly allow provider calls:

```bash
OCS_LIVE_VALIDATE=1 bin/ocs live_validate --directory /path/to/target --server http://127.0.0.1:4096
```

`live_validate` uses the minimal prompt `Reply exactly PONG.`. Expected token use is two minimal PONG prompts at most: one v2 steer admission and one legacy run/reply used by `run_blocking`. It records v2 steer admission, v2 wait availability, and the legacy run/reply result. Live validation creates disposable `ocs-live-` sessions and verifies they are deleted before the command exits.

Run a deterministic smoke check in no-live-model mode:

```bash
bin/ocs smoke --directory /path/to/target --server http://127.0.0.1:4096
```

Default smoke verifies health, capabilities, disposable create/delete cleanup, v2 steer admission, event stream connectivity, and blocker listing. Legacy run/reply execution is route-checked and reported as skipped in no-live-model mode; no provider-backed prompt is sent.

Smoke sessions use the recognizable `ocs-smoke-` prefix and are deleted before the command exits. Remove stale disposable sessions left by interrupted runs:

```bash
bin/ocs cleanup --directory /path/to/target --prefix ocs-smoke-
```

Run the default deterministic unit suite without live server or model access:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Optional E2E tests live under `tests/e2e/` and are not discovered by the default unit command. They run `bin/ocs` as a subprocess against an existing OpenCode server and are no-live-model by default; the current tracer only probes `capabilities --json` and does not send prompts.

E2E environment variables:

- `OCS_E2E_SERVER_URL`: existing OpenCode server URL. When unset, E2E tests are skipped cleanly.
- `OCS_E2E_TIMEOUT_SECONDS`: optional subprocess timeout in seconds. Default: `20`.

Run the E2E harness explicitly:

```bash
PYTHONDONTWRITEBYTECODE=1 OCS_E2E_SERVER_URL=http://127.0.0.1:4096 python3 -m unittest discover -s tests/e2e -p 'e2e_*.py'
```

Server selection:

- `--server URL`
- `OPENCODE_SERVER_URL`
- `OPENCODE_SERVER`
- Default: `http://127.0.0.1:4096`

Exit codes:

- `0`: capability probe succeeded
- `64`: command usage error
- `69`: server unavailable or health response unreadable
- `70`: server is reachable but lacks required session/prompt capabilities

Run policy exit codes:

- `0`: run completed with all workers `done`
- `1`: partial failure after at least one worker completed
- `75`: run is blocked
- `124`: run timed out
- `130`: run was aborted
- `69`: run failed before any worker completed, or the server/API is unavailable

## Notes

- OCS uses only Python standard-library modules.
- Live-provider validation is gated by `OCS_LIVE_VALIDATE=1` and is not part of the default test suite.

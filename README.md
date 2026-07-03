# ai_tools

Small utilities and Codex-facing assets for local automation.

## Repository layout

- `ralph-wiggum/`
  - A simple loop that repeatedly runs Codex against a fixed prompt until a `stop.md` file appears.
- `bin/ocs`
  - A lightweight OpenCode session orchestration CLI. The first command probes server capabilities.
- `opencode_session/`
  - Python standard-library API client and capability detection code used by the CLI.
- `skills/tmux-codex-orchestrator/`
  - A Codex skill for running worker Codex instances inside tmux panes while keeping a controller pane in the foreground.

## Prerequisites

- `codex` installed and available on `PATH`
- `tmux` installed for the orchestration skill
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

## Ralph Wiggum loop

`ralph-wiggum/work_loop.sh` runs Codex in a loop using the contents of `ralph-wiggum/prompt.md`.

Default behavior:

- Reads the prompt from [`ralph-wiggum/prompt.md`](ralph-wiggum/prompt.md)
- Stops when `stop.md` exists in the current working directory
- Sleeps `1` second between runs
- Uses `codex --dangerously-bypass-approvals-and-sandbox exec`

Example:

```bash
cd /path/to/target-repo
/root/ai_tools/ralph-wiggum/work_loop.sh
```

Useful environment variables:

- `STOP_FILE`
  - Override the stop-file path. Default: `"$PWD/stop.md"`
- `CODEX_CMD`
  - Override the Codex binary name. Default: `codex`
- `CODEX_ARGS`
  - Override the Codex argument string. Default: `--dangerously-bypass-approvals-and-sandbox exec`
- `CODEX_PROMPT_FLAG`
  - Used only for non-`exec` invocation styles
- `CODEX_SLEEP_SECONDS`
  - Delay between loop iterations. Default: `1`

Related prompt files:

- [`ralph-wiggum/prompt.md`](ralph-wiggum/prompt.md)
  - Instructions for the work loop agent
- [`ralph-wiggum/task-prompt.md`](ralph-wiggum/task-prompt.md)
  - Instructions for generating a `tasks.md` from an implementation plan

## tmux Codex orchestrator skill

The tmux skill is designed for controller-worker delegation inside tmux.

Core behavior:

- Launch worker Codex instances in tmux panes
- Prefer same-window splits
- Keep the controller pane visible
- Use file-backed payloads plus `tmux wait-for` for worker handoff
- Use unique prompt and payload files under `/tmp`

Key files:

- [`skills/tmux-codex-orchestrator/SKILL.md`](skills/tmux-codex-orchestrator/SKILL.md)
  - Main skill instructions
- [`skills/tmux-codex-orchestrator/references/commands.md`](skills/tmux-codex-orchestrator/references/commands.md)
  - Direct command recipes
- [`skills/tmux-codex-orchestrator/agents/openai.yaml`](skills/tmux-codex-orchestrator/agents/openai.yaml)
  - UI-facing skill metadata

Typical worker handoff:

1. Controller creates a unique prompt file, payload file, and event channel.
2. Controller launches the worker pane with the initial prompt attached on the Codex command line.
3. Worker writes a payload file whose first line is `STATUS=done` or `STATUS=blocked`.
4. Worker signals completion or blocker state with `tmux wait-for -S`.
5. Controller waits on `tmux wait-for`, then reads the payload file.

## Notes

- The repo is intentionally small and mostly contains automation assets rather than a conventional application.
- The tmux skill assumes the controller is responsible for orchestration and the worker is responsible only for delegated execution.

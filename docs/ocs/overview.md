# OCS Overview

OCS is a thin CLI over an existing OpenCode server. The server owns sessions, messages, event streams, permission requests, and question requests. OCS adds stable command names, compact status lines, JSON output, local orchestration records, and validation commands for automation.

## Mental Model

Sessions are durable OpenCode work contexts. Use `create`, `list`, `inspect`, `delete`, `abort`, `fork`, and `children` to manage them.

Prompts have two different paths. `steer` admits durable input through the v2 prompt route and returns admission/progress state. It does not wait for an assistant reply and does not use legacy run/reply fallback. `run_blocking` executes a prompt and waits for an assistant reply or terminal failure, using `/session/{sessionID}/message` or legacy run/reply when available.

Events are observation. `watch` streams normalized events for one session and can stop on terminal status, abort, timeout, or stream end.

Runs are local orchestration records. `run` stores workers under `OCS_RUN_STORE` or `.ocs/runs`, including dependencies, prompts, sessions, retry policy, timeout policy, blockers, output refs, and collected results.

Blockers are server-side permission or question requests. Use `permission` and `question` commands to list and resolve them.

Validation is split by cost. `smoke` is deterministic no-live-model validation. `live_validate` requires `OCS_LIVE_VALIDATE=1`, uses `Reply exactly PONG.`, may spend provider tokens, and verifies disposable sessions are deleted before exit.

## Common Workflows

Probe a server, admit input, execute a task, and watch progress:

```bash
bin/ocs capabilities --server http://127.0.0.1:4096
bin/ocs steer ses_1 "Continue from the failing test."
bin/ocs steer ses_1 "Run benchmarks after this turn." --delivery queue
bin/ocs run_blocking --session ses_1 "Finish the task"
bin/ocs watch ses_1 --timeout 120
```

Create and run a local orchestration record:

```bash
bin/ocs run init demo --directory .
bin/ocs run worker demo builder --role build --prompt "Run tests" --retry-limit 2 --retryable api --retryable provider --timeout-seconds 600 --timeout-policy timeout
bin/ocs run start demo
bin/ocs run status demo
bin/ocs run collect demo
```

Resolve blockers:

```bash
bin/ocs permission list --session ses_1
bin/ocs permission reply per_1 once
bin/ocs question list --session ses_1
bin/ocs question answer que_1 Ship
```

OCS normalizes status to `queued`, `active`, `blocked`, `done`, `failed`, `aborted`, and `timeout`. Local workers also expose `next_eligible_action`: `start`, `wait`, `resolve_blocker`, `collect`, `retry`, or `none`.

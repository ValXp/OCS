# Command Reference

This reference describes the CLI surface implemented in `opencode_session/commands`. Commands use compact output by default. Commands that expose structured data generally support `--json`; session and blocker inventory commands also support `--raw` for the exact API response body.

## Global Server Selection

Most server-backed commands accept `--server URL`. The default is selected in this order:

- `--server URL`
- `OPENCODE_SERVER_URL`
- `OPENCODE_SERVER`
- `http://127.0.0.1:4096`

## Capability Probe

`capabilities` probes server health and route support.

```bash
bin/ocs capabilities --server http://127.0.0.1:4096
bin/ocs capabilities --json
```

The compact output reports health, version, session route, v2 prompt route, v2 wait route, event route, blocking execution route, and legacy run/reply support.

## Session Commands

- `create DIRECTORY [--agent NAME] [--model NAME]`: create a session for a target directory.
- `list [--directory PATH] [--agent NAME] [--model NAME] [--blockers]`: list sessions with optional filters.
- `inspect SESSION_ID [--blockers]`: inspect one session.
- `get SESSION_ID [--blockers]`: alias for `inspect`.
- `delete SESSION_ID`: delete a session and verify it is no longer readable.
- `abort SESSION_ID`: request session abort.
- `fork SESSION_ID [--message-id ID]`: fork a session, optionally from a message.
- `children SESSION_ID [--directory PATH]`: list child sessions.

Session compact output includes normalized ID, title, directory, agent, model, cost, token total, and timestamps. `--blockers` adds permission/question counts.

## Prompt Commands

- `steer SESSION_ID TEXT [--delivery steer|queue] [--message-id ID]`: admit durable input and report admission state.
- `run_blocking [--session SESSION_ID] [--directory PATH] [--agent NAME] [--model NAME] PROMPT...`: execute a task and wait for a terminal result.
- `watch SESSION_ID [--json] [--timeout SECONDS]`: stream normalized session events.

If `run_blocking` has no prompt words, it reads prompt text from stdin. If `--session` is omitted, it creates a disposable session in `--directory` or `.` and deletes it before exit.

## Local Run Commands

All run commands accept `run --store PATH ...`. If omitted, the store is `OCS_RUN_STORE` or `.ocs/runs`.

- `run init NAME [--directory PATH] [--server URL]`: create a local run record.
- `run worker NAME WORKER_ID --role ROLE [options]`: add or update worker metadata.
- `run start NAME [--prompt TEXT] [--worker ID] [--role ROLE] [--directory PATH] [--server URL] [--session ID] [--agent NAME] [--model NAME] [--cleanup]`: start a single prompt or stored worker prompts.
- `run status NAME [--json]`: show persisted run state.
- `run collect NAME [--worker ID] [--json]`: print collected worker results.
- `run steer NAME WORKER_ID TEXT [--delivery steer|queue] [--message-id ID] [--json]`: admit input to a worker session and record the prompt ID.
- `run abort NAME WORKER_ID [--json]`: abort a worker session and mark the worker aborted when accepted.

Worker metadata options include `--session`, `--agent`, `--model`, `--prompt`, `--depends-on`, `--prompt-id`, `--status active|blocked`, `--retry-count`, `--retry-limit`, `--retryable`, `--timeout-seconds`, `--timeout-policy`, `--blocker`, and `--output-ref`. `--status blocked` requires at least one `--blocker`; terminal states are owned by `run start`, `run abort`, and result/failure/timeout reducers.

## Blocker Commands

- `permission list [--session SESSION_ID]`: list pending permission requests.
- `permission reply REQUEST_ID once|always|reject [--message TEXT]`: resolve a permission request.
- `question list [--session SESSION_ID]`: list pending question requests.
- `question answer REQUEST_ID ANSWER...`: answer with one or more labels/text values.
- `question answer REQUEST_ID --answers-json JSON`: submit nested answer arrays for multi-select questions.
- `question reject REQUEST_ID`: reject a question request.

## Validation Commands

- `smoke [--directory PATH] [--prefix PREFIX] [--event-timeout SECONDS] [--event-limit N] [--json]`: run deterministic no-live-model validation.
- `live_validate [--directory PATH] [--prefix PREFIX] [--agent NAME] [--model NAME] [--json]`: run opt-in live-provider validation when `OCS_LIVE_VALIDATE=1`.
- `cleanup [--directory PATH] [--prefix PREFIX] [--json]`: delete stale disposable sessions matching a prefix and directory.

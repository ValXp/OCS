# Prompt Control

OCS separates prompt admission, blocking execution, and observation. This keeps automation honest about whether a command merely accepted input or actually waited for an assistant result.

## Steer

```bash
bin/ocs steer ses_1 "Keep the current approach."
bin/ocs steer ses_1 "Run benchmarks after this turn." --delivery queue
bin/ocs steer ses_1 "Do not duplicate this." --message-id msg_client_1
```

`steer` admits durable input through the v2 prompt route. It returns admission/progress state, not task completion. Delivery modes are `steer` and `queue`; queue delivery is intentionally exposed through `steer --delivery queue` rather than a separate top-level command.

`--message-id` lets callers provide an idempotency key. A matching duplicate admission response is treated as an admitted replay.

The JSON output includes session ID, message ID, delivery, state, short status, raw state, API path, fallback availability, and sequence metadata when supplied by the server.

## Run Blocking

```bash
bin/ocs run_blocking --session ses_1 "Finish the task"
printf 'Finish the task\n' | bin/ocs run_blocking --session ses_1
bin/ocs run_blocking --directory /path/to/project --agent build "Reply exactly PONG."
```

`run_blocking` executes a task and waits for an assistant reply or terminal failure. It prefers `POST /session/{sessionID}/message`; if unavailable, it can use legacy `run` plus `reply` when both legacy routes exist.

When `--session` is omitted, `run_blocking` creates a disposable session in `--directory` or `.`, executes the prompt, then deletes the session before exit. Cleanup failure is treated as an API failure.

Provider failure is reported separately from API failure when the response has a failed/error status or an error payload.

## Watch

```bash
bin/ocs watch ses_1
bin/ocs watch ses_1 --json --timeout 120
```

`watch` streams events for one session and filters out events for other sessions. Compact output normalizes event kinds such as `admission`, `prompt`, `step`, `tool`, `text`, `blocker`, `error`, and `status`.

Text deltas from the same message are coalesced in compact output. JSON mode prints one normalized JSON object per event without coalescing.

Terminal statuses stop the watcher. Aborted sessions exit with the aborted exit code; timeouts exit with the timeout exit code; malformed event streams exit with a data error.

# Outputs And Exit Codes

OCS output is designed for both humans and automation. Compact output is the default. JSON output is available for stable contracts. Raw output is available on inventory-style commands when the exact server body matters.

## Compact Output

Compact output uses `key=value` fields and small tab-separated tables for multi-item results. Missing values are printed as `-`. Values with spaces are quoted.

Examples:

```text
steer session=ses_1 message=msg_123 delivery=steer status=queued admitted=4 promoted=-
run_blocking session=ses_1 status=done user=msg_user_1 assistant=msg_assistant_1 cost=0.015 tokens=20 text="Worker finished."
abort session=ses_1 accepted=true status=active
```

## JSON Output

Use `--json` for automation. JSON payloads include normalized IDs and route metadata where useful. `run status --json` prints the persisted local run record, including worker metadata and collected results.

`watch --json` prints one normalized event JSON object per line.

## Raw Output

Use `--raw` on session and blocker inventory commands to print the exact server response body. Raw output is useful when debugging server compatibility or schema changes.

## Status Terms

OCS maps longer server or orchestration states to short status terms:

- `queued`: pending, initialized, submitted, admitted.
- `active`: running, started, promoted, processing, in progress, aborting.
- `blocked`: waiting or needs input.
- `done`: complete, completed, success, succeeded, idle.
- `failed`: failure, error, errored.
- `aborted`: abort, aborted, cancelled, canceled.
- `timeout`: timeout, timed out.

Workers also expose `next_eligible_action`: `start`, `wait`, `resolve_blocker`, `collect`, `retry`, or `none`.

## CLI Exit Codes

- `0`: success.
- `64`: command usage error.
- `65`: data error, such as malformed question answer JSON or invalid event stream.
- `66`: missing local run record or missing blocker request.
- `69`: server unavailable, API failure, provider failure, or run failed before any worker completed.
- `70`: reachable server lacks required capabilities.
- `75`: run is blocked.
- `124`: watch or run timed out.
- `130`: run or watch was aborted.
- `1`: partial run failure after at least one worker completed.

## Run Exit Policy

Run status controls process exit. A fully done run exits `0`. A blocked run exits `75`. A timed-out run exits `124`. An aborted run exits `130`. A failed run exits `69`, unless at least one prompted worker completed first, in which case it exits `1` for partial failure.

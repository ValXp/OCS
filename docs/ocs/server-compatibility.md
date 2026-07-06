# Server Compatibility

OCS detects OpenCode server behavior from health endpoints and the OpenAPI document at `/doc`. It is intentionally tolerant of route variants so one CLI can work across current and older server builds.

## Health

Health probing tries these paths:

- `/global/health`
- `/api/health`
- `/health`

The compact capability output reports `health=` and `version=`. Version falls back to `unknown` when the health body does not expose a version field.

## Required Capability Baseline

`capabilities` treats a server as supported when it has session control plus either durable prompt admission or blocking execution.

Session control candidates:

- `POST /api/session`
- `POST /session`

Prompt admission candidates:

- `POST /api/session/{sessionID}/prompt`
- `POST /session/{sessionID}/prompt_async`

Blocking execution candidates:

- `POST /session/{sessionID}/message`
- legacy `POST /session/{sessionID}/run` plus `POST /session/{sessionID}/reply`

## Prompt And Wait Routes

OCS reports v2 prompt support separately from v2 wait support. Wait candidates are:

- `POST /api/session/{sessionID}/wait`
- a `wait` query parameter on the detected prompt route, reported as `?wait=true`

`steer` requires v2 prompt support. It does not fall back to legacy run/reply because admission and execution have different semantics.

## Blocking Execution Routes

`run_blocking` and local `run start` prefer `POST /session/{sessionID}/message` when available. If that route is not available, OCS falls back to legacy run/reply only when both legacy routes are present.

The JSON result includes `execution_strategy`, `api_path`, and `fallback` so automation can tell which route was used.

## Event Routes

Event support is detected from:

- `GET /api/event`
- `GET /event`
- `GET /global/event`

`watch` requires an event route. It accepts server-sent event framing and bare JSON event lines. Invalid event stream JSON exits as a data error.

## Session Payload Normalization

The API client normalizes common aliases such as `sessionID`, `sessionId`, `cwd`, `agentID`, `modelID`, `tokenUsage`, and timestamp aliases into the compact session fields used by the CLI.

When a command supports `--raw`, it prints the exact API response body and bypasses normalization.

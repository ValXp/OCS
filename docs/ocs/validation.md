# Validation

OCS validation commands are designed to separate deterministic server checks from provider-consuming checks.

## Smoke

```bash
bin/ocs smoke --directory /path/to/target --server http://127.0.0.1:4096
bin/ocs smoke --directory /path/to/target --json
```

Default smoke validation is no-live-model mode. It verifies health, capabilities, disposable session create/delete cleanup, v2 steer admission, event stream connectivity, and blocker listing. Blocking execution is route-checked and reported as skipped; no provider-backed prompt is sent.

Smoke sessions use the `ocs-smoke-` prefix by default and are deleted before the command exits.

Options include `--prefix`, `--event-timeout`, `--event-limit`, and `--json`.

## Live Validate

```bash
OCS_LIVE_VALIDATE=1 bin/ocs live_validate --directory /path/to/target --server http://127.0.0.1:4096
```

`live_validate` is gated by `OCS_LIVE_VALIDATE=1` before any server request is made. It uses `Reply exactly PONG.`, creates disposable `ocs-live-` sessions, validates v2 steer admission, records wait availability, runs blocking execution, verifies the assistant replied exactly `PONG`, and verifies disposable sessions are deleted before exit.

Use `--agent` and `--model` when the server should use a non-default configured agent or model.

## Cleanup

```bash
bin/ocs cleanup --directory /path/to/target --prefix ocs-smoke-
bin/ocs cleanup --directory /path/to/target --prefix ocs-live-
```

`cleanup` lists sessions, filters by directory and recognizable prefix, deletes matches, and verifies each deleted session is no longer readable.

The prefix can match session ID, title/name, or disposable metadata fields.

## Manual E2E

Optional E2E tests live under `tests/e2e/` and are not discovered by the default unit command. They run `bin/ocs` as a subprocess against an existing OpenCode server selected with `OCS_E2E_SERVER_URL`; the harness does not start, manage, mock, or skip the server.

When discovered, E2E runs every E2E test, including provider-consuming tests, and fails if the real server or default model is unavailable. Run them manually only when you have explicit approval to spend provider tokens.

```bash
PYTHONDONTWRITEBYTECODE=1 OCS_E2E_SERVER_URL=http://127.0.0.1 python3 -m unittest discover -s tests/e2e -p 'e2e_*.py'
```

E2E environment variables:

- `OCS_E2E_SERVER_URL`: existing OpenCode server URL.
- `OCS_E2E_AGENT`: optional OpenCode agent for live E2E commands.
- `OCS_E2E_MODEL`: optional provider model for live E2E commands.
- `OCS_E2E_TIMEOUT_SECONDS`: optional subprocess timeout in seconds.

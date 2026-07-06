# Orchestration

The `run` command manages local orchestration records. The OpenCode server still owns sessions and messages; OCS stores local worker metadata, dependency state, retry policy, timeout policy, blockers, outputs, and collected results.

Prompted workers execute in dependency-ordered serial steps. A single `run start` selects at most one ready worker, executes it, persists the result, and replans; independent ready workers do not run in parallel.

## Store Location

```bash
bin/ocs run --store .ocs/runs init demo --directory . --server http://127.0.0.1:4096
```

If `--store` is omitted, OCS uses `OCS_RUN_STORE` or `.ocs/runs` under the current directory. Records are JSON files guarded by file locks and normalized to schema version 1 when loaded.

## Run Records

A run record contains name, run ID, target directory, server URL, status, output refs, timestamps, and a worker map. `run status --json` prints the full record.

Compact run output includes worker status counts and output refs. A single worker prints as one compact line; multiple workers print as a small table.

## Workers

```bash
bin/ocs run worker demo builder --role build --prompt "Run tests"
bin/ocs run worker demo reviewer --role review --depends-on builder --prompt "Review the result"
```

Worker metadata can include session ID, agent, model, prompt, dependencies, prompt IDs, status, retry count, retry limit, retryable failure categories, timeout settings, blockers, and output refs.

If a worker has no existing session, `run start` creates one in the run directory. If a worker already has a session ID, OCS reuses it. `--cleanup` deletes sessions created by that start, but does not delete preexisting worker sessions.

## Starting Work

```bash
bin/ocs run start demo
bin/ocs run start demo --worker builder --prompt "Run tests" --session ses_existing
```

Without `--prompt`, `run start` executes stored prompted workers one at a time in dependency order. With `--prompt`, it starts or updates one worker and executes that prompt.

Serial execution is intentional product behavior. `--execution-policy continue` keeps moving to the next dependency-eligible worker after a failure, but it still runs one selected worker at a time and does not enable parallel worker execution.

Worker execution uses the same blocking execution strategy as `run_blocking`. Results store message IDs, status, terminal state, API path, fallback metadata, cost, tokens, and assistant text.

## Dependencies

Workers with dependencies run only after dependencies are `done`. Failed, blocked, aborted, or timed-out dependencies block dependent workers. OCS also blocks dependency cycles and dependencies on workers that are queued but have no prompt.

Blocked workers get blocker references such as `dependency:builder`, `dependency-cycle:a->b->a`, or `dependency-not-runnable:setup`.

## Retries

```bash
bin/ocs run worker demo builder --role build --retry-limit 2 --retryable api --retryable provider
```

Retryable categories are `api`, `provider`, `timeout`, and `all`. A retry increments `retry_count`, records the last failure category/reason, and keeps the worker active until retry policy is exhausted or the worker succeeds.

Timeout retries use a newly created session so an in-flight timed-out session is not reused.

## Timeouts

```bash
bin/ocs run worker demo builder --role build --timeout-seconds 600 --timeout-policy timeout
```

Timeout policy can map a timed-out worker to `timeout`, `blocked`, `failed`, or `aborted`. A blocked timeout adds the `timeout` blocker and exposes `next_eligible_action=resolve_blocker`.

## Control And Collection

```bash
bin/ocs run steer demo builder "Narrow the fix to auth refresh."
bin/ocs run abort demo builder
bin/ocs run collect demo --worker builder
```

`run steer` targets a worker session and records the admitted prompt ID. `run abort` targets the worker session and marks the worker aborted when accepted. `run collect` prints the stored result for one worker or completed workers in dependency order.

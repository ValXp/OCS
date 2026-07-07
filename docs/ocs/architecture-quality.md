# Architecture Quality Debt Plan

The architecture quality gate treats long-file exceptions as temporary debt. Growth above the current grandfathered ceiling fails immediately, shrinkage below the ceiling fails until the ceiling is lowered to the new current count, and every grandfathered file has a ratchet target in `tests/test_architecture_quality.py`.

The ratchet target for every long-file exception is the normal source-file limit: 300 lines. The target is explicit, but current debt is not failed until planned decomposition work lands. Direct `worker_state` decomposition is intentionally deferred and must not become an immediate blocking gate from this plan.

| File | Current ceiling | Target | Decomposition direction |
| --- | ---: | ---: | --- |
| `opencode_session/multi_worker_orchestration.py` | 500 | 300 | Extract worker selection, dependency planning, and multi-worker status rendering seams. |
| `opencode_session/remote_journal.py` | 732 | 300 | Split journal record codecs, persistence, and runner/reporting responsibilities. |
| `opencode_session/run_services.py` | 418 | 300 | Move run command mutations into narrower service modules around init, worker updates, and collection. |
| `opencode_session/validation_live.py` | 303 | 300 | Split live validation stages from response assertion helpers. |
| `opencode_session/worker_field_spec.py` | 386 | 300 | Decouple worker field specification/parsing from worker state hydration details. |
| `opencode_session/worker_session_provisioning.py` | 438 | 300 | Split session creation, reuse, and cleanup policy paths. |
| `opencode_session/worker_state.py` | 2395 | 300 | Defer direct decomposition, then extract serialization/hydration, lifecycle transition policy, retry/timeout/blocker policy, and import-cycle seams in separate issue-sized steps. |

When a file reaches 300 lines or less, remove it from the grandfathered list instead of keeping the exception. Any new long-file exception must add a target here and in `LONG_SOURCE_FILE_RATCHET_TARGETS`.

## `worker_state` Direct-Import Guardrail

Direct `worker_state` decomposition and known import-cycle removal are intentionally deferred. The architecture gate still freezes the current production importer set in `GRANDFATHERED_WORKER_STATE_DIRECT_IMPORTERS` so the debt cannot spread to new modules unnoticed.

The gate fails when a new production module imports `opencode_session.worker_state` directly without an explicit exception. It also fails when an importer stops depending on `worker_state` until the stale exception is removed from the test and this table.

| Importer | Current boundary debt |
| --- | --- |
| `opencode_session.cli_policy` | CLI policy still maps worker lifecycle outcomes from `worker_state`. |
| `opencode_session.multi_worker_orchestration` | Multi-worker orchestration still mutates worker records through `worker_state`. |
| `opencode_session.multi_worker_orchestration_contracts` | Orchestration contracts still expose worker state types/constants. |
| `opencode_session.multi_worker_orchestration_phases` | Phase helpers still read worker prompts through `worker_state`. |
| `opencode_session.run_formatting` | Run output formatting still uses legacy worker output projection. |
| `opencode_session.run_persistence` | Run persistence still hydrates workers through `worker_state`. |
| `opencode_session.run_prompt_worker` | Prompt-worker setup still imports queued lifecycle constants. |
| `opencode_session.run_record` | Run record normalization still depends on worker hydration helpers. |
| `opencode_session.run_services` | Run services still coordinate worker lifecycle mutations. |
| `opencode_session.run_start_core` | Run start still creates and initializes worker records directly. |
| `opencode_session.run_start_policy` | Start policy still marks worker failure through `worker_state`. |
| `opencode_session.worker_active_attempt_recovery` | Active-attempt recovery still mutates worker records directly. |
| `opencode_session.worker_attempt_log` | Attempt logging still uses `WorkerRecord` directly. |
| `opencode_session.worker_attempt_policy` | Attempt policy still reads lifecycle helpers directly. |
| `opencode_session.worker_cleanup_recovery` | Cleanup recovery still mutates worker records directly. |
| `opencode_session.worker_dependencies` | Dependency helpers are part of the known deferred cycle. |
| `opencode_session.worker_execution` | Worker execution still applies lifecycle updates directly. |
| `opencode_session.worker_field_spec` | Field spec still reads canonical lifecycle values lazily. |
| `opencode_session.worker_session_provisioning` | Session provisioning still hydrates and mutates worker records. |
| `opencode_session.worker_snapshot_transition` | Snapshot transition helpers still consume worker transition APIs. |
| `opencode_session.worker_storage_adapter` | Storage adapter still bridges persistence to `worker_state`. |

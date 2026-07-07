# Architecture Quality Debt Plan

The architecture quality gate treats long-file exceptions as temporary debt. Growth above the current grandfathered ceiling fails immediately, and every grandfathered file has a ratchet target in `tests/test_architecture_quality.py`.

The ratchet target for every long-file exception is the normal source-file limit: 300 lines. The target is explicit, but current debt is not failed until planned decomposition work lands. Direct `worker_state` decomposition is intentionally deferred and must not become an immediate blocking gate from this plan.

| File | Current ceiling | Target | Decomposition direction |
| --- | ---: | ---: | --- |
| `opencode_session/api_profile.py` | 337 | 300 | Separate route capability/profile parsing from presentation helpers. |
| `opencode_session/multi_worker_orchestration.py` | 569 | 300 | Extract worker selection, dependency planning, and multi-worker status rendering seams. |
| `opencode_session/remote_journal.py` | 732 | 300 | Split journal record codecs, persistence, and runner/reporting responsibilities. |
| `opencode_session/run_services.py` | 418 | 300 | Move run command mutations into narrower service modules around init, worker updates, and collection. |
| `opencode_session/schema_event_adapter.py` | 378 | 300 | Separate event envelope parsing from normalized event mapping. |
| `opencode_session/schema_message_adapter.py` | 326 | 300 | Separate message content extraction from execution metadata normalization. |
| `opencode_session/validation_live.py` | 303 | 300 | Split live validation stages from response assertion helpers. |
| `opencode_session/worker_field_spec.py` | 386 | 300 | Decouple worker field specification/parsing from worker state hydration details. |
| `opencode_session/worker_session_provisioning.py` | 438 | 300 | Split session creation, reuse, and cleanup policy paths. |
| `opencode_session/worker_state.py` | 2398 | 300 | Defer direct decomposition, then extract serialization/hydration, lifecycle transition policy, retry/timeout/blocker policy, and import-cycle seams in separate issue-sized steps. |

When a file reaches 300 lines or less, remove it from the grandfathered list instead of keeping the exception. Any new long-file exception must add a target here and in `LONG_SOURCE_FILE_RATCHET_TARGETS`.

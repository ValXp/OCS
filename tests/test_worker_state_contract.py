from copy import deepcopy
import unittest
from types import SimpleNamespace

from opencode_session.worker_state import (
    EXECUTABLE_WORKER_ACTIONS,
    EX_UNAVAILABLE,
    FAILED_DEPENDENCY_STATUSES,
    PUBLIC_WORKER_STATE_BY_LIFECYCLE,
    TERMINAL_WORKER_STATUSES,
    UNSET_TRANSITION_FIELD,
    WORKER_EXIT_CODE_BY_STATUS,
    WORKER_LIFECYCLE_METADATA,
    WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS,
    WORKER_LIFECYCLE_STATES,
    WORKER_STATUS_PRIORITY_BY_STATUS,
    WORKER_TRANSITION_METADATA,
    WorkerRecord,
    WorkerTransition,
    WorkerTransitionError,
    WorkerTransitionName,
    apply_worker_transition_to_worker,
    apply_worker_result,
    deserialize_worker_record,
    exit_code_for_status,
    exit_code_for_run,
    is_executable_worker,
    mark_dependency_blocked,
    mark_worker_aborted,
    mark_worker_active,
    mark_worker_failed,
    normalize_worker_snapshot,
    next_eligible_worker_action,
    normalize_worker,
    refresh_run_summary,
    require_internal_worker,
    schedule_worker_retry,
    serialize_worker_snapshot,
    status_priority,
    worker_lifecycle_source_states,
    worker_lifecycle_state,
    worker_lifecycle_state_for_status_alias,
    worker_lifecycle_target_states,
    worker_record_for_mutation,
)

try:
    from tests.worker_state_scenarios import WorkerScenario, assert_worker_outcome
except ModuleNotFoundError:
    from worker_state_scenarios import WorkerScenario, assert_worker_outcome


class WorkerStateContractTest(unittest.TestCase):
    def test_lifecycle_metadata_derives_public_status_policy_and_flags(self):
        self.assertEqual(WORKER_LIFECYCLE_STATES, frozenset(WORKER_LIFECYCLE_METADATA))
        self.assertEqual(
            PUBLIC_WORKER_STATE_BY_LIFECYCLE,
            {
                lifecycle_state: (metadata.status, metadata.next_eligible_action)
                for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
            },
        )
        self.assertEqual(
            TERMINAL_WORKER_STATUSES,
            frozenset(metadata.status for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.terminal_status),
        )
        self.assertEqual(
            FAILED_DEPENDENCY_STATUSES,
            frozenset(
                metadata.status for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.failed_dependency_status
            ),
        )
        self.assertEqual(
            EXECUTABLE_WORKER_ACTIONS,
            frozenset(
                metadata.next_eligible_action for metadata in WORKER_LIFECYCLE_METADATA.values() if metadata.executable
            ),
        )
        self.assertEqual(
            WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS,
            {
                metadata.status: lifecycle_state
                for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items()
                if metadata.status_alias
            },
        )
        self.assertEqual(WORKER_STATUS_PRIORITY_BY_STATUS, _metadata_by_status("status_priority"))
        self.assertEqual(WORKER_EXIT_CODE_BY_STATUS, _metadata_by_status("exit_code", skip_none=True))

    def test_lifecycle_metadata_feeds_status_helpers_and_reducer_legality(self):
        from opencode_session.commands.runs import _lifecycle_state_from_status_alias

        for status, lifecycle_state in WORKER_LIFECYCLE_STATE_BY_STATUS_ALIAS.items():
            with self.subTest(status=status):
                self.assertEqual(worker_lifecycle_state_for_status_alias(status), lifecycle_state)
                self.assertEqual(_lifecycle_state_from_status_alias(status), lifecycle_state)
                self.assertEqual(status_priority(status), WORKER_STATUS_PRIORITY_BY_STATUS[status])
                if status in WORKER_EXIT_CODE_BY_STATUS:
                    self.assertEqual(exit_code_for_status(status), WORKER_EXIT_CODE_BY_STATUS[status])

        for transition_name, metadata in WORKER_TRANSITION_METADATA.items():
            with self.subTest(transition=transition_name):
                self.assertEqual(worker_lifecycle_source_states(transition_name), metadata.source_states)
                self.assertEqual(worker_lifecycle_target_states(transition_name), metadata.target_states)

    def test_lifecycle_transition_views_are_derived_from_transition_metadata(self):
        source_transitions_by_state = {lifecycle_state: set() for lifecycle_state in WORKER_LIFECYCLE_METADATA}
        target_transitions_by_state = {lifecycle_state: set() for lifecycle_state in WORKER_LIFECYCLE_METADATA}
        for transition_name, metadata in WORKER_TRANSITION_METADATA.items():
            if not metadata.public_lifecycle_transition:
                continue
            for lifecycle_state in metadata.source_states:
                source_transitions_by_state[lifecycle_state].add(transition_name)
            for lifecycle_state in metadata.target_states:
                target_transitions_by_state[lifecycle_state].add(transition_name)

        for lifecycle_state, metadata in WORKER_LIFECYCLE_METADATA.items():
            with self.subTest(lifecycle_state=lifecycle_state):
                self.assertEqual(metadata.source_transitions, frozenset(source_transitions_by_state[lifecycle_state]))
                self.assertEqual(metadata.target_transitions, frozenset(target_transitions_by_state[lifecycle_state]))

    def test_normalize_worker_applies_defaults_and_derives_next_action(self):
        worker = normalize_worker(
            {
                "id": "",
                "status": "failed",
                "dependencies": "build",
                "retry_count": "1",
                "retry_limit": "2",
                "retryable_failures": ["api"],
                "last_failure_category": "api",
            },
            "review",
        )

        self.assertIsInstance(worker, WorkerRecord)
        self.assertNotIsInstance(worker, dict)
        self.assertEqual(worker["id"], "review")
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["dependencies"], [])
        self.assertEqual(worker["prompt_ids"], [])
        self.assertEqual(worker["timeout_policy"], "timeout")
        self.assertEqual(worker["lifecycle_state"], "failed_retry")
        self.assertEqual(worker["next_eligible_action"], "retry")

    def test_worker_execution_eligibility_derives_canonical_action(self):
        queued = {"id": "build", "prompt": "Build", "lifecycle_state": "queued"}
        waiting = {"id": "review", "prompt": "Review", "lifecycle_state": "active_wait"}
        retrying = {"id": "test", "prompt": "Test", "lifecycle_state": "active_retry"}
        stale_action = {
            "id": "docs",
            "prompt": "Docs",
            "lifecycle_state": "active_wait",
            "status": "active",
            "next_eligible_action": "retry",
        }

        self.assertTrue(is_executable_worker(queued))
        self.assertFalse(is_executable_worker(waiting))
        self.assertEqual(next_eligible_worker_action(waiting), "wait")
        self.assertTrue(is_executable_worker(retrying))
        self.assertFalse(is_executable_worker(stale_action))
        self.assertEqual(next_eligible_worker_action(stale_action), "wait")

    def test_deserialize_worker_derives_public_state_from_lifecycle(self):
        worker = deserialize_worker_record(
            {
                "lifecycle_state": "active_wait",
                "status": "failed",
                "next_eligible_action": "retry",
            },
            "review",
        )

        self.assertIsInstance(worker, WorkerRecord)
        self.assertNotIsInstance(worker, dict)
        assert_worker_outcome(self, worker, status="active", action="wait", lifecycle="active_wait")

    def test_worker_record_mutation_is_object_backed_not_snapshot_backed(self):
        snapshot = {
            "id": "review",
            "prompt_ids": ["msg_previous"],
            "lifecycle_state": "active_wait",
        }

        record = worker_record_for_mutation(snapshot, "review")
        record.remember_prompt_id("msg_new")

        self.assertIsInstance(record, WorkerRecord)
        self.assertNotIsInstance(record, dict)
        self.assertEqual(record["prompt_ids"], ["msg_previous", "msg_new"])
        self.assertEqual(snapshot["prompt_ids"], ["msg_previous"])

    def test_deserialize_worker_snapshot_hydrates_defaults_and_public_state(self):
        worker = deserialize_worker_record(
            {
                "status": "active",
                "next_eligible_action": "retry",
                "dependencies": "build",
            },
            "review",
        )

        self.assertEqual(worker["id"], "review")
        self.assertIsNone(worker["session_id"])
        self.assertEqual(worker["dependencies"], [])
        assert_worker_outcome(self, worker, status="active", action="retry", lifecycle="active_retry")

    def test_serialize_worker_snapshot_keeps_public_state_out_of_persisted_json(self):
        worker = deserialize_worker_record(
            {
                "id": "review",
                "session_id": "ses_review",
                "lifecycle_state": "done_collect",
                "status": "active",
                "next_eligible_action": "retry",
            },
            "review",
        )

        snapshot = serialize_worker_snapshot(worker, "review")

        self.assertIs(type(snapshot), dict)
        self.assertEqual(snapshot["lifecycle_state"], "done_collect")
        self.assertEqual(snapshot["session_id"], "ses_review")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_serialize_worker_snapshot_trusts_lifecycle_not_public_status(self):
        worker = {
            "id": "review",
            "lifecycle_state": "active_wait",
            "status": "done",
            "next_eligible_action": "collect",
        }

        snapshot = serialize_worker_snapshot(worker, "review")

        self.assertEqual(worker_lifecycle_state(worker), "active_wait")
        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_normalize_worker_snapshot_trusts_lifecycle_over_stale_public_status(self):
        snapshot = normalize_worker_snapshot(
            {
                "id": "review",
                "lifecycle_state": "active_wait",
                "status": "done",
                "next_eligible_action": "collect",
            },
            "review",
        )

        self.assertEqual(snapshot["lifecycle_state"], "active_wait")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_normalize_worker_snapshot_migrates_legacy_public_status_without_lifecycle(self):
        snapshot = normalize_worker_snapshot(
            {
                "id": "review",
                "status": "done",
                "next_eligible_action": "collect",
            },
            "review",
        )

        self.assertEqual(snapshot["lifecycle_state"], "done_collect")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_internal_worker_guard_reports_missing_required_fields(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            require_internal_worker({"id": "review"})

    def test_mark_worker_active_sets_waiting_action_and_timeout_start(self):
        scenario = WorkerScenario("builder", timeout_seconds=30).apply(
            lambda worker: mark_worker_active(worker, now=lambda: "2026-07-04T00:00:00Z")
        )
        worker = scenario.worker

        assert_worker_outcome(self, worker, status="active", action="wait", lifecycle="active_wait")
        self.assertEqual(worker["timeout_started_at"], "2026-07-04T00:00:00Z")

    def test_mark_worker_active_clears_stale_current_status_metadata(self):
        worker = normalize_worker(
            {
                "status": "blocked",
                "blockers": ["dependency:build"],
                "error": "previous failure",
                "failure_category": "api",
                "failure_reason": "previous failure",
                "failure_retryable": False,
                "last_failure_category": "api",
                "last_failure_reason": "previous failure",
            },
            "review",
        )

        apply_worker_transition_to_worker(worker, mark_worker_active(worker))

        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["blockers"], [])
        self.assertNotIn("error", worker)
        self.assertIsNone(worker["failure_category"])
        self.assertIsNone(worker["failure_reason"])
        self.assertNotIn("failure_retryable", worker)
        self.assertEqual(worker["last_failure_category"], "api")
        self.assertEqual(worker["last_failure_reason"], "previous failure")

    def test_failed_worker_records_failure_and_appends_prompt_ids(self):
        scenario = WorkerScenario(
            "review",
            status="active",
            next_eligible_action="wait",
            failure_retryable=False,
            prompt_ids=["msg_previous"],
        ).apply(lambda worker: mark_worker_failed(worker, "provider", "provider failed", prompt_ids=("msg_failed",)))
        worker = scenario.worker

        assert_worker_outcome(self, worker, status="failed", action="none", lifecycle="failed_terminal")
        self.assertEqual(worker["error"], "provider failed")
        self.assertEqual(worker["failure_category"], "provider")
        self.assertEqual(worker["failure_reason"], "provider failed")
        self.assertEqual(worker["prompt_ids"], ["msg_previous", "msg_failed"])
        self.assertNotIn("failure_retryable", worker)

    def test_retryable_failure_keeps_worker_retry_eligible(self):
        scenario = WorkerScenario(
            "review",
            status="active",
            next_eligible_action="wait",
            retryable_failures=["provider"],
            retry_count=0,
            retry_limit=1,
        ).apply(
            lambda worker: mark_worker_failed(worker, "provider", "provider failed")
        )
        worker = scenario.worker

        assert_worker_outcome(self, worker, status="failed", action="retry", lifecycle="failed_retry")
        self.assertEqual(worker["failure_category"], "provider")
        self.assertEqual(worker["failure_reason"], "provider failed")
        self.assertNotIn("failure_retryable", worker)

    def test_accepted_abort_preserves_late_result_but_keeps_prompt_ids(self):
        worker = normalize_worker(
            {
                "status": "active",
                "session_id": "ses_build",
                "prompt_ids": ["msg_initial"],
            },
            "build",
        )
        apply_worker_transition_to_worker(
            worker,
            mark_worker_aborted(worker, {"session_id": "ses_build", "accepted": True}),
        )

        apply_worker_transition_to_worker(
            worker,
            apply_worker_result(
                worker,
                {
                    "status": "done",
                    "message_ids": {"user": "msg_user", "assistant": "msg_assistant"},
                },
                prompt_ids=("msg_user",),
            ),
        )

        self.assertEqual(worker["status"], "aborted")
        self.assertEqual(worker["next_eligible_action"], "none")
        self.assertEqual(worker["abort"], {"session_id": "ses_build", "accepted": True})
        self.assertEqual(worker["prompt_ids"], ["msg_initial", "msg_user"])
        self.assertNotIn("result", worker)
        self.assertEqual(worker["output_refs"], [])

    def test_mark_dependency_blocked_records_blockers_and_resolution_action(self):
        worker = normalize_worker({}, "review")

        apply_worker_transition_to_worker(worker, mark_dependency_blocked(worker, ["dependency:build"]))

        assert_worker_outcome(
            self,
            worker,
            status="blocked",
            action="resolve_blocker",
            lifecycle="blocked_dependency",
            blockers=["dependency:build"],
        )

    def test_worker_scenario_asserts_retry_to_success_outcome(self):
        result = {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

        (
            WorkerScenario(
                "review",
                prompt="Review",
                status="failed",
                retryable_failures=["provider"],
                retry_count=0,
                retry_limit=1,
            )
            .apply(lambda worker: schedule_worker_retry(worker, "provider", "provider failed"))
            .assert_outcome(self, status="active", action="retry", lifecycle="active_retry")
            .apply(lambda worker: mark_worker_active(worker))
            .apply(lambda worker: apply_worker_result(worker, result, prompt_ids=("msg_user",)))
            .assert_outcome(
                self,
                status="done",
                action="collect",
                lifecycle="done_collect",
                output_refs=["assistant:msg_assistant"],
            )
        )

    def test_apply_worker_result_done_clears_stale_current_status_metadata(self):
        worker = normalize_worker(
            {
                "status": "active",
                "blockers": ["dependency:build"],
                "error": "previous failure",
                "failure_category": "api",
                "failure_reason": "previous failure",
                "failure_retryable": False,
                "last_failure_category": "api",
                "last_failure_reason": "previous failure",
            },
            "review",
        )

        apply_worker_transition_to_worker(
            worker,
            apply_worker_result(
                worker,
                {
                    "status": "done",
                    "message_ids": {"assistant": "msg_assistant"},
                },
            ),
        )

        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["blockers"], [])
        self.assertNotIn("error", worker)
        self.assertIsNone(worker["failure_category"])
        self.assertIsNone(worker["failure_reason"])
        self.assertNotIn("failure_retryable", worker)
        self.assertEqual(worker["last_failure_category"], "api")
        self.assertEqual(worker["last_failure_reason"], "previous failure")
        self.assertEqual(worker["output_refs"], ["assistant:msg_assistant"])

    def test_worker_transition_reducer_reports_invalid_lifecycle_transitions(self):
        from opencode_session.worker_lifecycle_reducer import apply_worker_transition_to_record

        original = normalize_worker(
            {
                "status": "done",
                "result": {
                    "status": "done",
                    "message_ids": {"assistant": "msg_done"},
                },
                "output_refs": ["assistant:msg_done"],
            },
            "review",
        )
        transitions = [
            mark_worker_active(original),
            mark_worker_failed(original, "provider", "late failure"),
            WorkerTransition.retry_scheduled("review", "provider", "retry", retry_count=1),
            WorkerTransition.timed_out(
                "review",
                "late timeout",
                status="timeout",
                timed_out_at="2026-07-04T00:00:00Z",
            ),
            WorkerTransition.result_applied(
                "review",
                {"status": "done", "message_ids": {"assistant": "msg_late"}},
            ),
        ]

        for transition in transitions:
            with self.subTest(transition=transition.name):
                worker = deepcopy(original)

                result = apply_worker_transition_to_record(WorkerRecord.from_worker(worker, "review"), transition)

                self.assertFalse(result.applied)
                self.assertTrue(result.skipped)
                self.assertIn(f"illegal worker transition '{transition.name.value}'", result.reason)
                self.assertIn("from lifecycle_state 'done_collect'", result.reason)
                with self.assertRaisesRegex(WorkerTransitionError, "illegal worker transition") as raised:
                    apply_worker_transition_to_worker(worker, transition)
                self.assertFalse(raised.exception.result.applied)
                self.assertEqual(raised.exception.result.reason, result.reason)
                self.assertEqual(worker, original)

    def test_worker_transition_reducer_allows_legal_retry_timeout_and_result_transitions(self):
        retry_worker = normalize_worker(
            {
                "status": "failed",
                "failure_category": "provider",
                "retryable_failures": ["provider"],
                "retry_count": 0,
                "retry_limit": 1,
            },
            "review",
        )
        apply_worker_transition_to_worker(retry_worker, schedule_worker_retry(retry_worker, "provider", "try again"))

        assert_worker_outcome(self, retry_worker, status="active", action="retry", lifecycle="active_retry")
        self.assertEqual(retry_worker["retry_count"], 1)

        timeout_worker = normalize_worker(
            {
                "status": "active",
                "retryable_failures": ["timeout"],
                "retry_count": 0,
                "retry_limit": 1,
            },
            "review",
        )
        apply_worker_transition_to_worker(
            timeout_worker,
            WorkerTransition.timed_out(
                "review",
                "worker timed out",
                status="failed",
                timed_out_at="2026-07-04T00:00:00Z",
                retry_available=True,
                manual_retry_required=True,
            ),
        )

        assert_worker_outcome(self, timeout_worker, status="failed", action="retry", lifecycle="timeout_failed_retry")
        self.assertTrue(timeout_worker["manual_retry_required"])

        result_worker = normalize_worker({"status": "active"}, "review")
        apply_worker_transition_to_worker(
            result_worker,
            WorkerTransition.result_applied(
                "review",
                {"status": "done", "message_ids": {"assistant": "msg_assistant"}},
            ),
        )

        assert_worker_outcome(self, result_worker, status="done", action="collect", lifecycle="done_collect")
        self.assertEqual(result_worker["output_refs"], ["assistant:msg_assistant"])

    def test_worker_transition_definitions_are_complete_and_cohesive(self):
        from opencode_session.worker_lifecycle_reducer import _WORKER_TRANSITION_DEFINITIONS, WorkerLifecycleReducer

        self.assertEqual(set(WorkerTransitionName), set(WORKER_TRANSITION_METADATA))
        self.assertEqual(set(WorkerTransitionName), set(_WORKER_TRANSITION_DEFINITIONS))
        for transition_name, definition in _WORKER_TRANSITION_DEFINITIONS.items():
            with self.subTest(transition=transition_name):
                metadata = WORKER_TRANSITION_METADATA[transition_name]
                self.assertIs(definition.metadata, metadata)
                self.assertIs(definition.name, transition_name)
                self.assertEqual(definition.source_states, metadata.source_states)
                self.assertEqual(definition.target_states, metadata.target_states)
                self.assertEqual(definition.target_lifecycle, metadata.target_lifecycle)
                self.assertIs(definition.apply_transition, getattr(WorkerLifecycleReducer, metadata.apply_method))
                self.assertTrue(callable(definition.apply_transition))

    def test_retry_transition_definition_carries_legality_target_and_behavior(self):
        from opencode_session.worker_lifecycle_reducer import _WORKER_TRANSITION_DEFINITIONS

        worker = normalize_worker(
            {
                "status": "failed",
                "failure_category": "provider",
                "retryable_failures": ["provider"],
                "retry_count": 0,
                "retry_limit": 1,
            },
            "review",
        )
        transition = schedule_worker_retry(worker, "provider", "try again")
        definition = _WORKER_TRANSITION_DEFINITIONS[transition.name]
        target_lifecycle = definition.target_lifecycle_state(transition)

        self.assertIn(worker_lifecycle_state(worker), definition.source_states)
        self.assertEqual(target_lifecycle, "active_retry")
        self.assertIn(target_lifecycle, definition.target_states)

        apply_worker_transition_to_worker(worker, transition)

        self.assertEqual(worker_lifecycle_state(worker), target_lifecycle)

    def test_snapshot_replay_allows_legal_active_to_done_transition(self):
        worker = normalize_worker({"status": "active", "prompt_ids": ["msg_initial"]}, "review")
        snapshot = normalize_worker_snapshot(
            {
                "id": "review",
                "lifecycle_state": "done_collect",
                "prompt_ids": ["msg_done"],
                "result": {
                    "status": "done",
                    "message_ids": {"assistant": "msg_assistant"},
                },
                "output_refs": ["assistant:msg_assistant"],
            },
            "review",
        )

        apply_worker_transition_to_worker(worker, WorkerTransition.snapshot_applied(snapshot))

        assert_worker_outcome(self, worker, status="done", action="collect", lifecycle="done_collect")
        self.assertEqual(worker["prompt_ids"], ["msg_initial", "msg_done"])
        self.assertEqual(worker["output_refs"], ["assistant:msg_assistant"])

    def test_snapshot_replay_noops_illegal_lifecycle_rewind(self):
        from opencode_session.worker_lifecycle_reducer import apply_worker_transition_to_record

        original = normalize_worker(
            {
                "status": "done",
                "prompt_ids": ["msg_done"],
                "result": {
                    "status": "done",
                    "message_ids": {"assistant": "msg_assistant"},
                },
                "output_refs": ["assistant:msg_assistant"],
            },
            "review",
        )
        stale_snapshot = normalize_worker_snapshot(
            {
                "id": "review",
                "lifecycle_state": "active_wait",
                "prompt_ids": ["msg_stale"],
            },
            "review",
        )
        worker = deepcopy(original)
        transition = WorkerTransition.snapshot_applied(stale_snapshot)

        result = apply_worker_transition_to_record(WorkerRecord.from_worker(worker, "review"), transition)

        self.assertFalse(result.applied)
        self.assertTrue(result.skipped)
        self.assertTrue(result.stale_snapshot_recovery)
        self.assertIn("stale snapshot ignored", result.reason)

        apply_worker_transition_to_worker(worker, transition)

        self.assertEqual(worker, original)

    def test_schedule_worker_retry_clears_stale_current_status_metadata(self):
        worker = normalize_worker(
            {
                "status": "failed",
                "blockers": ["dependency:build"],
                "error": "previous failure",
                "failure_category": "api",
                "failure_reason": "previous failure",
                "failure_retryable": True,
                "retryable_failures": ["api"],
                "retry_count": 0,
                "retry_limit": 1,
            },
            "review",
        )

        scheduled = schedule_worker_retry(worker, "api", "previous failure")
        apply_worker_transition_to_worker(worker, scheduled)

        self.assertTrue(scheduled)
        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["blockers"], [])
        self.assertNotIn("error", worker)
        self.assertIsNone(worker["failure_category"])
        self.assertIsNone(worker["failure_reason"])
        self.assertNotIn("failure_retryable", worker)
        self.assertEqual(worker["last_failure_category"], "api")
        self.assertEqual(worker["last_failure_reason"], "previous failure")
        self.assertEqual(worker["next_eligible_action"], "retry")
        self.assertEqual(worker["lifecycle_state"], "active_retry")

    def test_worker_transition_name_rejects_raw_strings(self):
        worker = normalize_worker({}, "review")
        transition = mark_worker_active(worker)

        self.assertIs(transition.name, WorkerTransitionName.ACTIVE)
        with self.assertRaisesRegex(ValueError, "unknown worker transition: active"):
            WorkerTransition("review", "active")
        with self.assertRaisesRegex(ValueError, "unknown worker transition: missing"):
            WorkerTransition("review", "missing")

    def test_worker_transition_reducer_rejects_raw_string_dispatch(self):
        worker = normalize_worker({}, "review")
        transition = SimpleNamespace(
            worker_id="review",
            name="active",
            payload=SimpleNamespace(
                timeout_started_at=UNSET_TRANSITION_FIELD,
                clear_prompt_ids=False,
            ),
            attempt_finalization=None,
        )

        with self.assertRaisesRegex(ValueError, "unknown worker transition: active"):
            apply_worker_transition_to_worker(worker, transition)

    def test_mark_worker_active_clears_retry_marker_prompt_ids(self):
        worker = normalize_worker(
            {
                "status": "failed",
                "retryable_failures": ["provider"],
                "retry_count": 0,
                "retry_limit": 1,
                "prompt_ids": ["msg_failed"],
            },
            "review",
        )
        apply_worker_transition_to_worker(
            worker,
            schedule_worker_retry(worker, "provider", "provider failed", prompt_ids=("msg_failed",)),
        )

        apply_worker_transition_to_worker(worker, mark_worker_active(worker))

        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["lifecycle_state"], "active_wait")
        self.assertEqual(worker["prompt_ids"], [])

    def test_mark_worker_aborted_only_changes_status_when_abort_is_accepted(self):
        worker = normalize_worker({"status": "active", "next_eligible_action": "wait"}, "planner")

        apply_worker_transition_to_worker(worker, mark_worker_aborted(worker, {"accepted": False}))

        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["next_eligible_action"], "wait")

        apply_worker_transition_to_worker(worker, mark_worker_aborted(worker, {"accepted": True}))

        self.assertEqual(worker["status"], "aborted")
        self.assertEqual(worker["next_eligible_action"], "none")
        self.assertEqual(worker["abort"], {"accepted": True})

    def test_refresh_run_summary_and_exit_code_use_failed_precedence_for_mixed_terminal_workers(self):
        run = {
            "workers": {
                "build": {"id": "build", "prompt": "Build", "lifecycle_state": "timeout_terminal"},
                "review": {"id": "review", "prompt": "Review", "lifecycle_state": "aborted"},
                "test": {"id": "test", "prompt": "Test", "lifecycle_state": "failed_terminal"},
            }
        }

        refresh_run_summary(run)

        self.assertEqual(run["status"], "failed")
        self.assertEqual(exit_code_for_run(run), EX_UNAVAILABLE)


def _metadata_by_status(field_name, *, skip_none=False):
    values = {}
    for metadata in WORKER_LIFECYCLE_METADATA.values():
        value = getattr(metadata, field_name)
        if skip_none and value is None:
            continue
        if metadata.status in values and values[metadata.status] != value:
            raise AssertionError(f"conflicting metadata for {metadata.status}")
        values[metadata.status] = value
    return values


if __name__ == "__main__":
    unittest.main()

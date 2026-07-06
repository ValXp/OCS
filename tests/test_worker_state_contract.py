import unittest

from opencode_session.worker_state import (
    EX_UNAVAILABLE,
    apply_worker_transition,
    apply_worker_transition_to_worker,
    apply_worker_result,
    exit_code_for_run,
    mark_dependency_blocked,
    mark_worker_aborted,
    mark_worker_active,
    normalize_worker,
    refresh_run_summary,
    schedule_worker_retry,
)
from opencode_session.schema_common import Worker, WorkerSnapshotRecord
from opencode_session.worker_domain import WorkerRecord, WorkerSchedulingState, WorkerTransition
from opencode_session.worker_snapshot_codec import (
    require_internal_worker,
    deserialize_worker_record,
    serialize_worker_snapshot,
)

try:
    from tests.worker_state_scenarios import WorkerScenario, assert_worker_outcome
except ModuleNotFoundError:
    from worker_state_scenarios import WorkerScenario, assert_worker_outcome


class WorkerStateContractTest(unittest.TestCase):
    def test_worker_domain_facade_exposes_decomposed_worker_helpers(self):
        import opencode_session.worker_domain as worker_domain
        from opencode_session.worker_attempt_log import new_worker_attempt_record
        from opencode_session.worker_lifecycle import WorkerSchedulingState as DecomposedSchedulingState
        from opencode_session.worker_lifecycle_reducer import WorkerTransition as DecomposedTransition
        from opencode_session.worker_snapshot_codec import WorkerRecord as DecomposedRecord

        self.assertIs(worker_domain.WorkerRecord, DecomposedRecord)
        self.assertIs(worker_domain.WorkerSchedulingState, DecomposedSchedulingState)
        self.assertIs(worker_domain.WorkerTransition, DecomposedTransition)
        self.assertIs(worker_domain.new_worker_attempt_record, new_worker_attempt_record)

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

        self.assertEqual(worker["id"], "review")
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["dependencies"], [])
        self.assertEqual(worker["prompt_ids"], [])
        self.assertEqual(worker["timeout_policy"], "timeout")
        self.assertEqual(worker["lifecycle_state"], "failed_retry")
        self.assertEqual(worker["next_eligible_action"], "retry")

    def test_worker_scheduling_state_derives_canonical_execution_action(self):
        queued = {"id": "build", "prompt": "Build", "status": "queued"}
        waiting = {"id": "review", "prompt": "Review", "status": "active", "next_eligible_action": "wait"}
        retrying = {"id": "test", "prompt": "Test", "status": "active", "next_eligible_action": "retry"}
        stale_action = {
            "id": "docs",
            "prompt": "Docs",
            "lifecycle_state": "active_wait",
            "status": "active",
            "next_eligible_action": "retry",
        }

        self.assertTrue(WorkerSchedulingState.from_worker(queued).can_execute())
        self.assertFalse(WorkerSchedulingState.from_worker(waiting).can_execute())
        self.assertEqual(WorkerSchedulingState.from_worker(waiting).next_eligible_action, "wait")
        self.assertTrue(WorkerSchedulingState.from_worker(retrying).can_execute())
        self.assertFalse(WorkerSchedulingState.from_worker(stale_action).can_execute())
        self.assertEqual(WorkerSchedulingState.from_worker(stale_action).lifecycle_state, "active_wait")
        self.assertEqual(WorkerSchedulingState.from_worker(stale_action).next_eligible_action, "wait")

    def test_worker_record_serializes_public_state_from_lifecycle(self):
        worker = WorkerRecord.from_worker(
            {
                "id": "review",
                "lifecycle_state": "active_wait",
                "status": "failed",
                "next_eligible_action": "retry",
            },
            "review",
        ).to_worker()

        self.assertEqual(worker["lifecycle_state"], "active_wait")
        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["next_eligible_action"], "wait")

    def test_worker_model_requires_internal_lifecycle_status_and_action_fields(self):
        required_keys = Worker.__required_keys__

        self.assertIn("session_id", required_keys)
        self.assertIn("lifecycle_state", required_keys)
        self.assertIn("status", required_keys)
        self.assertIn("next_eligible_action", required_keys)
        self.assertEqual(WorkerSnapshotRecord.__required_keys__, frozenset())

    def test_deserialize_worker_snapshot_hydrates_required_internal_fields(self):
        worker = deserialize_worker_record(
            {
                "status": "active",
                "next_eligible_action": "retry",
                "dependencies": "build",
            },
            "review",
        )

        require_internal_worker(worker)
        self.assertEqual(worker["id"], "review")
        self.assertIsNone(worker["session_id"])
        self.assertEqual(worker["dependencies"], [])
        self.assertEqual(worker["lifecycle_state"], "active_retry")
        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["next_eligible_action"], "retry")

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

        self.assertEqual(snapshot["lifecycle_state"], "done_collect")
        self.assertEqual(snapshot["session_id"], "ses_review")
        self.assertNotIn("status", snapshot)
        self.assertNotIn("next_eligible_action", snapshot)

    def test_internal_worker_guard_reports_missing_required_fields(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            require_internal_worker({"id": "review"})

    def test_mark_worker_active_sets_waiting_action_and_timeout_start(self):
        worker = normalize_worker({"timeout_seconds": 30}, "builder")

        transition = mark_worker_active(worker, now=lambda: "2026-07-04T00:00:00Z")
        apply_worker_transition_to_worker(worker, transition)

        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["lifecycle_state"], "active_wait")
        self.assertEqual(worker["next_eligible_action"], "wait")
        self.assertEqual(worker["timeout_started_at"], "2026-07-04T00:00:00Z")
        self.assertNotIn("status", transition.set_fields)
        self.assertNotIn("next_eligible_action", transition.set_fields)

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

    def test_worker_transition_applies_lifecycle_patch_without_snapshot_whitelist(self):
        latest_workers = {
            "review": {
                "id": "review",
                "status": "active",
                "next_eligible_action": "wait",
                "failure_retryable": False,
                "prompt_ids": ["msg_previous"],
            }
        }

        merged = apply_worker_transition(
            latest_workers,
            WorkerTransition.failed(
                "review",
                "provider",
                "provider failed",
                retryable=True,
                retry_available=False,
                prompt_ids=("msg_failed",),
            ),
        )

        self.assertEqual(merged["status"], "failed")
        self.assertEqual(merged["error"], "provider failed")
        self.assertEqual(merged["prompt_ids"], ["msg_previous", "msg_failed"])
        self.assertNotIn("failure_retryable", merged)

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
                "status": "failed",
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
                "build": {"id": "build", "prompt": "Build", "status": "timeout"},
                "review": {"id": "review", "prompt": "Review", "status": "aborted"},
                "test": {"id": "test", "prompt": "Test", "status": "failed"},
            }
        }

        refresh_run_summary(run)

        self.assertEqual(run["status"], "failed")
        self.assertEqual(exit_code_for_run(run), EX_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace

from opencode_session.worker_state import (
    EX_UNAVAILABLE,
    UNSET_TRANSITION_FIELD,
    WorkerTransition,
    WorkerTransitionName,
    apply_worker_transition_to_worker,
    apply_worker_result,
    deserialize_worker_record,
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
    worker_lifecycle_state,
)

try:
    from tests.worker_state_scenarios import WorkerScenario, assert_worker_outcome
except ModuleNotFoundError:
    from worker_state_scenarios import WorkerScenario, assert_worker_outcome


class WorkerStateContractTest(unittest.TestCase):
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

        assert_worker_outcome(self, worker, status="active", action="wait", lifecycle="active_wait")

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

    def test_normalize_worker_snapshot_converts_public_status_at_boundary(self):
        snapshot = normalize_worker_snapshot(
            {
                "id": "review",
                "lifecycle_state": "active_wait",
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


if __name__ == "__main__":
    unittest.main()

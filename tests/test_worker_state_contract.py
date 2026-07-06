import unittest

from opencode_session.worker_state import (
    EX_UNAVAILABLE,
    apply_worker_result,
    exit_code_for_run,
    mark_dependency_blocked,
    mark_worker_aborted,
    mark_worker_active,
    normalize_worker,
    refresh_run_summary,
    schedule_worker_retry,
)
from opencode_session.worker_lifecycle import WorkerTransition
from opencode_session.worker_normalization import WorkerRecord
from opencode_session.worker_scheduling import WorkerSchedulingState


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

    def test_mark_worker_active_sets_waiting_action_and_timeout_start(self):
        worker = normalize_worker({"timeout_seconds": 30}, "builder")

        transition = mark_worker_active(worker, now=lambda: "2026-07-04T00:00:00Z")

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

        mark_worker_active(worker)

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

        merged = WorkerTransition.failed(
            "review",
            "provider",
            "provider failed",
            retryable=True,
            retry_available=False,
            prompt_ids=("msg_failed",),
        ).apply_to(latest_workers)

        self.assertEqual(merged["status"], "failed")
        self.assertEqual(merged["error"], "provider failed")
        self.assertEqual(merged["prompt_ids"], ["msg_previous", "msg_failed"])
        self.assertNotIn("failure_retryable", merged)

    def test_mark_dependency_blocked_records_blockers_and_resolution_action(self):
        worker = normalize_worker({}, "review")

        mark_dependency_blocked(worker, ["dependency:build"])

        self.assertEqual(worker["status"], "blocked")
        self.assertEqual(worker["blockers"], ["dependency:build"])
        self.assertEqual(worker["next_eligible_action"], "resolve_blocker")

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

        apply_worker_result(
            worker,
            {
                "status": "done",
                "message_ids": {"assistant": "msg_assistant"},
            },
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
        schedule_worker_retry(worker, "provider", "provider failed", prompt_ids=("msg_failed",))

        mark_worker_active(worker)

        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["lifecycle_state"], "active_wait")
        self.assertEqual(worker["prompt_ids"], [])

    def test_mark_worker_aborted_only_changes_status_when_abort_is_accepted(self):
        worker = normalize_worker({"status": "active", "next_eligible_action": "wait"}, "planner")

        mark_worker_aborted(worker, {"accepted": False})

        self.assertEqual(worker["status"], "active")
        self.assertEqual(worker["next_eligible_action"], "wait")

        mark_worker_aborted(worker, {"accepted": True})

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

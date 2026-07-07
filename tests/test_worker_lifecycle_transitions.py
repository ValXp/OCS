from copy import deepcopy
import unittest

from opencode_session import worker_state as worker_state_module
from opencode_session.worker_snapshot_transition import worker_snapshot_transition
from opencode_session.worker_state import (
    WORKER_TRANSITION_METADATA,
    WorkerRecord,
    WorkerTransition,
    WorkerTransitionError,
    WorkerTransitionName,
    reduce_worker_transition,
)


NOW = "2026-07-04T00:00:00Z"


class WorkerLifecycleTransitionTest(unittest.TestCase):
    def test_worker_transition_definitions_are_single_sourced(self):
        specs = worker_state_module._WORKER_TRANSITION_SPECS

        self.assertCountEqual((spec.name for spec in specs), tuple(WorkerTransitionName))
        self.assertEqual(set(WORKER_TRANSITION_METADATA), set(WorkerTransitionName))
        for spec in specs:
            with self.subTest(name=spec.name.value):
                metadata = WORKER_TRANSITION_METADATA[spec.name]

                self.assertEqual(metadata.name, spec.name)
                self.assertEqual(metadata.source_states, spec.source_states)
                self.assertEqual(metadata.target_states, spec.target_states)
                self.assertEqual(metadata.public_lifecycle_transition, spec.public_lifecycle_transition)

    def test_public_worker_transitions_produce_observable_outcomes(self):
        cases = (
            (
                "start queued worker",
                {"timeout_seconds": 30},
                lambda worker: WorkerTransition.active(worker.worker_id, timeout_started_at=NOW),
                {"status": "active", "next_eligible_action": "wait"},
                {"timeout_started_at": NOW},
            ),
            (
                "record retryable provider failure",
                {
                    "lifecycle_state": "active_wait",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                lambda worker: WorkerTransition.failed(
                    worker.worker_id,
                    "provider",
                    "provider failed",
                    retry_available=True,
                ),
                {"status": "failed", "next_eligible_action": "retry"},
                {"failure_category": "provider", "failure_reason": "provider failed"},
            ),
            (
                "record terminal provider failure",
                {"lifecycle_state": "active_wait"},
                lambda worker: WorkerTransition.failed(
                    worker.worker_id,
                    "provider",
                    "provider failed",
                    retryable=False,
                    retry_available=False,
                ),
                {"status": "failed", "next_eligible_action": "none"},
                {"failure_retryable": False, "failure_reason": "provider failed"},
            ),
            (
                "block worker on dependency",
                {},
                lambda worker: WorkerTransition.dependency_blocked(worker.worker_id, ["dependency:build"]),
                {"status": "blocked", "next_eligible_action": "resolve_blocker"},
                {"blockers": ["dependency:build"]},
            ),
            (
                "schedule retry from failed worker",
                {
                    "lifecycle_state": "failed_retry",
                    "blockers": ["dependency:build"],
                    "error": "previous failure",
                    "failure_category": "provider",
                    "failure_reason": "previous failure",
                    "failure_retryable": True,
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                lambda worker: WorkerTransition.retry_scheduled(
                    worker.worker_id,
                    "provider",
                    "previous failure",
                    retry_count=worker.retry_count + 1,
                    prompt_ids=("msg_failed",),
                ),
                {"status": "active", "next_eligible_action": "retry"},
                {
                    "blockers": [],
                    "retry_count": 1,
                    "prompt_ids": ["msg_failed"],
                    "last_failure_category": "provider",
                    "last_failure_reason": "previous failure",
                },
            ),
            (
                "apply done result",
                {
                    "lifecycle_state": "active_wait",
                    "blockers": ["dependency:build"],
                    "error": "previous failure",
                    "failure_category": "api",
                    "failure_reason": "previous failure",
                    "failure_retryable": False,
                },
                lambda worker: WorkerTransition.result_applied(
                    worker.worker_id,
                    {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}},
                    prompt_ids=("msg_user",),
                ),
                {"status": "done", "next_eligible_action": "collect"},
                {"blockers": [], "prompt_ids": ["msg_user"], "output_refs": ["assistant:msg_assistant"]},
            ),
            (
                "record retryable timeout failure",
                {
                    "lifecycle_state": "active_wait",
                    "retryable_failures": ["timeout"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                lambda worker: WorkerTransition.timed_out(
                    worker.worker_id,
                    "worker timed out",
                    status="failed",
                    timed_out_at=NOW,
                    retry_available=True,
                    manual_retry_required=True,
                ),
                {"status": "failed", "next_eligible_action": "retry"},
                {
                    "failure_category": "timeout",
                    "failure_reason": "worker timed out",
                    "manual_retry_required": True,
                    "timed_out_at": NOW,
                },
            ),
        )

        for name, fields, transition_factory, expected_output, expected_snapshot in cases:
            with self.subTest(name=name):
                worker = worker_record(**fields)

                worker.apply_transition(transition_factory(worker))

                output = worker.to_output_dict()
                snapshot = worker.to_snapshot()
                for field_name, expected_value in expected_output.items():
                    self.assertEqual(output[field_name], expected_value)
                for field_name, expected_value in expected_snapshot.items():
                    self.assertEqual(snapshot[field_name], expected_value)
                self.assertNotIn("status", snapshot)
                self.assertNotIn("next_eligible_action", snapshot)

    def test_done_result_clears_stale_current_status_metadata(self):
        worker = worker_record(
            lifecycle_state="active_wait",
            blockers=["dependency:build"],
            error="previous failure",
            failure_category="api",
            failure_reason="previous failure",
            failure_retryable=False,
            last_failure_category="api",
            last_failure_reason="previous failure",
        )

        worker.apply_transition(
            WorkerTransition.result_applied(
                worker.worker_id,
                {"status": "done", "message_ids": {"assistant": "msg_assistant"}},
            )
        )

        output = worker.to_output_dict()
        snapshot = worker.to_snapshot()
        self.assertEqual(output["status"], "done")
        self.assertEqual(output["next_eligible_action"], "collect")
        self.assertEqual(snapshot["blockers"], [])
        self.assertEqual(snapshot["failure_category"], None)
        self.assertEqual(snapshot["failure_reason"], None)
        self.assertEqual(snapshot["last_failure_category"], "api")
        self.assertEqual(snapshot["last_failure_reason"], "previous failure")
        self.assertNotIn("error", snapshot)
        self.assertNotIn("failure_retryable", snapshot)

    def test_reduce_worker_transition_returns_updated_worker_without_mutating_source(self):
        worker = worker_record(
            lifecycle_state="active_wait",
            blockers=["dependency:build"],
            error="previous failure",
            failure_category="api",
            failure_reason="previous failure",
        )
        original = deepcopy(worker)

        result = reduce_worker_transition(
            worker,
            WorkerTransition.result_applied(
                worker.worker_id,
                {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}},
                prompt_ids=("msg_user",),
            ),
        )

        self.assertTrue(result.applied)
        self.assertEqual(worker, original)
        self.assertIsNot(result.worker, worker)
        self.assertEqual(result.worker.to_output_dict()["status"], "done")
        self.assertEqual(result.worker.prompt_ids, ["msg_user"])
        self.assertEqual(result.worker.output_refs, ["assistant:msg_assistant"])
        self.assertNotIn("error", result.worker.to_snapshot())

    def test_reduce_worker_transition_skips_illegal_transition_without_mutating_source(self):
        worker = worker_record(
            lifecycle_state="done_collect",
            result={"status": "done", "message_ids": {"assistant": "msg_done"}},
            output_refs=["assistant:msg_done"],
        )
        original = deepcopy(worker)

        result = reduce_worker_transition(worker, WorkerTransition.active(worker.worker_id))

        self.assertTrue(result.skipped)
        self.assertIn("illegal worker transition 'active'", result.reason)
        self.assertEqual(worker, original)
        self.assertEqual(result.worker, original)

    def test_retry_start_clears_retry_marker_prompt_ids(self):
        worker = worker_record(
            lifecycle_state="failed_retry",
            retryable_failures=["provider"],
            retry_count=0,
            retry_limit=1,
            prompt_ids=["msg_failed"],
        )
        worker.apply_transition(
            WorkerTransition.retry_scheduled(
                worker.worker_id,
                "provider",
                "provider failed",
                retry_count=1,
                prompt_ids=("msg_failed",),
            )
        )

        worker.apply_transition(WorkerTransition.active(worker.worker_id, clear_prompt_ids=True))

        output = worker.to_output_dict()
        self.assertEqual(output["status"], "active")
        self.assertEqual(output["next_eligible_action"], "wait")
        self.assertEqual(worker.prompt_ids, [])

    def test_accepted_abort_keeps_public_abort_when_late_result_arrives(self):
        worker = worker_record(
            lifecycle_state="active_wait",
            session_id="ses_build",
            prompt_ids=["msg_initial"],
        )
        worker.apply_transition(WorkerTransition.aborted(worker.worker_id, {"session_id": "ses_build", "accepted": True}))

        worker.apply_transition(
            WorkerTransition.result_applied(
                worker.worker_id,
                {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}},
                prompt_ids=("msg_user",),
            )
        )

        output = worker.to_output_dict()
        snapshot = worker.to_snapshot()
        self.assertEqual(output["status"], "aborted")
        self.assertEqual(output["next_eligible_action"], "none")
        self.assertEqual(snapshot["abort"], {"session_id": "ses_build", "accepted": True})
        self.assertEqual(snapshot["prompt_ids"], ["msg_initial", "msg_user"])
        self.assertEqual(snapshot["output_refs"], [])
        self.assertNotIn("result", snapshot)

    def test_abort_only_changes_public_status_when_abort_is_accepted(self):
        worker = worker_record(lifecycle_state="active_wait")

        worker.apply_transition(WorkerTransition.aborted(worker.worker_id, {"accepted": False}))

        self.assertEqual(worker.to_output_dict()["status"], "active")
        self.assertEqual(worker.to_output_dict()["next_eligible_action"], "wait")

        worker.apply_transition(WorkerTransition.aborted(worker.worker_id, {"accepted": True}))

        self.assertEqual(worker.to_output_dict()["status"], "aborted")
        self.assertEqual(worker.to_output_dict()["next_eligible_action"], "none")
        self.assertEqual(worker.abort, {"accepted": True})

    def test_illegal_transition_preserves_worker(self):
        worker = worker_record(
            lifecycle_state="done_collect",
            result={"status": "done", "message_ids": {"assistant": "msg_done"}},
            output_refs=["assistant:msg_done"],
        )
        original = deepcopy(worker)

        with self.assertRaisesRegex(WorkerTransitionError, "illegal worker transition"):
            worker.apply_transition(WorkerTransition.active(worker.worker_id))

        self.assertEqual(worker, original)

    def test_snapshot_replay_applies_forward_snapshot_and_ignores_stale_rewind(self):
        worker = worker_record(lifecycle_state="active_wait", prompt_ids=["msg_initial"])
        done_snapshot = {
            "id": worker.worker_id,
            "lifecycle_state": "done_collect",
            "prompt_ids": ["msg_done"],
            "result": {"status": "done", "message_ids": {"assistant": "msg_assistant"}},
            "output_refs": ["assistant:msg_assistant"],
        }

        worker.apply_transition(worker_snapshot_transition(done_snapshot))
        after_done = deepcopy(worker)
        worker.apply_transition(
            worker_snapshot_transition(
                {"id": worker.worker_id, "lifecycle_state": "active_wait", "prompt_ids": ["msg_stale"]}
            )
        )

        output = worker.to_output_dict()
        self.assertEqual(output["status"], "done")
        self.assertEqual(output["next_eligible_action"], "collect")
        self.assertEqual(worker.prompt_ids, ["msg_initial", "msg_done"])
        self.assertEqual(worker.output_refs, ["assistant:msg_assistant"])
        self.assertEqual(worker, after_done)

    def test_transition_rejects_raw_string_name(self):
        with self.assertRaisesRegex(ValueError, "unknown worker transition: active"):
            WorkerTransition("review", "active")


def worker_record(worker_id="review", **fields):
    fields = {"id": worker_id, "prompt": "Review", **fields}
    return WorkerRecord.from_worker(fields, worker_id).to_worker()


if __name__ == "__main__":
    unittest.main()

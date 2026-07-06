from copy import deepcopy
from types import SimpleNamespace
import unittest

from opencode_session.worker_state import (
    UNSET_TRANSITION_FIELD,
    WORKER_TRANSITION_METADATA,
    WorkerRecord,
    WorkerTransition,
    WorkerTransitionError,
    WorkerTransitionName,
    apply_worker_result,
    apply_worker_transition_to_worker,
    mark_dependency_blocked,
    mark_worker_aborted,
    mark_worker_active,
    mark_worker_failed,
    normalize_worker,
    normalize_worker_snapshot,
    schedule_worker_retry,
    worker_lifecycle_source_states,
    worker_lifecycle_state,
    worker_lifecycle_target_states,
    worker_field,
    worker_has_field,
    worker_transition_is_legal,
    worker_transition_target_lifecycle_state,
)

try:
    from tests.worker_state_scenarios import (
        WorkerScenario,
        WorkerTransitionCase,
        assert_worker_outcome,
        assert_worker_transition_case,
    )
except ModuleNotFoundError:
    from worker_state_scenarios import (
        WorkerScenario,
        WorkerTransitionCase,
        assert_worker_outcome,
        assert_worker_transition_case,
    )


TRANSITION_CASES = (
    WorkerTransitionCase(
        "active worker records timeout start",
        {"timeout_seconds": 30},
        lambda worker: mark_worker_active(worker, now=lambda: "2026-07-04T00:00:00Z"),
        {"status": "active", "action": "wait", "lifecycle": "active_wait"},
        expected_fields={"timeout_started_at": "2026-07-04T00:00:00Z"},
    ),
    WorkerTransitionCase(
        "active worker clears stale current-status metadata",
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
        mark_worker_active,
        {"status": "active", "action": "wait", "lifecycle": "active_wait"},
        expected_fields={
            "blockers": [],
            "failure_category": None,
            "failure_reason": None,
            "last_failure_category": "api",
            "last_failure_reason": "previous failure",
        },
        absent_fields=("error", "failure_retryable"),
    ),
    WorkerTransitionCase(
        "terminal failure records prompt ids",
        {
            "status": "active",
            "next_eligible_action": "wait",
            "failure_retryable": False,
            "prompt_ids": ["msg_previous"],
        },
        lambda worker: mark_worker_failed(worker, "provider", "provider failed", prompt_ids=("msg_failed",)),
        {"status": "failed", "action": "none", "lifecycle": "failed_terminal"},
        expected_fields={
            "error": "provider failed",
            "failure_category": "provider",
            "failure_reason": "provider failed",
            "prompt_ids": ["msg_previous", "msg_failed"],
        },
        absent_fields=("failure_retryable",),
    ),
    WorkerTransitionCase(
        "retryable failure remains retry eligible",
        {
            "status": "active",
            "next_eligible_action": "wait",
            "retryable_failures": ["provider"],
            "retry_count": 0,
            "retry_limit": 1,
        },
        lambda worker: mark_worker_failed(worker, "provider", "provider failed"),
        {"status": "failed", "action": "retry", "lifecycle": "failed_retry"},
        expected_fields={"failure_category": "provider", "failure_reason": "provider failed"},
        absent_fields=("failure_retryable",),
    ),
    WorkerTransitionCase(
        "dependency block records blockers and resolution action",
        {},
        lambda worker: mark_dependency_blocked(worker, ["dependency:build"]),
        {
            "status": "blocked",
            "action": "resolve_blocker",
            "lifecycle": "blocked_dependency",
            "blockers": ["dependency:build"],
        },
    ),
    WorkerTransitionCase(
        "done result clears stale current-status metadata",
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
        lambda worker: apply_worker_result(
            worker,
            {
                "status": "done",
                "message_ids": {"assistant": "msg_assistant"},
            },
        ),
        {
            "status": "done",
            "action": "collect",
            "lifecycle": "done_collect",
            "output_refs": ["assistant:msg_assistant"],
        },
        expected_fields={
            "blockers": [],
            "failure_category": None,
            "failure_reason": None,
            "last_failure_category": "api",
            "last_failure_reason": "previous failure",
        },
        absent_fields=("error", "failure_retryable"),
    ),
    WorkerTransitionCase(
        "scheduled retry clears stale current-status metadata",
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
        lambda worker: schedule_worker_retry(worker, "api", "previous failure"),
        {"status": "active", "action": "retry", "lifecycle": "active_retry"},
        expected_fields={
            "blockers": [],
            "failure_category": None,
            "failure_reason": None,
            "last_failure_category": "api",
            "last_failure_reason": "previous failure",
            "retry_count": 1,
        },
        absent_fields=("error", "failure_retryable"),
    ),
)


class WorkerLifecycleTransitionTest(unittest.TestCase):
    def test_transition_cases_preserve_public_outcomes_and_side_effects(self):
        for case in TRANSITION_CASES:
            with self.subTest(case=case.name):
                assert_worker_transition_case(self, case)

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

        self.assertEqual(worker_field(worker, "status"), "aborted")
        self.assertEqual(worker_field(worker, "next_eligible_action"), "none")
        self.assertEqual(worker_field(worker, "abort"), {"session_id": "ses_build", "accepted": True})
        self.assertEqual(worker_field(worker, "prompt_ids"), ["msg_initial", "msg_user"])
        self.assertFalse(worker_has_field(worker, "result"))
        self.assertEqual(worker_field(worker, "output_refs"), [])

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
        self.assertEqual(worker_field(retry_worker, "retry_count"), 1)

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
        self.assertTrue(worker_field(timeout_worker, "manual_retry_required"))

        result_worker = normalize_worker({"status": "active"}, "review")
        apply_worker_transition_to_worker(
            result_worker,
            WorkerTransition.result_applied(
                "review",
                {"status": "done", "message_ids": {"assistant": "msg_assistant"}},
            ),
        )

        assert_worker_outcome(self, result_worker, status="done", action="collect", lifecycle="done_collect")
        self.assertEqual(worker_field(result_worker, "output_refs"), ["assistant:msg_assistant"])

    def test_worker_transition_names_dispatch_through_public_transitions(self):
        cases = (
            (
                WorkerTransitionName.PROVISIONED,
                {"session_id": "ses_review", "agent": "build", "model": "openai/gpt-5.5"},
                WorkerTransition.provisioned,
                {"status": "queued", "action": "start", "lifecycle": "queued"},
                {"session_id": "ses_review", "agent": "build", "model": "openai/gpt-5.5"},
            ),
            (
                WorkerTransitionName.ACTIVE,
                {},
                mark_worker_active,
                {"status": "active", "action": "wait", "lifecycle": "active_wait"},
                {},
            ),
            (
                WorkerTransitionName.ATTEMPT_STARTED,
                {"status": "active"},
                lambda worker: WorkerTransition.attempt_started(
                    worker_field(worker, "id"),
                    {"id": "attempt-1", "status": "active", "session_id": "ses_review"},
                ),
                {"status": "active", "action": "wait", "lifecycle": "active_wait"},
                {"attempts": [{"id": "attempt-1", "status": "active", "session_id": "ses_review"}]},
            ),
            (
                WorkerTransitionName.FAILED,
                {"status": "active"},
                lambda worker: mark_worker_failed(worker, "provider", "provider failed", retryable=False),
                {"status": "failed", "action": "none", "lifecycle": "failed_terminal"},
                {"failure_category": "provider", "failure_reason": "provider failed"},
            ),
            (
                WorkerTransitionName.DEPENDENCY_BLOCKED,
                {},
                lambda worker: mark_dependency_blocked(worker, ["dependency:build"]),
                {"status": "blocked", "action": "resolve_blocker", "lifecycle": "blocked_dependency"},
                {"blockers": ["dependency:build"]},
            ),
            (
                WorkerTransitionName.ABORTED,
                {"status": "active"},
                lambda worker: mark_worker_aborted(worker, {"accepted": True}),
                {"status": "aborted", "action": "none", "lifecycle": "aborted"},
                {"abort": {"accepted": True}},
            ),
            (
                WorkerTransitionName.RETRY_SCHEDULED,
                {
                    "status": "failed",
                    "failure_category": "provider",
                    "retryable_failures": ["provider"],
                    "retry_count": 0,
                    "retry_limit": 1,
                },
                lambda worker: schedule_worker_retry(worker, "provider", "try again"),
                {"status": "active", "action": "retry", "lifecycle": "active_retry"},
                {"retry_count": 1},
            ),
            (
                WorkerTransitionName.TIMED_OUT,
                {"status": "active"},
                lambda worker: WorkerTransition.timed_out(
                    worker_field(worker, "id"),
                    "worker timed out",
                    status="timeout",
                    timed_out_at="2026-07-04T00:00:00Z",
                ),
                {"status": "timeout", "action": "none", "lifecycle": "timeout_terminal"},
                {"timed_out_at": "2026-07-04T00:00:00Z", "failure_category": "timeout"},
            ),
            (
                WorkerTransitionName.RESULT_APPLIED,
                {"status": "active"},
                lambda worker: WorkerTransition.result_applied(
                    worker_field(worker, "id"),
                    {"status": "done", "message_ids": {"assistant": "msg_assistant"}},
                ),
                {"status": "done", "action": "collect", "lifecycle": "done_collect"},
                {"output_refs": ["assistant:msg_assistant"]},
            ),
            (
                WorkerTransitionName.CLEANUP_UPDATED,
                {"cleanup": {"requested": True, "deleted": False}},
                WorkerTransition.cleanup_updated,
                {"status": "queued", "action": "start", "lifecycle": "queued"},
                {"cleanup": {"requested": True, "deleted": False}},
            ),
            (
                WorkerTransitionName.SNAPSHOT_APPLIED,
                {"status": "active", "prompt_ids": ["msg_initial"]},
                lambda worker: WorkerTransition.snapshot_applied(
                    normalize_worker_snapshot(
                        {
                            "id": worker_field(worker, "id"),
                            "lifecycle_state": "done_collect",
                            "prompt_ids": ["msg_done"],
                            "result": {"status": "done", "message_ids": {"assistant": "msg_assistant"}},
                            "output_refs": ["assistant:msg_assistant"],
                        },
                        worker_field(worker, "id"),
                    )
                ),
                {"status": "done", "action": "collect", "lifecycle": "done_collect"},
                {"prompt_ids": ["msg_initial", "msg_done"], "output_refs": ["assistant:msg_assistant"]},
            ),
        )

        self.assertEqual(set(WorkerTransitionName), set(WORKER_TRANSITION_METADATA))
        self.assertEqual(set(WorkerTransitionName), {case[0] for case in cases})
        for transition_name, worker_fields, transition_factory, expected_outcome, expected_fields in cases:
            with self.subTest(transition=transition_name):
                worker = normalize_worker(worker_fields, "review")
                transition = transition_factory(worker)
                target_lifecycle = worker_transition_target_lifecycle_state(transition)

                self.assertIs(transition.name, transition_name)
                self.assertIn(worker_lifecycle_state(worker), worker_lifecycle_source_states(transition_name))
                self.assertTrue(worker_transition_is_legal(worker, transition))
                if worker_lifecycle_target_states(transition_name):
                    self.assertIn(target_lifecycle, worker_lifecycle_target_states(transition_name))
                else:
                    self.assertIsNone(target_lifecycle)

                apply_worker_transition_to_worker(worker, transition)

                assert_worker_outcome(self, worker, **expected_outcome)
                for field_name, expected_value in expected_fields.items():
                    self.assertEqual(worker_field(worker, field_name), expected_value)

    def test_retry_transition_definition_carries_legality_target_and_behavior(self):
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
        target_lifecycle = worker_transition_target_lifecycle_state(transition)

        self.assertIn(worker_lifecycle_state(worker), worker_lifecycle_source_states(transition.name))
        self.assertTrue(worker_transition_is_legal(worker, transition))
        self.assertEqual(target_lifecycle, "active_retry")
        self.assertIn(target_lifecycle, worker_lifecycle_target_states(transition.name))

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
        self.assertEqual(worker_field(worker, "prompt_ids"), ["msg_initial", "msg_done"])
        self.assertEqual(worker_field(worker, "output_refs"), ["assistant:msg_assistant"])

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

        self.assertEqual(worker_field(worker, "status"), "active")
        self.assertEqual(worker_field(worker, "lifecycle_state"), "active_wait")
        self.assertEqual(worker_field(worker, "prompt_ids"), [])

    def test_mark_worker_aborted_only_changes_status_when_abort_is_accepted(self):
        worker = normalize_worker({"status": "active", "next_eligible_action": "wait"}, "planner")

        apply_worker_transition_to_worker(worker, mark_worker_aborted(worker, {"accepted": False}))

        self.assertEqual(worker_field(worker, "status"), "active")
        self.assertEqual(worker_field(worker, "next_eligible_action"), "wait")

        apply_worker_transition_to_worker(worker, mark_worker_aborted(worker, {"accepted": True}))

        self.assertEqual(worker_field(worker, "status"), "aborted")
        self.assertEqual(worker_field(worker, "next_eligible_action"), "none")
        self.assertEqual(worker_field(worker, "abort"), {"accepted": True})

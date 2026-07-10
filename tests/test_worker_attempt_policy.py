import unittest

from opencode_session.api_transport import OpenCodeApiError, OpenCodeApiTimeoutError
from opencode_session.blocking_execution import BlockingExecutionTimeout, BlockingProviderFailure
from opencode_session.worker_attempt_policy import (
    COMPLETED,
    RETRY_SCHEDULED,
    TERMINAL_FAILURE,
    WorkerExecutionTimeout,
    apply_worker_attempt_transition,
    classify_worker_attempt_exception,
    classify_worker_attempt_result,
)
from opencode_session.worker_storage_adapter import hydrate_worker_record
from opencode_session.worker_state import WorkerTransitionName


def worker_record(**fields):
    worker = {
        "id": "worker",
        "retry_count": 0,
        "retry_limit": 0,
        "retryable_failures": [],
    }
    worker.update(fields)
    return hydrate_worker_record(worker, worker["id"])


class WorkerAttemptPolicyTest(unittest.TestCase):
    def test_classifies_worker_attempt_exceptions_without_executing_policy(self):
        worker = worker_record(timeout_seconds=0.05)

        timeout = classify_worker_attempt_exception(worker, WorkerExecutionTimeout())
        api = classify_worker_attempt_exception(worker, OpenCodeApiError("HTTP 503 POST /session/ses/run"))
        provider = classify_worker_attempt_exception(
            worker,
            BlockingProviderFailure("provider overloaded", prompt_id="msg_user_failed"),
        )
        api_timeout = classify_worker_attempt_exception(
            worker,
            OpenCodeApiTimeoutError("OpenCode server timed out at http://127.0.0.1"),
        )
        blocking_timeout = classify_worker_attempt_exception(
            worker,
            BlockingExecutionTimeout("blocking execution timed out", prompt_id="msg_timeout"),
        )

        self.assertEqual(timeout.kind, "failed")
        self.assertEqual(timeout.failure_category, "timeout")
        self.assertEqual(timeout.reason, "worker timed out after 0.05s")
        self.assertEqual(api.failure_category, "api")
        self.assertEqual(api.reason, "HTTP 503 POST /session/ses/run")
        self.assertEqual(provider.failure_category, "provider")
        self.assertEqual(provider.reason, "provider overloaded")
        self.assertEqual(provider.prompt_id, "msg_user_failed")
        self.assertEqual(api_timeout.failure_category, "timeout")
        self.assertEqual(api_timeout.reason, "OpenCode server timed out at http://127.0.0.1")
        self.assertEqual(blocking_timeout.failure_category, "timeout")
        self.assertEqual(blocking_timeout.prompt_id, "msg_timeout")
        self.assertIsNone(classify_worker_attempt_exception(worker, RuntimeError("boom")))

    def test_completed_result_policy_applies_result_transition_with_prompt_id(self):
        worker = worker_record()
        result = {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}
        attempt = classify_worker_attempt_result(result)

        transition = apply_worker_attempt_transition(worker, attempt, now=lambda: "2026-07-03T00:00:00Z")

        self.assertEqual(transition.kind, COMPLETED)
        self.assertIsNone(transition.error)
        self.assertEqual(transition.worker_transition.name, WorkerTransitionName.RESULT_APPLIED)
        self.assertEqual(transition.worker_transition.payload.prompt_ids, ("msg_user",))

    def test_retry_policy_schedules_retry_for_retryable_provider_failure(self):
        worker = worker_record(retry_limit=1, retryable_failures=["provider"])
        attempt = classify_worker_attempt_exception(
            worker,
            BlockingProviderFailure("transient provider outage", prompt_id="msg_user_failed"),
        )

        transition = apply_worker_attempt_transition(worker, attempt, now=lambda: "2026-07-03T00:00:00Z")

        self.assertEqual(transition.kind, RETRY_SCHEDULED)
        self.assertIsNone(transition.error)
        self.assertEqual(transition.failure_category, "provider")
        self.assertEqual(transition.worker_transition.name, WorkerTransitionName.RETRY_SCHEDULED)
        self.assertEqual(transition.worker_transition.payload.retry_count, 1)
        self.assertEqual(transition.worker_transition.payload.prompt_ids, ("msg_user_failed",))

    def test_retry_policy_marks_terminal_api_failure_when_retry_unavailable(self):
        worker = worker_record(retry_limit=1, retryable_failures=["provider"])
        attempt = classify_worker_attempt_exception(worker, OpenCodeApiError("HTTP 503 POST /session/ses/run"))

        transition = apply_worker_attempt_transition(worker, attempt, now=lambda: "2026-07-03T00:00:00Z")

        self.assertEqual(transition.kind, TERMINAL_FAILURE)
        self.assertEqual(transition.error, "api failure: HTTP 503 POST /session/ses/run")
        self.assertEqual(transition.failure_category, "api")
        self.assertEqual(transition.worker_transition.name, WorkerTransitionName.FAILED)
        self.assertFalse(transition.worker_transition.payload.retry_available)

    def test_timeout_retry_policy_preserves_manual_retry_semantics(self):
        worker = worker_record(timeout_seconds=0.05, retry_limit=1, retryable_failures=["timeout"])
        attempt = classify_worker_attempt_exception(worker, WorkerExecutionTimeout(prompt_id="msg_timeout"))

        transition = apply_worker_attempt_transition(worker, attempt, now=lambda: "2026-07-03T00:00:00Z")

        self.assertEqual(transition.kind, TERMINAL_FAILURE)
        self.assertIn("automatic timeout retry skipped", transition.error)
        self.assertEqual(transition.failure_category, "timeout")
        self.assertEqual(transition.worker_transition.name, WorkerTransitionName.TIMED_OUT)
        self.assertTrue(transition.worker_transition.payload.retry_available)
        self.assertTrue(transition.worker_transition.payload.manual_retry_required)


if __name__ == "__main__":
    unittest.main()

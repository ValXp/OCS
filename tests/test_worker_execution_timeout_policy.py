import tempfile
import unittest

from opencode_session.worker_execution import WorkerExecutionTimeout, execute_worker_attempts
from opencode_session.worker_state import ensure_worker, worker_field, worker_has_field, worker_output_field

try:
    from tests.worker_execution_helpers import CAPABILITIES, FakeClient
except ModuleNotFoundError:
    from worker_execution_helpers import CAPABILITIES, FakeClient


class WorkerExecutionTimeoutPolicyTest(unittest.TestCase):
    def test_execute_worker_attempts_skips_automatic_timeout_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker.update_canonical_fields(timeout_seconds=0.05, retry_limit=1, retryable_failures=["timeout"])
            client = FakeClient(["ses_initial", "ses_unused"])

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
                raise WorkerExecutionTimeout()

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

        self.assertEqual(outcome.kind, "terminal_failure")
        self.assertEqual(outcome.failure_category, "timeout")
        self.assertIn("automatic timeout retry skipped", outcome.error)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        self.assertEqual(worker_field(worker, "session_id"), "ses_initial")
        self.assertEqual(worker_output_field(worker, "status"), "timeout")
        self.assertEqual(worker_field(worker, "retry_count"), 0)
        self.assertTrue(worker_field(worker, "manual_retry_required"))
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "retry")
        self.assertEqual(worker_field(worker, "failure_reason"), "worker timed out after 0.05s")
        self.assertFalse(worker_has_field(worker, "timeout_retry_sessions"))
        self.assertFalse(worker_has_field(worker, "result"))

    def test_execute_worker_attempts_does_not_start_retry_session_after_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker.update_canonical_fields(timeout_seconds=0.05, retry_limit=1, retryable_failures=["timeout"])
            client = FakeClient(["ses_initial", "ses_retry"])

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt, capabilities["legacy_fallback_available"]))
                self.assertLessEqual(deadline.require_time(), 0.05)
                raise WorkerExecutionTimeout()

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
                agent="build",
                model="openai/gpt-5.5",
            )

        self.assertEqual(outcome.failure_category, "timeout")
        self.assertIn("automatic timeout retry skipped", outcome.error)
        self.assertEqual(outcome.created_session_ids, ["ses_initial"])
        self.assertEqual(
            client.requests,
            [
                ("create", directory, "build", "openai/gpt-5.5"),
                ("execute", "ses_initial", "Finish the worker task", True),
            ],
        )
        self.assertEqual(worker_output_field(worker, "status"), "timeout")
        self.assertEqual(worker_field(worker, "session_id"), "ses_initial")
        self.assertEqual(worker_field(worker, "retry_count"), 0)
        self.assertEqual(worker_field(worker, "last_failure_category"), "timeout")
        self.assertEqual(worker_field(worker, "last_failure_reason"), "worker timed out after 0.05s")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "retry")
        self.assertTrue(worker_field(worker, "manual_retry_required"))
        self.assertFalse(worker_has_field(worker, "result"))
        self.assertFalse(worker_has_field(worker, "timeout_retry_sessions"))

    def test_execute_worker_attempts_does_not_schedule_timeout_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker.update_canonical_fields(timeout_seconds=0.05, retry_limit=1, retryable_failures=["timeout"])
            client = FakeClient(["ses_initial", "ses_retry"])

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
                raise WorkerExecutionTimeout()

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

        self.assertEqual(outcome.kind, "terminal_failure")
        self.assertIn("automatic timeout retry skipped", outcome.error)
        self.assertEqual(outcome.failure_category, "timeout")
        self.assertEqual(outcome.created_session_ids, ["ses_initial"])
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        self.assertEqual(worker_output_field(worker, "status"), "timeout")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "retry")
        self.assertEqual(worker_field(worker, "session_id"), "ses_initial")


if __name__ == "__main__":
    unittest.main()

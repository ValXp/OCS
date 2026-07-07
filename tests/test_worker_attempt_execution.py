from copy import deepcopy
import tempfile
import unittest

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.run_record import run_record_for_output
from opencode_session.worker_attempt_execution import WorkerPromptExecution
from opencode_session.worker_execution import WorkerExecutionExecutor, execute_worker_attempts
from opencode_session.worker_session_provisioning import WORKER_SESSION_JOURNAL_FIELD, WorkerSessionCreationJournal
from opencode_session.worker_storage_adapter import hydrate_worker_record
from opencode_session.worker_state import (
    apply_worker_transition,
    ensure_worker,
    worker_field,
    worker_has_field,
    worker_output_field,
)

try:
    from tests.worker_execution_helpers import CAPABILITIES, FakeClient, WorkerExecutionAssertionsMixin
except ModuleNotFoundError:
    from worker_execution_helpers import CAPABILITIES, FakeClient, WorkerExecutionAssertionsMixin


class WorkerAttemptExecutionTest(WorkerExecutionAssertionsMixin, unittest.TestCase):
    def test_worker_execution_records_completed_run_outcome(self):
        run = {
            "name": "demo",
            "directory": "/workspace",
            "workers": {"worker": hydrate_worker_record({"id": "worker"}, "worker")},
        }
        worker = run["workers"]["worker"]
        client = FakeClient(["ses_new"])

        def persist_run_mutation(run, mutator):
            mutator(run)
            return run

        def persist_worker_transition(run, worker, transition):
            updated = apply_worker_transition(run.setdefault("workers", {}), transition)
            return run, updated

        def execute_prompt(client, session_id, prompt, capabilities):
            client.requests.append(("execute", session_id, prompt))
            return {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

        journal = WorkerSessionCreationJournal(
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
            id_factory=lambda: "worker-session-intent-1",
        )
        executor = WorkerExecutionExecutor(
            apply_transition=persist_worker_transition,
            executor=execute_prompt,
            now=lambda: "2026-07-03T00:00:00Z",
            session_journal=journal,
        )

        outcome = executor.execute(
            client,
            run,
            worker,
            "Finish the worker task",
            CAPABILITIES,
            agent="build",
            model="openai/gpt-5.5",
            cleanup_requested=True,
        )

        self.assertEqual(outcome.kind, "completed")
        self.assertEqual(outcome.created_session_ids, ["ses_new"])
        self.assertIs(outcome.run, run)
        self.assertIsNone(outcome.error)
        self.assertIsNone(outcome.failure_category)
        self.assertNotIn(WORKER_SESSION_JOURNAL_FIELD, run)
        self.assertCountEqual(
            client.requests,
            [
                ("create", "/workspace", "build", "openai/gpt-5.5"),
                ("execute", "ses_new", "Finish the worker task"),
            ],
        )
        worker = run["workers"]["worker"]
        worker_output = run_record_for_output(run)["workers"]["worker"]
        self.assertEqual(worker_output["status"], "done")
        self.assertEqual(worker_output["next_eligible_action"], "collect")
        self.assertEqual(worker_field(worker, "session_id"), "ses_new")
        self.assertEqual(worker_field(worker, "agent"), "build")
        self.assertEqual(worker_field(worker, "model"), "openai/gpt-5.5")
        self.assertEqual(
            worker_field(worker, "cleanup"),
            {"requested": True, "deleted": False, "sessions": ["ses_new"]},
        )
        attempt = self.assert_single_worker_attempt(worker, status="completed", session_id="ses_new")
        self.assertEqual(attempt.get("id"), "attempt-1")
        self.assertEqual(attempt.get("created_session_ids"), ["ses_new"])
        self.assertEqual(attempt.get("started_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("finished_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("result_status"), "done")
        self.assertEqual(attempt.get("user_message_id"), "msg_user")
        self.assertEqual(attempt.get("assistant_message_id"), "msg_assistant")

    def test_execute_worker_attempts_rejects_create_response_without_session_id_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            client = FakeClient([None])

            def execute_prompt(client, session_id, prompt, capabilities):
                self.fail(f"worker executed with malformed session id {session_id!r}")

            with self.assertRaisesRegex(
                OpenCodeApiError,
                "session creation returned malformed response: missing session id",
            ):
                execute_worker_attempts(
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

        self.assertEqual(client.requests, [("create", directory, "build", "openai/gpt-5.5")])
        self.assertIsNone(worker_field(worker, "session_id"))
        self.assertIsNone(worker_field(worker, "agent"))
        self.assertIsNone(worker_field(worker, "model"))
        self.assertFalse(worker_has_field(worker, "result"))

    def test_execute_worker_attempts_applies_active_attempt_before_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            client = FakeClient(["ses_initial"])
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                self.assertEqual(worker_output_field(worker, "status"), "active")
                attempt = self.assert_single_worker_attempt(worker, status="active", session_id="ses_initial")
                self.assertEqual(attempt.get("id"), "attempt-1")
                self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
                self.assertEqual(attempt.get("started_at"), "2026-07-03T00:00:00Z")
                self.assertIsNone(attempt.get("finished_at"))
                self.assertNotIn("result_status", attempt)
                return {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

        self.assertEqual(executions, [("ses_initial", "Finish the worker task")])
        self.assertEqual(client.requests, [("create", directory, None, None)])
        self.assertEqual(outcome.kind, "completed")
        self.assertEqual(worker_output_field(worker, "status"), "done")
        attempt = self.assert_single_worker_attempt(worker, status="completed", session_id="ses_initial")
        self.assertEqual(attempt.get("id"), "attempt-1")
        self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
        self.assertEqual(attempt.get("started_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("finished_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("result_status"), "done")
        self.assertEqual(attempt.get("user_message_id"), "msg_user")
        self.assertEqual(attempt.get("assistant_message_id"), "msg_assistant")

    def test_execute_worker_attempts_uses_executor_protocol_request_with_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker.update_canonical_fields(timeout_seconds=1)
            client = FakeClient(["ses_initial"])
            executions = []

            class RecordingExecutor:
                def execute_prompt(self, execution):
                    executions.append(execution)
                    return {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=RecordingExecutor(),
                now=lambda: "2026-07-03T00:00:00Z",
            )

        self.assertEqual(outcome.kind, "completed")
        self.assertEqual(len(executions), 1)
        execution = executions[0]
        self.assertIsInstance(execution, WorkerPromptExecution)
        self.assertIs(execution.client, client)
        self.assertEqual(execution.session_id, "ses_initial")
        self.assertEqual(execution.prompt, "Finish the worker task")
        self.assertEqual(execution.capabilities, CAPABILITIES)
        self.assertIsNotNone(execution.deadline)
        self.assertLessEqual(execution.deadline.remaining(), 1)

    def test_execute_worker_attempts_returns_retry_scheduled_after_one_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker.update_canonical_fields(retry_limit=1, retryable_failures=["provider"])
            client = FakeClient(["ses_initial"])
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                if len(executions) > 1:
                    self.fail("worker execution should return retry_scheduled instead of retrying inline")
                raise BlockingProviderFailure("transient provider outage", prompt_id="msg_user_failed")

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

        self.assertEqual(outcome.kind, "retry_scheduled")
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.failure_category, "provider")
        self.assertEqual(outcome.created_session_ids, ["ses_initial"])
        self.assertEqual(executions, [("ses_initial", "Finish the worker task")])
        self.assertEqual(worker_output_field(worker, "status"), "active")
        self.assertEqual(worker_output_field(worker, "next_eligible_action"), "retry")
        self.assertEqual(worker_field(worker, "retry_count"), 1)
        self.assertEqual(worker_field(worker, "last_failure_category"), "provider")
        self.assertEqual(worker_field(worker, "last_failure_reason"), "transient provider outage")
        attempt = self.assert_single_worker_attempt(worker, status="retry_scheduled", session_id="ses_initial")
        self.assertEqual(attempt.get("user_message_id"), "msg_user_failed")

    def test_worker_execution_executor_persists_attempt_record_before_blocking_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            client = FakeClient(["ses_initial"])
            persisted_workers = []

            def persist_worker_transition(run, worker, transition):
                persisted_run = deepcopy(run)
                updated = apply_worker_transition(persisted_run.setdefault("workers", {}), transition)
                persisted_workers.append(deepcopy(updated))
                return persisted_run, updated

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                self.assertTrue(persisted_workers)
                persisted_worker = persisted_workers[-1]
                self.assertEqual(worker_output_field(persisted_worker, "status"), "active")
                self.assertEqual(worker_output_field(persisted_worker, "next_eligible_action"), "wait")
                attempt = self.assert_single_worker_attempt(
                    persisted_worker,
                    status="active",
                    session_id="ses_initial",
                )
                self.assertEqual(attempt.get("id"), "attempt-1")
                self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
                self.assertEqual(attempt.get("started_at"), "2026-07-03T00:00:00Z")
                self.assertIsNone(attempt.get("finished_at"))
                self.assertNotIn("result_status", attempt)
                return {"status": "done", "message_ids": {"user": "msg_user", "assistant": "msg_assistant"}}

            worker_executor = WorkerExecutionExecutor(
                apply_transition=persist_worker_transition,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )
            outcome = worker_executor.execute(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
            )

        self.assertEqual(outcome.kind, "completed")
        self.assertTrue(persisted_workers)
        persisted_worker = persisted_workers[-1]
        self.assertEqual(worker_output_field(persisted_worker, "status"), "done")
        self.assertEqual(worker_output_field(persisted_worker, "next_eligible_action"), "collect")
        attempt = self.assert_single_worker_attempt(persisted_worker, status="completed", session_id="ses_initial")
        self.assertEqual(attempt.get("id"), "attempt-1")
        self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
        self.assertEqual(attempt.get("started_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("finished_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("result_status"), "done")
        self.assertEqual(attempt.get("user_message_id"), "msg_user")
        self.assertEqual(attempt.get("assistant_message_id"), "msg_assistant")


if __name__ == "__main__":
    unittest.main()

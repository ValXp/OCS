from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.run_persistence import PersistedWorkerTransitions
from opencode_session.run_start_core import RunStartCore
from opencode_session.worker_attempt_execution import WorkerPromptExecution
from opencode_session.worker_cleanup_recovery import (
    cleanup_created_worker_sessions,
    recoverable_created_worker_sessions_by_worker,
)
from opencode_session.worker_execution import (
    WorkerExecutionTimeout,
    execute_worker_attempts,
)
from opencode_session.worker_session_provisioning import (
    WORKER_SESSION_JOURNAL_FIELD,
    WorkerSessionCreatedRecord,
    WorkerSessionCreationJournal,
    WorkerSessionCreationIntent,
    WorkerSessionProvisioner,
    ensure_worker_session,
    provision_worker_session,
)
from opencode_session.worker_state import WorkerRecord, apply_worker_transition, ensure_worker


CAPABILITIES = {
    "route_availability": {
        "blocking_message": {"path": "/session/{sessionID}/message", "method": "POST", "available": False},
        "legacy_run": {"path": "/session/{sessionID}/run", "method": "POST", "available": True},
        "legacy_reply": {"path": "/session/{sessionID}/reply", "method": "POST", "available": True},
    },
    "blocking_message_available": False,
    "blocking_execution_available": True,
    "legacy_fallback_available": True,
}


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeClient:
    def __init__(self, session_ids, *, delete_failures=None):
        self.requests = []
        self.session_ids = list(session_ids)
        self.delete_failures = dict(delete_failures or {})

    def create_session_response(self, directory, *, agent=None, model=None):
        self.requests.append(("create", directory, agent, model))
        return FakeResponse({"id": self.session_ids.pop(0), "directory": directory})

    def delete_session_response(self, session_id):
        self.requests.append(("delete", session_id))
        if session_id in self.delete_failures:
            raise self.delete_failures[session_id]

    def delete_session(self, session_id):
        response = self.delete_session_response(session_id)
        return response.data if response is not None else None

    def get_session(self, session_id):
        self.requests.append(("get", session_id))
        raise OpenCodeApiError(f"session not found: {session_id}", status=404)


class WorkerExecutionTest(unittest.TestCase):
    def assert_single_worker_attempt(self, worker, *, status, session_id):
        attempts = worker.get("attempts")
        self.assertIsInstance(attempts, list)
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt.get("session_id"), session_id)
        self.assertEqual(attempt.get("status"), status)
        return attempt

    def test_worker_session_creation_records_serialize_journal_entries(self):
        intent = WorkerSessionCreationIntent(
            id="worker-session-intent-1",
            worker_id="worker",
            directory="/workspace",
            agent="build",
            model="openai/gpt-5.5",
            cleanup_requested=True,
            intent_recorded_at="2026-07-05T00:00:00Z",
        )
        created = WorkerSessionCreatedRecord(
            id="worker-session-intent-1",
            worker_id="worker",
            session_id="ses_new",
            cleanup_requested=True,
            created_at="2026-07-05T00:00:00Z",
        )

        self.assertEqual(
            intent.to_journal_entry(),
            {
                "id": "worker-session-intent-1",
                "kind": "worker_session_create",
                "status": "intent",
                "worker_id": "worker",
                "directory": "/workspace",
                "agent": "build",
                "model": "openai/gpt-5.5",
                "cleanup_requested": True,
                "intent_recorded_at": "2026-07-05T00:00:00Z",
            },
        )
        self.assertEqual(
            created.to_journal_update(),
            {
                "status": "created",
                "session_id": "ses_new",
                "created_session_ids": ["ses_new"],
                "created_at": "2026-07-05T00:00:00Z",
            },
        )
        self.assertEqual(
            created.to_journal_entry(),
            {
                "id": "worker-session-intent-1",
                "kind": "worker_session_create",
                "worker_id": "worker",
                "cleanup_requested": True,
                "status": "created",
                "session_id": "ses_new",
                "created_session_ids": ["ses_new"],
                "created_at": "2026-07-05T00:00:00Z",
            },
        )

    def test_ensure_worker_session_uses_worker_record_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            client = FakeClient(["ses_new"])
            calls = []
            original = WorkerRecord.set_session

            def traced_set_session(self, session_id, *, agent=None, model=None):
                calls.append((self.worker_id, session_id, agent, model))
                return original(self, session_id, agent=agent, model=model)

            with patch.object(WorkerRecord, "set_session", traced_set_session):
                outcome = ensure_worker_session(
                    client,
                    run,
                    worker,
                    agent="build",
                    model="openai/gpt-5.5",
                    treat_falsey_session_as_missing=True,
                )

        self.assertIsInstance(worker, WorkerRecord)
        self.assertEqual(outcome.session_id, "ses_new")
        self.assertEqual(calls, [("worker", "ses_new", "build", "openai/gpt-5.5")])
        self.assertEqual(worker["session_id"], "ses_new")

    def test_provision_without_create_uses_worker_record_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            client = FakeClient([])
            calls = []
            original = WorkerRecord.set_session

            def traced_set_session(self, session_id, *, agent=None, model=None):
                calls.append((self.worker_id, session_id, agent, model))
                return original(self, session_id, agent=agent, model=model)

            with patch.object(WorkerRecord, "set_session", traced_set_session):
                outcome = provision_worker_session(
                    client,
                    run,
                    worker,
                    session_id="ses_existing",
                    agent="plan",
                    model="openai/gpt-5.5",
                    create_session=False,
                )

        self.assertEqual(client.requests, [])
        self.assertEqual(outcome.session_id, "ses_existing")
        self.assertEqual(calls, [("worker", "ses_existing", "plan", "openai/gpt-5.5")])
        self.assertEqual(worker["session_id"], "ses_existing")

    def test_cleanup_created_worker_sessions_clears_stale_sessions_after_single_session_success(self):
        worker = WorkerRecord.from_worker(
            {
                "id": "worker",
                "cleanup": {
                    "requested": True,
                    "deleted": False,
                    "error": "DELETE /api/session/ses_old failed: HTTP 500",
                    "sessions": ["ses_old", "ses_retry"],
                },
            },
            "worker",
        ).to_worker()
        client = FakeClient([])

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_new"])

        self.assertEqual(client.requests, [("delete", "ses_new"), ("get", "ses_new")])
        self.assertEqual(outcome.deleted_session_ids, ["ses_new"])
        self.assertIsNone(outcome.error)
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": True})

    def test_cleanup_created_worker_sessions_treats_missing_session_as_deleted(self):
        worker = WorkerRecord.default_fields("worker")
        client = FakeClient(
            [],
            delete_failures={"ses_missing": OpenCodeApiError("session not found", status=404)},
        )

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_missing"])

        self.assertEqual(client.requests, [("delete", "ses_missing"), ("get", "ses_missing")])
        self.assertEqual(outcome.deleted_session_ids, ["ses_missing"])
        self.assertIsNone(outcome.error)
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": True})

    def test_worker_session_journal_records_cleanup_failure_when_discard_fails(self):
        run = {"name": "demo", "directory": "/workspace", "workers": {"worker": {"id": "worker"}}}
        worker = run["workers"]["worker"]
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            if len(calls) == 2:
                raise RuntimeError("forced cleanup failure")
            mutator(run)
            return run

        journal = WorkerSessionCreationJournal(
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
            id_factory=lambda: "worker-session-intent-1",
        )

        run, worker, intent = journal.record_intent(run, worker, cleanup_requested=True)
        run, worker = journal.discard_intent_best_effort(run, worker, intent)

        self.assertEqual(calls, ["persist", "persist", "persist"])
        self.assertIs(worker, run["workers"]["worker"])
        entry = run[WORKER_SESSION_JOURNAL_FIELD][0]
        self.assertEqual(entry["id"], "worker-session-intent-1")
        self.assertEqual(entry["kind"], "worker_session_create")
        self.assertEqual(entry["status"], "intent")
        self.assertTrue(entry["cleanup_requested"])
        self.assertEqual(
            entry["cleanup_failure"],
            {
                "operation": "discard_worker_session_create",
                "error_type": "RuntimeError",
                "message": "forced cleanup failure",
                "recorded_at": "2026-07-05T00:00:00Z",
            },
        )

    def test_worker_session_provisioner_records_created_session_before_finalize(self):
        run = {"name": "demo", "directory": "/workspace", "workers": {"worker": {"id": "worker"}}}
        worker = run["workers"]["worker"]
        journals = []

        def persist_run_mutation(run, mutator):
            mutator(run)
            journals.append(deepcopy(run.get(WORKER_SESSION_JOURNAL_FIELD)))
            return run

        journal = WorkerSessionCreationJournal(
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
            id_factory=lambda: "worker-session-intent-1",
        )
        provisioner = WorkerSessionProvisioner(session_journal=journal)
        client = FakeClient(["ses_new"])

        provisioning = provisioner.provision(
            client,
            run,
            worker,
            agent="build",
            model="openai/gpt-5.5",
            cleanup_requested=True,
        )

        self.assertEqual(client.requests, [("create", "/workspace", "build", "openai/gpt-5.5")])
        self.assertEqual(provisioning.outcome.session_id, "ses_new")
        self.assertEqual(provisioning.outcome.created_session_id, "ses_new")
        self.assertEqual(journals[0][0]["status"], "intent")
        self.assertEqual(journals[1][0]["status"], "created")
        self.assertIs(provisioning.worker, run["workers"]["worker"])
        self.assertEqual(provisioning.worker["session_id"], "ses_new")
        self.assertEqual(provisioning.worker["cleanup"], {"requested": True, "deleted": False, "sessions": ["ses_new"]})

        finalized_run, finalized_worker = provisioner.finalize_best_effort(
            provisioning.run,
            provisioning.worker,
            provisioning,
        )

        self.assertIs(finalized_run, run)
        self.assertIs(finalized_worker, provisioning.worker)
        self.assertNotIn(WORKER_SESSION_JOURNAL_FIELD, run)

    def test_recoverable_created_worker_sessions_merges_cleanup_and_journal_transactions(self):
        run = {
            "workers": {
                "worker": {
                    "cleanup": {
                        "deleted": False,
                        "sessions": ["ses_worker_cleanup", "ses_duplicate"],
                    }
                },
                "deleted": {
                    "cleanup": {
                        "deleted": True,
                        "sessions": ["ses_deleted"],
                    }
                },
            },
            WORKER_SESSION_JOURNAL_FIELD: [
                {
                    "id": "worker-session-intent-1",
                    "kind": "worker_session_create",
                    "worker_id": "worker",
                    "cleanup_requested": True,
                    "created_session_ids": ["ses_duplicate", "ses_created"],
                    "session_id": "ses_created",
                },
                {
                    "id": "worker-session-intent-2",
                    "kind": "worker_session_create",
                    "worker_id": "other",
                    "cleanup_requested": True,
                    "session_id": "ses_other",
                },
                {
                    "id": "worker-session-intent-3",
                    "kind": "worker_session_create",
                    "worker_id": "skipped",
                    "cleanup_requested": False,
                    "session_id": "ses_skipped",
                },
            ],
        }

        self.assertEqual(
            recoverable_created_worker_sessions_by_worker(run),
            {
                "worker": ["ses_worker_cleanup", "ses_duplicate", "ses_created"],
                "other": ["ses_other"],
            },
        )

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
        self.assertIsNone(worker["session_id"])
        self.assertIsNone(worker["agent"])
        self.assertIsNone(worker["model"])
        self.assertNotIn("result", worker)

    def test_execute_worker_attempts_applies_active_attempt_before_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            client = FakeClient(["ses_initial"])
            executions = []

            def execute_prompt(client, session_id, prompt, capabilities):
                executions.append((session_id, prompt))
                self.assertEqual(worker["status"], "active")
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
        self.assertEqual(worker["status"], "done")
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
            worker["timeout_seconds"] = 1
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

    def test_execute_worker_attempts_skips_automatic_timeout_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker["timeout_seconds"] = 0.05
            worker["retry_limit"] = 1
            worker["retryable_failures"] = ["timeout"]
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
        self.assertEqual(worker["session_id"], "ses_initial")
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["retry_count"], 0)
        self.assertTrue(worker["manual_retry_required"])
        self.assertEqual(worker["next_eligible_action"], "retry")
        self.assertEqual(worker["failure_reason"], "worker timed out after 0.05s")
        self.assertNotIn("timeout_retry_sessions", worker)
        self.assertNotIn("result", worker)

    def test_run_start_core_persists_attempt_record_before_blocking_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            client = FakeClient(["ses_initial"])
            persisted_workers = []

            def persist_worker_transition(run, transition):
                persisted_run = deepcopy(run)
                updated = apply_worker_transition(persisted_run.setdefault("workers", {}), transition)
                persisted_workers.append(deepcopy(updated))
                return PersistedWorkerTransitions(persisted_run, [updated])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                self.assertTrue(persisted_workers)
                persisted_worker = persisted_workers[-1]
                self.assertEqual(persisted_worker["status"], "active")
                self.assertEqual(persisted_worker["next_eligible_action"], "wait")
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

            core = RunStartCore(
                persist_worker_transition=persist_worker_transition,
                refresh_run_summary=lambda run: None,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )
            outcome = core.execute_worker(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
            )

        self.assertEqual(outcome.kind, "completed")
        self.assertTrue(persisted_workers)
        persisted_worker = persisted_workers[-1]
        self.assertEqual(persisted_worker["status"], "done")
        self.assertEqual(persisted_worker["next_eligible_action"], "collect")
        attempt = self.assert_single_worker_attempt(persisted_worker, status="completed", session_id="ses_initial")
        self.assertEqual(attempt.get("id"), "attempt-1")
        self.assertEqual(attempt.get("created_session_ids"), ["ses_initial"])
        self.assertEqual(attempt.get("started_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("finished_at"), "2026-07-03T00:00:00Z")
        self.assertEqual(attempt.get("result_status"), "done")
        self.assertEqual(attempt.get("user_message_id"), "msg_user")
        self.assertEqual(attempt.get("assistant_message_id"), "msg_assistant")

    def test_execute_worker_attempts_does_not_start_retry_session_after_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker["timeout_seconds"] = 0.05
            worker["retry_limit"] = 1
            worker["retryable_failures"] = ["timeout"]
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
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["session_id"], "ses_initial")
        self.assertEqual(worker["retry_count"], 0)
        self.assertEqual(worker["last_failure_category"], "timeout")
        self.assertEqual(worker["last_failure_reason"], "worker timed out after 0.05s")
        self.assertEqual(worker["next_eligible_action"], "retry")
        self.assertTrue(worker["manual_retry_required"])
        self.assertNotIn("result", worker)
        self.assertNotIn("timeout_retry_sessions", worker)

    def test_execute_worker_attempts_does_not_schedule_timeout_retry_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker["timeout_seconds"] = 0.05
            worker["retry_limit"] = 1
            worker["retryable_failures"] = ["timeout"]
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
                stop_after_retry=True,
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
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["next_eligible_action"], "retry")
        self.assertEqual(worker["session_id"], "ses_initial")


if __name__ == "__main__":
    unittest.main()

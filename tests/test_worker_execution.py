from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.run_record import run_directory, run_record_for_output
from opencode_session.run_store import RunStoreError
from opencode_session.remote_journal import (
    OUTBOX_STATE_APPLIED,
    OUTBOX_STATE_INTENT,
    OUTBOX_STATE_REMOTE_SUCCEEDED,
    OUTBOX_STATE_UNCERTAIN,
)
from opencode_session.worker_attempt_execution import WorkerPromptExecution
from opencode_session.worker_cleanup_recovery import (
    cleanup_created_worker_sessions,
    recoverable_created_worker_sessions_by_worker,
)
from opencode_session.worker_execution import (
    WorkerExecutionExecutor,
    WorkerExecutionTimeout,
    execute_worker_attempts,
)
from opencode_session.worker_session_provisioning import (
    WORKER_SESSION_JOURNAL_FIELD,
    WorkerSessionCreationJournal,
    WorkerSessionProvisioner,
    ensure_worker_session,
    provision_worker_session,
    recoverable_worker_session_creations_by_worker,
)
from opencode_session.worker_storage_adapter import hydrate_worker_record
from opencode_session.worker_state import (
    WorkerRecord,
    apply_worker_transition,
    ensure_worker,
    worker_field,
    worker_has_field,
    worker_output_field,
)


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
        self.create_session_metadata = []
        self.session_ids = list(session_ids)
        self.delete_failures = dict(delete_failures or {})

    def create_session_response(self, directory, *, agent=None, model=None, metadata=None):
        self.requests.append(("create", directory, agent, model))
        self.create_session_metadata.append(metadata)
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
        attempts = worker_field(worker, "attempts")
        self.assertIsInstance(attempts, list)
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt.get("session_id"), session_id)
        self.assertEqual(attempt.get("status"), status)
        return attempt

    def test_worker_session_creation_journal_records_intent_and_created_entry(self):
        run = {
            "name": "demo",
            "directory": "/workspace",
            "workers": {"worker": WorkerRecord.default_fields("worker")},
        }
        worker = run["workers"]["worker"]

        def persist_run_mutation(run, mutator):
            mutator(run)
            return run

        journal = WorkerSessionCreationJournal(
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
            id_factory=lambda: "worker-session-intent-1",
        )

        run, worker, intent = journal.record_intent(
            run,
            worker,
            agent="build",
            model="openai/gpt-5.5",
            cleanup_requested=True,
        )

        self.assertEqual(
            intent.to_journal_entry(),
            {
                "id": "worker-session-intent-1",
                "kind": "worker_session_create",
                "worker_id": "worker",
                "directory": "/workspace",
                "agent": "build",
                "model": "openai/gpt-5.5",
                "cleanup_requested": True,
                "intent_recorded_at": "2026-07-05T00:00:00Z",
            },
        )
        self.assertEqual(run[WORKER_SESSION_JOURNAL_FIELD][0]["outbox_state"], OUTBOX_STATE_INTENT)

        run, worker = journal.record_created(
            run,
            worker,
            intent,
            "ses_new",
            agent="build",
            model="openai/gpt-5.5",
        )

        self.assertEqual(
            run[WORKER_SESSION_JOURNAL_FIELD][0],
            {
                "id": "worker-session-intent-1",
                "kind": "worker_session_create",
                "outbox_state": OUTBOX_STATE_APPLIED,
                "worker_id": "worker",
                "directory": "/workspace",
                "agent": "build",
                "model": "openai/gpt-5.5",
                "cleanup_requested": True,
                "intent_recorded_at": "2026-07-05T00:00:00Z",
                "session_id": "ses_new",
                "created_session_ids": ["ses_new"],
                "created_at": "2026-07-05T00:00:00Z",
            },
        )

        worker = run["workers"]["worker"]
        self.assertEqual(worker_field(worker, "session_id"), "ses_new")
        self.assertEqual(worker_field(worker, "agent"), "build")
        self.assertEqual(worker_field(worker, "model"), "openai/gpt-5.5")
        self.assertEqual(
            worker_field(worker, "cleanup"),
            {"requested": True, "deleted": False, "sessions": ["ses_new"]},
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

            with patch.object(WorkerRecord, "set_session", traced_set_session), patch(
                "opencode_session.worker_session_provisioning.run_directory",
                wraps=run_directory,
            ) as directory_accessor:
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
        self.assertEqual(directory_accessor.call_count, 1)
        self.assertEqual(calls, [("worker", "ses_new", "build", "openai/gpt-5.5")])
        self.assertEqual(worker_field(worker, "session_id"), "ses_new")

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
        self.assertEqual(worker_field(worker, "session_id"), "ses_existing")

    def test_cleanup_created_worker_sessions_clears_stale_sessions_after_single_session_success(self):
        worker = WorkerRecord(
            "worker",
            {
                "id": "worker",
                "cleanup": {
                    "requested": True,
                    "deleted": False,
                    "error": "DELETE /api/session/ses_old failed: HTTP 500",
                    "sessions": ["ses_old", "ses_retry"],
                },
            },
        ).to_worker()
        client = FakeClient([])

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_new"])

        self.assertEqual(client.requests, [("delete", "ses_new"), ("get", "ses_new")])
        self.assertEqual(outcome.deleted_session_ids, ["ses_new"])
        self.assertIsNone(outcome.error)
        self.assertEqual(worker_field(worker, "cleanup"), {"requested": True, "deleted": True})

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
        self.assertEqual(worker_field(worker, "cleanup"), {"requested": True, "deleted": True})

    def test_cleanup_created_worker_sessions_persists_only_pending_sessions_after_partial_failure(self):
        worker = WorkerRecord.default_fields("worker")
        failure = OpenCodeApiError("DELETE /api/session/ses_live failed: HTTP 500", status=500)
        client = FakeClient([], delete_failures={"ses_live": failure})

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_deleted", "ses_live"])

        self.assertEqual(
            client.requests,
            [("delete", "ses_deleted"), ("get", "ses_deleted"), ("delete", "ses_live")],
        )
        self.assertEqual(outcome.deleted_session_ids, ["ses_deleted"])
        self.assertIs(outcome.error, failure)
        self.assertEqual(
            worker_field(worker, "cleanup"),
            {
                "requested": True,
                "deleted": False,
                "error": "DELETE /api/session/ses_live failed: HTTP 500",
                "sessions": ["ses_live"],
                "verified": ["ses_deleted"],
            },
        )
        self.assertEqual(
            recoverable_created_worker_sessions_by_worker({"workers": {"worker": worker}}),
            {"worker": ["ses_live"]},
        )

    def test_worker_session_journal_records_cleanup_failure_when_discard_fails(self):
        run = {
            "name": "demo",
            "directory": "/workspace",
            "workers": {"worker": hydrate_worker_record({"id": "worker"}, "worker")},
        }
        worker = run["workers"]["worker"]
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            if len(calls) == 2:
                raise RunStoreError("forced cleanup failure")
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
        self.assertEqual(entry["outbox_state"], OUTBOX_STATE_INTENT)
        self.assertTrue(entry["cleanup_requested"])
        self.assertEqual(
            entry["cleanup_failure"],
            {
                "operation": "discard_worker_session_create",
                "error_type": "RunStoreError",
                "message": "forced cleanup failure",
                "recorded_at": "2026-07-05T00:00:00Z",
            },
        )

    def test_worker_session_provisioner_marks_creation_intent_uncertain_when_remote_create_fails(self):
        run = {
            "name": "demo",
            "directory": "/workspace",
            "workers": {"worker": hydrate_worker_record({"id": "worker"}, "worker")},
        }
        worker = run["workers"]["worker"]
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            mutator(run)
            return run

        class RejectingCreateClient(FakeClient):
            def create_session_response(self, directory, *, agent=None, model=None, metadata=None):
                self.requests.append(("create", directory, agent, model))
                self.create_session_metadata.append(metadata)
                raise RuntimeError("remote create rejected")

        journal = WorkerSessionCreationJournal(
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
            id_factory=lambda: "worker-session-intent-1",
        )
        provisioner = WorkerSessionProvisioner(session_journal=journal)
        client = RejectingCreateClient([])

        with self.assertRaisesRegex(RuntimeError, "remote create rejected"):
            provisioner.provision(
                client,
                run,
                worker,
                agent="build",
                model="openai/gpt-5.5",
                cleanup_requested=True,
            )

        self.assertEqual(calls, ["persist", "persist"])
        self.assertEqual(client.requests, [("create", "/workspace", "build", "openai/gpt-5.5")])
        self.assertEqual(
            client.create_session_metadata,
            [
                {
                    "ocs.remote_mutation_kind": "worker_session_create",
                    "ocs.remote_mutation_id": "worker-session-intent-1",
                    "ocs.worker_id": "worker",
                    "ocs.cleanup_requested": "true",
                    "ocs.run_name": "demo",
                }
            ],
        )
        self.assertIsNone(worker_field(run["workers"]["worker"], "session_id"))
        entry = run[WORKER_SESSION_JOURNAL_FIELD][0]
        self.assertEqual(entry["id"], "worker-session-intent-1")
        self.assertEqual(entry["kind"], "worker_session_create")
        self.assertEqual(entry["outbox_state"], OUTBOX_STATE_UNCERTAIN)
        self.assertTrue(entry["cleanup_requested"])
        self.assertEqual(
            entry["uncertain_failure"],
            {
                "operation": "call_worker_session_create",
                "error_type": "RuntimeError",
                "message": "remote create rejected",
                "recorded_at": "2026-07-05T00:00:00Z",
            },
        )

    def test_worker_session_provisioner_records_created_session_before_finalize(self):
        run = {
            "name": "demo",
            "directory": "/workspace",
            "workers": {"worker": hydrate_worker_record({"id": "worker"}, "worker")},
        }
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
        self.assertEqual(
            client.create_session_metadata,
            [
                {
                    "ocs.remote_mutation_kind": "worker_session_create",
                    "ocs.remote_mutation_id": "worker-session-intent-1",
                    "ocs.worker_id": "worker",
                    "ocs.cleanup_requested": "true",
                    "ocs.run_name": "demo",
                }
            ],
        )
        self.assertEqual(provisioning.outcome.session_id, "ses_new")
        self.assertEqual(provisioning.outcome.created_session_id, "ses_new")
        self.assertEqual(journals[0][0]["outbox_state"], OUTBOX_STATE_INTENT)
        self.assertEqual(journals[1][0]["outbox_state"], OUTBOX_STATE_REMOTE_SUCCEEDED)
        self.assertEqual(journals[1][0]["session_id"], "ses_new")
        self.assertEqual(journals[1][0]["created_session_ids"], ["ses_new"])
        self.assertEqual(journals[2][0]["outbox_state"], OUTBOX_STATE_APPLIED)
        self.assertIs(provisioning.worker, run["workers"]["worker"])
        self.assertEqual(worker_field(provisioning.worker, "session_id"), "ses_new")
        self.assertEqual(
            worker_field(provisioning.worker, "cleanup"),
            {"requested": True, "deleted": False, "sessions": ["ses_new"]},
        )

        finalized_run, finalized_worker = provisioner.finalize_best_effort(
            provisioning.run,
            provisioning.worker,
            provisioning,
        )

        self.assertIs(finalized_run, run)
        self.assertIs(finalized_worker, provisioning.worker)
        self.assertNotIn(WORKER_SESSION_JOURNAL_FIELD, run)

    def test_worker_session_provisioner_recovers_created_session_when_local_apply_fails(self):
        run = {
            "name": "demo",
            "directory": "/workspace",
            "workers": {"worker": hydrate_worker_record({"id": "worker"}, "worker")},
        }
        worker = run["workers"]["worker"]
        calls = []

        def persist_run_mutation(run, mutator):
            calls.append("persist")
            if len(calls) == 3:
                raise RunStoreError("forced worker session apply failure")
            mutator(run)
            return run

        journal = WorkerSessionCreationJournal(
            persist_run_mutation,
            now=lambda: "2026-07-05T00:00:00Z",
            id_factory=lambda: "worker-session-intent-1",
        )
        provisioner = WorkerSessionProvisioner(session_journal=journal)
        client = FakeClient(["ses_new"])

        with self.assertRaisesRegex(RunStoreError, "forced worker session apply failure"):
            provisioner.provision(
                client,
                run,
                worker,
                agent="build",
                model="openai/gpt-5.5",
                cleanup_requested=True,
            )

        self.assertEqual(calls, ["persist", "persist", "persist"])
        self.assertEqual(client.requests, [("create", "/workspace", "build", "openai/gpt-5.5")])
        self.assertIsNone(worker_field(run["workers"]["worker"], "cleanup"))
        entry = run[WORKER_SESSION_JOURNAL_FIELD][0]
        self.assertEqual(entry["id"], "worker-session-intent-1")
        self.assertEqual(entry["kind"], "worker_session_create")
        self.assertEqual(entry["outbox_state"], OUTBOX_STATE_REMOTE_SUCCEEDED)
        self.assertEqual(entry["session_id"], "ses_new")
        self.assertEqual(entry["created_session_ids"], ["ses_new"])
        self.assertTrue(entry["cleanup_requested"])
        self.assertEqual(
            recoverable_worker_session_creations_by_worker(run),
            {"worker": ["ses_new"]},
        )

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

    def test_recoverable_created_worker_sessions_merges_cleanup_and_journal_transactions(self):
        run = {
            "workers": {
                "worker": hydrate_worker_record(
                    {
                        "id": "worker",
                        "cleanup": {
                            "deleted": False,
                            "sessions": ["ses_worker_cleanup", "ses_duplicate"],
                        },
                    },
                    "worker",
                ),
                "deleted": hydrate_worker_record(
                    {
                        "id": "deleted",
                        "cleanup": {
                            "deleted": True,
                            "sessions": ["ses_deleted"],
                        },
                    },
                    "deleted",
                ),
            },
            WORKER_SESSION_JOURNAL_FIELD: [
                {
                    "id": "worker-session-intent-1",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "worker_id": "worker",
                    "cleanup_requested": True,
                    "created_session_ids": ["ses_duplicate", "ses_created"],
                    "session_id": "ses_created",
                },
                {
                    "id": "worker-session-intent-2",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "worker_id": "other",
                    "cleanup_requested": True,
                    "session_id": "ses_other",
                },
                {
                    "id": "worker-session-intent-3",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "worker_id": "skipped",
                    "cleanup_requested": False,
                    "session_id": "ses_skipped",
                },
                {
                    "id": "worker-session-intent-4",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_REMOTE_SUCCEEDED,
                    "worker_id": "remote-succeeded",
                    "cleanup_requested": True,
                    "session_id": "ses_remote_succeeded",
                },
                {
                    "id": "worker-session-intent-5",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_INTENT,
                    "worker_id": "pending",
                    "cleanup_requested": True,
                    "session_id": "ses_pending",
                },
            ],
        }

        self.assertEqual(
            recoverable_created_worker_sessions_by_worker(run),
            {
                "worker": ["ses_worker_cleanup", "ses_duplicate", "ses_created"],
                "other": ["ses_other"],
                "remote-succeeded": ["ses_remote_succeeded"],
            },
        )

    def test_worker_session_creation_recovery_reads_domain_records(self):
        run = {
            WORKER_SESSION_JOURNAL_FIELD: [
                {
                    "id": "worker-session-intent-1",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "worker_id": "worker",
                    "cleanup_requested": True,
                    "created_session_ids": ["ses_created", "ses_duplicate"],
                    "session_id": "ses_created",
                },
                {
                    "id": "worker-session-intent-2",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_APPLIED,
                    "worker_id": "skipped",
                    "cleanup_requested": False,
                    "session_id": "ses_skipped",
                },
                {
                    "id": "worker-session-intent-3",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_REMOTE_SUCCEEDED,
                    "worker_id": "remote-succeeded",
                    "cleanup_requested": True,
                    "session_id": "ses_remote_succeeded",
                },
                {
                    "id": "worker-session-intent-4",
                    "kind": "worker_session_create",
                    "outbox_state": OUTBOX_STATE_INTENT,
                    "worker_id": "pending",
                    "cleanup_requested": True,
                    "session_id": "ses_pending",
                },
            ]
        }

        self.assertEqual(
            recoverable_worker_session_creations_by_worker(run),
            {"worker": ["ses_created", "ses_duplicate"], "remote-succeeded": ["ses_remote_succeeded"]},
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

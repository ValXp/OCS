from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from opencode_session.api_transport import OpenCodeApiError
from opencode_session.run_record import ensure_run_worker, run_directory
from opencode_session.run_store import RunStoreError
from opencode_session.remote_journal import (
    OUTBOX_STATE_APPLIED,
    OUTBOX_STATE_INTENT,
    OUTBOX_STATE_REMOTE_SUCCEEDED,
    OUTBOX_STATE_UNCERTAIN,
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
from opencode_session.worker_state import WorkerRecord, worker_field

try:
    from tests.worker_execution_helpers import FakeClient
except ModuleNotFoundError:
    from worker_execution_helpers import FakeClient


class WorkerSessionProvisioningTest(unittest.TestCase):
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
            worker = ensure_run_worker(run, "worker", role="worker")
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
            worker = ensure_run_worker(run, "worker", role="worker")
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


if __name__ == "__main__":
    unittest.main()

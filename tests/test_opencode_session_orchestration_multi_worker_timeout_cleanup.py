import tempfile
import unittest

from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartRequest,
)
from opencode_session.run_start_core import (
    CreatedWorkerCleanupExecutor,
    CreatedWorkerCleanupPlan,
    CreatedWorkerCleanupPlanner,
    CreatedWorkerCleanupStep,
)
from opencode_session.run_persistence import PersistedWorkerTransitions
from opencode_session.run_store import RunStore
from opencode_session.timeout_boundary import TimeoutExpired
from opencode_session.worker_storage_adapter import hydrate_worker_record
from opencode_session.worker_state import (
    apply_worker_transition,
    worker_field,
    worker_has_field,
    worker_output_field,
)

try:
    from tests.multi_worker_orchestration_helpers import CAPABILITIES, FakeClient
except ModuleNotFoundError:
    from multi_worker_orchestration_helpers import CAPABILITIES, FakeClient


class DependencyOrderedSerialOrchestrationTimeoutCleanupTest(unittest.TestCase):
    def test_serial_start_with_cleanup_after_first_ready_worker_failure_does_not_precreate_later_session(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker("demo", "alpha", role="build", prompt="Run alpha")
            store.upsert_worker("demo", "beta", role="review", prompt="Run beta")
            client = FakeClient(["ses_alpha", "ses_beta"])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                raise BlockingProviderFailure("alpha failed")

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                DependencyOrderedSerialRunStartRequest(name="demo", worker_id="alpha", role="build", cleanup=True)
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "provider failure: alpha failed")
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_alpha", "Run alpha"),
                ("delete", "ses_alpha"),
                ("get", "ses_alpha"),
            ],
        )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker_output_field(run["workers"]["alpha"], "status"), "failed")
        self.assertEqual(worker_field(run["workers"]["alpha"], "cleanup"), {"requested": True, "deleted": True})
        self.assertEqual(worker_output_field(run["workers"]["beta"], "status"), "queued")
        self.assertIsNone(worker_field(run["workers"]["beta"], "session_id"))
        self.assertFalse(worker_has_field(run["workers"]["beta"], "cleanup"))

    def test_cleanup_attempts_later_worker_after_earlier_worker_delete_fails(self):
        first_error = "DELETE /api/session/ses_alpha failed: HTTP 500"
        client = FakeClient([], delete_failures={"ses_alpha": first_error})
        run = {
            "status": "done",
            "workers": {
                "alpha": hydrate_worker_record({"id": "alpha", "lifecycle_state": "done_collect"}, "alpha"),
                "beta": hydrate_worker_record({"id": "beta", "lifecycle_state": "done_collect"}, "beta"),
            },
        }
        persisted_worker_ids = []

        def persist_worker_transition(run, transition):
            persisted_worker_ids.append(transition.worker_id)
            updated = apply_worker_transition(run.setdefault("workers", {}), transition)
            return PersistedWorkerTransitions(run, [updated])

        cleanup_executor = CreatedWorkerCleanupExecutor(
            persist_worker_transition=persist_worker_transition,
            refresh_run_summary=lambda run: None,
        )

        outcome = cleanup_executor.cleanup(
            client,
            run,
            CreatedWorkerCleanupPlan(
                (
                    CreatedWorkerCleanupStep("alpha", ("ses_alpha",)),
                    CreatedWorkerCleanupStep("beta", ("ses_beta",)),
                )
            ),
        )

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, f"api failure: disposable session cleanup failed: {first_error}")
        self.assertEqual(client.requests, [("delete", "ses_alpha"), ("delete", "ses_beta"), ("get", "ses_beta")])
        self.assertEqual(
            worker_field(run["workers"]["alpha"], "cleanup"),
            {"requested": True, "deleted": False, "error": first_error},
        )
        self.assertEqual(worker_output_field(run["workers"]["alpha"], "status"), "done")
        self.assertIsNone(worker_field(run["workers"]["alpha"], "failure_reason"))
        self.assertEqual(worker_field(run["workers"]["beta"], "cleanup"), {"requested": True, "deleted": True})
        self.assertEqual(persisted_worker_ids, ["alpha", "beta"])

    def test_cleanup_planner_merges_remembered_and_recoverable_sessions_without_deleting(self):
        client = FakeClient([])
        run = {
            "workers": {
                "worker": hydrate_worker_record(
                    {
                        "id": "worker",
                        "lifecycle_state": "done_collect",
                        "cleanup": {
                            "requested": True,
                            "deleted": False,
                            "sessions": ["ses_recovered"],
                        },
                    },
                    "worker",
                )
            }
        }

        plan = CreatedWorkerCleanupPlanner().plan(
            {"worker": ["ses_created", "ses_recovered"], "orphan": ["ses_orphan"]},
            run,
        )

        self.assertEqual(
            plan,
            CreatedWorkerCleanupPlan(
                (
                    CreatedWorkerCleanupStep("worker", ("ses_created", "ses_recovered")),
                    CreatedWorkerCleanupStep("orphan", ("ses_orphan",)),
                )
            ),
        )
        self.assertEqual(client.requests, [])
        self.assertEqual(
            worker_field(run["workers"]["worker"], "cleanup"),
            {"requested": True, "deleted": False, "sessions": ["ses_recovered"]},
        )

    def test_timeout_retry_is_manual_and_keeps_original_session(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                timeout_seconds=0.01,
                retry_limit=1,
                retryable_failures=["timeout"],
            )
            client = FakeClient(["ses_initial", "ses_retry"])

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
                raise TimeoutExpired()

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                DependencyOrderedSerialRunStartRequest(name="demo", worker_id="worker", role="worker")
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 124)
        self.assertIn("automatic timeout retry skipped", outcome.error)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        retry_worker = run["workers"]["worker"]
        self.assertEqual(worker_field(retry_worker, "session_id"), "ses_initial")
        self.assertEqual(worker_output_field(retry_worker, "status"), "timeout")
        self.assertEqual(worker_output_field(retry_worker, "next_eligible_action"), "retry")
        self.assertTrue(worker_field(retry_worker, "manual_retry_required"))
        self.assertFalse(worker_has_field(retry_worker, "result"))

    def test_cleanup_deletes_initial_session_after_timeout_without_retry(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                timeout_seconds=0.01,
                retry_limit=1,
                retryable_failures=["timeout"],
            )
            client = FakeClient(["ses_initial", "ses_retry"])

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
                raise TimeoutExpired()

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                DependencyOrderedSerialRunStartRequest(name="demo", worker_id="worker", role="worker", cleanup=True)
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 124)
        self.assertIn("automatic timeout retry skipped", outcome.error)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
                ("delete", "ses_initial"),
                ("get", "ses_initial"),
            ],
        )
        worker = run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "timeout")
        self.assertEqual(worker_field(worker, "cleanup"), {"requested": True, "deleted": True})
        self.assertTrue(worker_field(worker, "manual_retry_required"))

    def test_cleanup_reports_initial_session_delete_failure_after_timeout_without_retry(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                prompt="Finish the worker task",
                timeout_seconds=0.01,
                retry_limit=1,
                retryable_failures=["timeout"],
            )
            first_error = "DELETE /api/session/ses_initial failed: HTTP 500"
            client = FakeClient(["ses_initial", "ses_retry"], delete_failures={"ses_initial": first_error})

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
                raise TimeoutExpired()

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                DependencyOrderedSerialRunStartRequest(name="demo", worker_id="worker", role="worker", cleanup=True)
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, f"api failure: disposable session cleanup failed: {first_error}")
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
                ("delete", "ses_initial"),
            ],
        )
        worker = run["workers"]["worker"]
        self.assertEqual(worker_output_field(worker, "status"), "timeout")
        self.assertEqual(worker_field(worker, "failure_reason"), "worker timed out after 0.01s")
        self.assertEqual(worker_field(worker, "cleanup"), {"requested": True, "deleted": False, "error": first_error})


if __name__ == "__main__":
    unittest.main()

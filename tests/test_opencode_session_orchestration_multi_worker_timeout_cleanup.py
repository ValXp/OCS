import tempfile
import unittest

from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.multi_worker_orchestration import (
    DependencyOrderedSerialRunOrchestrationService,
    DependencyOrderedSerialRunStartRequest,
)
from opencode_session.run_start_core import RunStartCore
from opencode_session.run_store import RunStore
from opencode_session.timeout_boundary import TimeoutExpired

try:
    from tests.multi_worker_orchestration_helpers import CAPABILITIES, FakeClient
except ModuleNotFoundError:
    from multi_worker_orchestration_helpers import CAPABILITIES, FakeClient


class MultiWorkerOrchestrationTimeoutCleanupTest(unittest.TestCase):
    def test_start_with_cleanup_after_first_ready_worker_failure_does_not_precreate_later_worker_session(self):
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
        self.assertEqual(run["workers"]["alpha"]["status"], "failed")
        self.assertEqual(run["workers"]["alpha"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(run["workers"]["beta"]["status"], "queued")
        self.assertIsNone(run["workers"]["beta"]["session_id"])
        self.assertNotIn("cleanup", run["workers"]["beta"])

    def test_cleanup_attempts_later_worker_after_earlier_worker_delete_fails(self):
        first_error = "DELETE /api/session/ses_alpha failed: HTTP 500"
        client = FakeClient([], delete_failures={"ses_alpha": first_error})
        run = {
            "status": "done",
            "workers": {
                "alpha": {"id": "alpha", "status": "done"},
                "beta": {"id": "beta", "status": "done"},
            },
        }
        persisted_worker_ids = []
        core = RunStartCore(
            persist_worker_transition=lambda run, transition: persisted_worker_ids.append(transition.worker_id),
            refresh_run_summary=lambda run: None,
            now=lambda: "2026-07-03T00:00:00Z",
        )

        outcome = core.cleanup_created_workers(
            client,
            run,
            {"alpha": ["ses_alpha"], "beta": ["ses_beta"]},
        )

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, f"api failure: disposable session cleanup failed: {first_error}")
        self.assertEqual(client.requests, [("delete", "ses_alpha"), ("delete", "ses_beta"), ("get", "ses_beta")])
        self.assertEqual(
            run["workers"]["alpha"]["cleanup"],
            {"requested": True, "deleted": False, "error": first_error},
        )
        self.assertEqual(run["workers"]["alpha"]["status"], "done")
        self.assertNotIn("failure_reason", run["workers"]["alpha"])
        self.assertEqual(run["workers"]["beta"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(persisted_worker_ids, ["alpha", "beta"])

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
        self.assertEqual(retry_worker["session_id"], "ses_initial")
        self.assertEqual(retry_worker["status"], "timeout")
        self.assertEqual(retry_worker["next_eligible_action"], "retry")
        self.assertTrue(retry_worker["manual_retry_required"])
        self.assertNotIn("result", retry_worker)

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
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": True})
        self.assertTrue(worker["manual_retry_required"])

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
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["failure_reason"], "worker timed out after 0.01s")
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": False, "error": first_error})


if __name__ == "__main__":
    unittest.main()

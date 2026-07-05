import tempfile
import unittest

from opencode_session.run_state import SingleWorkerRunStartRequest, SingleWorkerRunStateService
from opencode_session.run_store import RunStore
from opencode_session.timeout_boundary import TimeoutExpired
from opencode_session.worker_execution import WorkerExecutionTimeout

try:
    from tests.single_worker_run_state_helpers import CAPABILITIES, FakeClient
except ModuleNotFoundError:
    from single_worker_run_state_helpers import CAPABILITIES, FakeClient


class SingleWorkerRunStateTimeoutTest(unittest.TestCase):
    def test_start_times_out_worker_and_persists_timeout_metadata(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                timeout_seconds=1,
            )
            client = FakeClient()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                raise WorkerExecutionTimeout()

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                SingleWorkerRunStartRequest(
                    name="demo",
                    worker_id="worker",
                    role="worker",
                    prompt="Finish the worker task",
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 124)
        self.assertEqual(outcome.error, "worker timed out after 1s")
        self.assertEqual(run["status"], "timeout")
        timeout_worker = run["workers"]["worker"]
        self.assertEqual(timeout_worker["status"], "timeout")
        self.assertEqual(timeout_worker["timeout_seconds"], 1)
        self.assertEqual(timeout_worker["timeout_policy"], "timeout")
        self.assertEqual(timeout_worker["timeout_started_at"], "2026-07-03T00:00:00Z")
        self.assertEqual(timeout_worker["timed_out_at"], "2026-07-03T00:00:00Z")
        self.assertEqual(timeout_worker["failure_category"], "timeout")
        self.assertEqual(timeout_worker["failure_reason"], "worker timed out after 1s")
        self.assertEqual(timeout_worker["next_eligible_action"], "none")
        self.assertNotIn("result", timeout_worker)

    def test_start_skips_automatic_timeout_retry_and_marks_manual_retry(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                timeout_seconds=0.05,
                retry_limit=1,
                retryable_failures=["timeout"],
            )
            client = FakeClient(session_ids=["ses_initial", "ses_retry"])

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
                raise TimeoutExpired()

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                SingleWorkerRunStartRequest(
                    name="demo",
                    worker_id="worker",
                    role="worker",
                    prompt="Finish the worker task",
                )
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
        self.assertEqual(retry_worker["status"], "timeout")
        self.assertEqual(retry_worker["session_id"], "ses_initial")
        self.assertEqual(retry_worker["retry_count"], 0)
        self.assertEqual(retry_worker["last_failure_category"], "timeout")
        self.assertEqual(retry_worker["last_failure_reason"], "worker timed out after 0.05s")
        self.assertEqual(retry_worker["next_eligible_action"], "retry")
        self.assertTrue(retry_worker["manual_retry_required"])
        self.assertNotIn("result", retry_worker)
        self.assertNotIn("timeout_retry_sessions", retry_worker)

    def test_timeout_aware_executor_receives_worker_deadline(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                timeout_seconds=0.01,
            )
            client = FakeClient(session_ids=["ses_initial"])
            remaining_values = []

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                remaining_values.append(deadline.remaining())
                raise TimeoutExpired()

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                SingleWorkerRunStartRequest(
                    name="demo",
                    worker_id="worker",
                    role="worker",
                    prompt="Finish the worker task",
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 124)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        self.assertEqual(len(remaining_values), 1)
        self.assertLessEqual(remaining_values[0], 0.01)
        timeout_worker = run["workers"]["worker"]
        self.assertEqual(timeout_worker["session_id"], "ses_initial")
        self.assertEqual(timeout_worker["status"], "timeout")

    def test_start_non_retry_timeout_keeps_timeout_recording_on_original_session(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                timeout_seconds=0.05,
            )
            client = FakeClient(session_ids=["ses_initial", "ses_unused"])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                raise WorkerExecutionTimeout()

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                SingleWorkerRunStartRequest(
                    name="demo",
                    worker_id="worker",
                    role="worker",
                    prompt="Finish the worker task",
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 124)
        self.assertEqual(outcome.error, "worker timed out after 0.05s")
        self.assertEqual(
            client.requests,
            [("create", directory, None, None), ("execute", "ses_initial", "Finish the worker task")],
        )
        self.assertEqual(run["status"], "timeout")
        timeout_worker = run["workers"]["worker"]
        self.assertEqual(timeout_worker["session_id"], "ses_initial")
        self.assertEqual(timeout_worker["status"], "timeout")
        self.assertEqual(timeout_worker["retry_count"], 0)
        self.assertEqual(timeout_worker["failure_category"], "timeout")
        self.assertEqual(timeout_worker["failure_reason"], "worker timed out after 0.05s")
        self.assertEqual(timeout_worker["next_eligible_action"], "none")
        self.assertNotIn("timeout_retry_sessions", timeout_worker)
        self.assertNotIn("result", timeout_worker)

    def test_timeout_boundary_uses_synchronous_deadline(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                timeout_seconds=0.05,
            )
            client = FakeClient()

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
                raise TimeoutExpired()

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )
            outcome = service.start(
                SingleWorkerRunStartRequest(
                    name="demo",
                    worker_id="worker",
                    role="worker",
                    prompt="Finish the worker task",
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 124)
        self.assertEqual(outcome.error, "worker timed out after 0.05s")
        self.assertEqual(run["status"], "timeout")
        self.assertEqual(run["workers"]["worker"]["failure_category"], "timeout")


if __name__ == "__main__":
    unittest.main()

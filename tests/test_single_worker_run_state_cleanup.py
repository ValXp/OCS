import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.run_state import SingleWorkerRunStartRequest, SingleWorkerRunStateService
from opencode_session.run_store import RunStore
from opencode_session.timeout_boundary import TimeoutExpired
from opencode_session.worker_execution import WorkerExecutionTimeout

try:
    from tests.single_worker_run_state_helpers import CAPABILITIES, FakeClient
except ModuleNotFoundError:
    from single_worker_run_state_helpers import CAPABILITIES, FakeClient


class SingleWorkerRunStateCleanupTest(unittest.TestCase):
    def test_start_with_cleanup_deletes_initial_session_after_timeout_without_retry(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                timeout_seconds=0.01,
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
                    cleanup=True,
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
                ("delete", "ses_initial"),
                ("get", "ses_initial"),
            ],
        )
        worker = run["workers"]["worker"]
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": True})
        self.assertTrue(worker["manual_retry_required"])

    def test_start_cleanup_deletes_created_session_after_execution_failure(self):
        cases = [
            (
                "api",
                lambda: OpenCodeApiError("HTTP 503 POST /session/ses_new/run: upstream overloaded", status=503),
                {},
                69,
                "api failure: HTTP 503 POST /session/ses_new/run: upstream overloaded",
            ),
            (
                "provider",
                lambda: BlockingProviderFailure("provider rejected request", prompt_id="msg_user_1"),
                {},
                69,
                "provider failure: provider rejected request",
            ),
            (
                "timeout",
                lambda: WorkerExecutionTimeout(),
                {"timeout_seconds": 1},
                124,
                "worker timed out after 1s",
            ),
        ]
        for name, error_factory, worker_options, expected_exit_code, expected_error in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
                    store = RunStore(store_root)
                    store.create_run("demo", directory=directory, server_url="http://opencode.example")
                    if worker_options:
                        store.upsert_worker("demo", "worker", role="worker", **worker_options)
                    client = FakeClient()

                    def execute_prompt(client, session_id, prompt, capabilities):
                        client.requests.append(("execute", session_id, prompt))
                        raise error_factory()

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
                            cleanup=True,
                        )
                    )
                    run = store.load_run("demo")

                self.assertEqual(outcome.exit_code, expected_exit_code)
                self.assertEqual(outcome.error, expected_error)
                self.assertEqual(
                    client.requests,
                    [
                        ("create", directory, None, None),
                        ("execute", "ses_new", "Finish the worker task"),
                        ("delete", "ses_new"),
                        ("get", "ses_new"),
                    ],
                )
                self.assertEqual(run["workers"]["worker"]["cleanup"], {"requested": True, "deleted": True})

    def test_start_cleanup_deletes_created_session_after_success(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            client = FakeClient()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_user_1", "assistant": "msg_assistant_1"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": "Worker finished.",
                }

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
                    directory=directory,
                    server_url="http://opencode.example",
                    cleanup=True,
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNone(outcome.error)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_new", "Finish the worker task"),
                ("delete", "ses_new"),
                ("get", "ses_new"),
            ],
        )
        self.assertEqual(run["workers"]["worker"]["cleanup"], {"requested": True, "deleted": True})


if __name__ == "__main__":
    unittest.main()

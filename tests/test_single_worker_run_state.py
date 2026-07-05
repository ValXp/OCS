import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.run_state import SingleWorkerRunStartRequest, SingleWorkerRunStateService
from opencode_session.run_store import RunStore

try:
    from tests.single_worker_run_state_helpers import CAPABILITIES, FakeClient, UNSUPPORTED_CAPABILITIES
except ModuleNotFoundError:
    from single_worker_run_state_helpers import CAPABILITIES, FakeClient, UNSUPPORTED_CAPABILITIES


class SingleWorkerRunStateServiceTest(unittest.TestCase):
    def test_start_unsupported_blocking_execution_is_not_retryable(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                retry_limit=1,
                retryable_failures=["api"],
            )
            client = FakeClient()
            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: UNSUPPORTED_CAPABILITIES,
                executor=lambda *args, **kwargs: self.fail("unsupported server should not execute worker"),
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

        self.assertEqual(outcome.exit_code, 70)
        self.assertIn("unsupported route behavior", outcome.error)
        worker = run["workers"]["worker"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["failure_category"], "api")
        self.assertEqual(worker["retryable_failures"], ["api"])
        self.assertEqual(worker["next_eligible_action"], "none")

    def test_start_api_setup_failure_is_not_retryable(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                retry_limit=1,
                retryable_failures=["api"],
            )
            client = FakeClient()

            def detect_capabilities(client):
                raise OpenCodeApiError("capability probe failed")

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=detect_capabilities,
                executor=lambda *args, **kwargs: self.fail("failed setup should not execute worker"),
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

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "api failure: capability probe failed")
        worker = run["workers"]["worker"]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["failure_category"], "api")
        self.assertEqual(worker["retryable_failures"], ["api"])
        self.assertEqual(worker["next_eligible_action"], "none")

    def test_start_success_persists_worker_result_and_run_summary(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            client = FakeClient()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt, capabilities["legacy_fallback_available"]))
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
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNone(outcome.error)
        self.assertEqual(client.requests, [("create", directory, None, None), ("execute", "ses_new", "Finish the worker task", True)])
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["output_refs"], ["worker:msg_assistant_1"])
        worker = run["workers"]["worker"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["role"], "worker")
        self.assertEqual(worker["prompt"], "Finish the worker task")
        self.assertEqual(worker["prompt_ids"], ["msg_user_1"])
        self.assertEqual(worker["output_refs"], ["assistant:msg_assistant_1"])
        self.assertEqual(worker["next_eligible_action"], "collect")
        self.assertEqual(worker["result"]["text"], "Worker finished.")

    def test_start_treats_empty_stored_session_id_as_missing(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker("demo", "worker", role="worker", session_id="")
            client = FakeClient(session_ids=["ses_created"])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt, capabilities["legacy_fallback_available"]))
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
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNone(outcome.error)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_created", "Finish the worker task", True),
            ],
        )
        worker = run["workers"]["worker"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["session_id"], "ses_created")
        self.assertEqual(worker["result"]["session_id"], "ses_created")

    def test_start_rejects_create_response_without_session_id_before_execution(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            client = FakeClient(session_ids=[None])

            def execute_prompt(client, session_id, prompt, capabilities):
                self.fail(f"worker executed with malformed session id {session_id!r}")

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
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(
            outcome.error,
            "api failure: session creation returned malformed response: missing session id",
        )
        self.assertEqual(client.requests, [("create", directory, None, None)])
        self.assertEqual(run["status"], "failed")
        worker = run["workers"]["worker"]
        self.assertEqual(worker["status"], "failed")
        self.assertIsNone(worker["session_id"])
        self.assertEqual(worker["failure_category"], "api")
        self.assertEqual(worker["failure_reason"], "session creation returned malformed response: missing session id")
        self.assertEqual(worker["next_eligible_action"], "none")
        self.assertNotIn("result", worker)

    def test_start_records_terminal_provider_failure_without_result(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            client = FakeClient()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                raise BlockingProviderFailure("provider rejected request", prompt_id="msg_user_1")

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
                )
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, "provider failure: provider rejected request")
        self.assertEqual(run["status"], "failed")
        worker = run["workers"]["worker"]
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["prompt_ids"], ["msg_user_1"])
        self.assertEqual(worker["output_refs"], [])
        self.assertEqual(worker["error"], "provider rejected request")
        self.assertEqual(worker["failure_category"], "provider")
        self.assertEqual(worker["failure_reason"], "provider rejected request")
        self.assertEqual(worker["last_failure_category"], "provider")
        self.assertEqual(worker["last_failure_reason"], "provider rejected request")
        self.assertEqual(worker["next_eligible_action"], "none")
        self.assertNotIn("result", worker)

if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.multi_worker_orchestration import DependencyOrderedSerialRunOrchestrationService
from opencode_session.run_store import RunStore

try:
    from tests.single_worker_run_state_helpers import CAPABILITIES, FakeClient, start_single_worker_run
except ModuleNotFoundError:
    from single_worker_run_state_helpers import CAPABILITIES, FakeClient, start_single_worker_run


class SingleWorkerRunStateRetryTest(unittest.TestCase):
    def test_start_retries_retryable_provider_failure_and_persists_success_metadata(self):
        with tempfile.TemporaryDirectory() as store_root, tempfile.TemporaryDirectory() as directory:
            store = RunStore(store_root)
            store.create_run("demo", directory=directory, server_url="http://opencode.example")
            store.upsert_worker(
                "demo",
                "worker",
                role="worker",
                retry_limit=1,
                retryable_failures=["provider"],
            )
            client = FakeClient()
            attempts = [
                BlockingProviderFailure("transient provider outage", prompt_id="msg_user_failed"),
                {
                    "session_id": "ses_new",
                    "message_ids": {"user": "msg_user_retry", "assistant": "msg_assistant_1"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": "Worker finished after retry.",
                },
            ]

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                attempt = attempts.pop(0)
                if isinstance(attempt, Exception):
                    raise attempt
                return attempt

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = start_single_worker_run(
                store,
                service,
                name="demo",
                worker_id="worker",
                role="worker",
                prompt="Finish the worker task",
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNone(outcome.error)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_new", "Finish the worker task"),
                ("execute", "ses_new", "Finish the worker task"),
            ],
        )
        self.assertEqual(run["status"], "done")
        retry_worker = run["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["provider"])
        self.assertEqual(retry_worker["last_failure_category"], "provider")
        self.assertEqual(retry_worker["last_failure_reason"], "transient provider outage")
        self.assertIsNone(retry_worker["failure_reason"])
        self.assertEqual(retry_worker["prompt_ids"], ["msg_user_retry"])
        self.assertEqual(retry_worker["next_eligible_action"], "collect")
        self.assertEqual(retry_worker["result"]["message_ids"], {"user": "msg_user_retry", "assistant": "msg_assistant_1"})

    def test_start_retries_retryable_api_failure_and_persists_success_metadata(self):
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
            attempts = [
                OpenCodeApiError("HTTP 503 POST /session/ses_new/run: upstream overloaded", status=503),
                {
                    "session_id": "ses_new",
                    "message_ids": {"user": "msg_user_retry", "assistant": "msg_assistant_1"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": "Worker finished after API retry.",
                },
            ]

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                attempt = attempts.pop(0)
                if isinstance(attempt, Exception):
                    raise attempt
                return attempt

            service = DependencyOrderedSerialRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = start_single_worker_run(
                store,
                service,
                name="demo",
                worker_id="worker",
                role="worker",
                prompt="Finish the worker task",
            )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 0)
        self.assertIsNone(outcome.error)
        retry_worker = run["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["api"])
        self.assertEqual(retry_worker["last_failure_category"], "api")
        self.assertIn("HTTP 503", retry_worker["last_failure_reason"])
        self.assertIsNone(retry_worker["failure_reason"])
        self.assertEqual(retry_worker["next_eligible_action"], "collect")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.run_state import SingleWorkerRunStartRequest, SingleWorkerRunStateService, WorkerExecutionTimeout
from opencode_session.run_store import RunStore


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
    def __init__(self):
        self.timeout = 3
        self.requests = []

    def create_session_response(self, directory, *, agent=None, model=None):
        self.requests.append(("create", directory, agent, model))
        return FakeResponse({"id": "ses_new", "directory": directory})

    def delete_session(self, session_id):
        self.requests.append(("delete", session_id))


class SingleWorkerRunStateServiceTest(unittest.TestCase):
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
        retry_worker = run["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["api"])
        self.assertEqual(retry_worker["last_failure_category"], "api")
        self.assertIn("HTTP 503", retry_worker["last_failure_reason"])
        self.assertIsNone(retry_worker["failure_reason"])
        self.assertEqual(retry_worker["next_eligible_action"], "collect")

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
            ],
        )
        self.assertEqual(run["workers"]["worker"]["cleanup"], {"requested": True, "deleted": True})


if __name__ == "__main__":
    unittest.main()

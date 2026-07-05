import tempfile
import threading
import unittest
from unittest import mock

from opencode_session.run_state import SingleWorkerRunStartRequest, SingleWorkerRunStateService, WorkerExecutionTimeout
from opencode_session.run_store import RunStore
from opencode_session.timeout_boundary import TimeoutExpired

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

    def test_start_retries_timeout_in_new_session_while_original_call_is_in_flight(self):
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
            release_late_call = threading.Event()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                if session_id == "ses_initial":
                    release_late_call.wait(1)
                    return {
                        "session_id": session_id,
                        "message_ids": {"user": "msg_user_late", "assistant": "msg_assistant_late"},
                        "status": "done",
                        "raw_status": "completed",
                        "terminal_state": "done",
                        "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                        "execution_strategy": "legacy_run_reply",
                        "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                        "cost": 0.015,
                        "tokens": {"total": 20},
                        "text": "Worker finished too late.",
                    }
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_user_retry", "assistant": "msg_assistant_1"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": "Worker finished after isolated retry.",
                }

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            try:
                outcome = service.start(
                    SingleWorkerRunStartRequest(
                        name="demo",
                        worker_id="worker",
                        role="worker",
                        prompt="Finish the worker task",
                    )
                )
                run = store.load_run("demo")
            finally:
                release_late_call.set()

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
                ("create", directory, None, None),
                ("execute", "ses_retry", "Finish the worker task"),
            ],
        )
        retry_worker = run["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["session_id"], "ses_retry")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["last_failure_category"], "timeout")
        self.assertEqual(retry_worker["last_failure_reason"], "worker timed out after 0.05s")
        self.assertEqual(retry_worker["result"]["session_id"], "ses_retry")
        self.assertEqual(
            retry_worker["timeout_retry_sessions"],
            [
                {
                    "timed_out_session_id": "ses_initial",
                    "retry_session_id": "ses_retry",
                    "reason": "worker timed out after 0.05s",
                    "created_at": "2026-07-03T00:00:00Z",
                }
            ],
        )

    def test_timeout_retry_abandoned_callback_keeps_original_session_after_rebind(self):
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
            release_abandoned_callback = threading.Event()
            abandoned_threads = []
            deadline_calls = []

            class DelayedFirstTimeoutDeadline:
                def __init__(self, timeout):
                    self.timeout = timeout

                def run(self, callback):
                    deadline_calls.append(self.timeout)
                    if len(deadline_calls) == 1:
                        thread = threading.Thread(target=lambda: (release_abandoned_callback.wait(1), callback()))
                        thread.start()
                        abandoned_threads.append(thread)
                        raise TimeoutExpired()
                    return callback()

            def result_for(session_id):
                return {
                    "session_id": session_id,
                    "message_ids": {"user": f"msg_user_{session_id}", "assistant": f"msg_assistant_{session_id}"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": f"Worker finished in {session_id}.",
                }

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                return result_for(session_id)

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            try:
                with mock.patch("opencode_session.worker_execution.TimeoutDeadline", DelayedFirstTimeoutDeadline):
                    outcome = service.start(
                        SingleWorkerRunStartRequest(
                            name="demo",
                            worker_id="worker",
                            role="worker",
                            prompt="Finish the worker task",
                        )
                    )
                run = store.load_run("demo")
            finally:
                release_abandoned_callback.set()
                for thread in abandoned_threads:
                    thread.join(1)

        self.assertFalse(any(thread.is_alive() for thread in abandoned_threads))
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("create", directory, None, None),
                ("execute", "ses_retry", "Finish the worker task"),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        retry_worker = run["workers"]["worker"]
        self.assertEqual(retry_worker["session_id"], "ses_retry")
        self.assertEqual(retry_worker["result"]["session_id"], "ses_retry")

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

    def test_timeout_boundary_does_not_require_main_thread_signal_handling(self):
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
            release = threading.Event()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                release.wait(1)
                return {
                    "session_id": session_id,
                    "message_ids": {"user": "msg_user_late", "assistant": "msg_assistant_late"},
                    "status": "done",
                    "raw_status": "completed",
                    "terminal_state": "done",
                    "api_path": {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
                    "execution_strategy": "legacy_run_reply",
                    "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": "Worker finished too late.",
                }

            service = SingleWorkerRunStateService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )
            result = {}

            def start_service():
                try:
                    result["outcome"] = service.start(
                        SingleWorkerRunStartRequest(
                            name="demo",
                            worker_id="worker",
                            role="worker",
                            prompt="Finish the worker task",
                        )
                    )
                except BaseException as error:
                    result["error"] = error

            thread = threading.Thread(target=start_service)
            thread.start()
            thread.join(timeout=1)
            release.set()
            thread.join(timeout=1)
            run = store.load_run("demo")

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(result["outcome"].exit_code, 124)
        self.assertEqual(result["outcome"].error, "worker timed out after 0.05s")
        self.assertEqual(run["status"], "timeout")
        self.assertEqual(run["workers"]["worker"]["failure_category"], "timeout")


if __name__ == "__main__":
    unittest.main()

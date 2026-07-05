import tempfile
import threading
import unittest
from unittest import mock

from opencode_session.api_client import OpenCodeApiError
from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.run_state import SingleWorkerRunStartRequest, SingleWorkerRunStateService, WorkerExecutionTimeout
from opencode_session.run_store import RunStore
from opencode_session.timeout_boundary import TimeoutExpired


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

UNSUPPORTED_CAPABILITIES = {
    "route_availability": {
        "blocking_message": {"path": "/session/{sessionID}/message", "method": "POST", "available": False},
        "legacy_run": {"path": "/session/{sessionID}/run", "method": "POST", "available": False},
        "legacy_reply": {"path": "/session/{sessionID}/reply", "method": "POST", "available": False},
    },
    "blocking_message_available": False,
    "blocking_execution_available": False,
    "legacy_fallback_available": False,
}


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeClient:
    def __init__(self, session_ids=None):
        self.timeout = 3
        self.requests = []
        self.session_ids = list(session_ids or ["ses_new"])

    def create_session_response(self, directory, *, agent=None, model=None):
        self.requests.append(("create", directory, agent, model))
        return FakeResponse({"id": self.session_ids.pop(0), "directory": directory})

    def delete_session(self, session_id):
        self.requests.append(("delete", session_id))


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

    def test_start_with_cleanup_deletes_initial_and_timeout_retry_sessions(self):
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
            deadline_calls = []

            class FirstAttemptTimeoutDeadline:
                def __init__(self, timeout):
                    self.timeout = timeout

                def run(self, callback):
                    deadline_calls.append(self.timeout)
                    if len(deadline_calls) == 1:
                        raise TimeoutExpired()
                    return callback()

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
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

            with mock.patch("opencode_session.worker_execution.TimeoutDeadline", FirstAttemptTimeoutDeadline):
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

        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("create", directory, None, None),
                ("execute", "ses_retry", "Finish the worker task"),
                ("delete", "ses_initial"),
                ("delete", "ses_retry"),
            ],
        )
        self.assertEqual(
            run["workers"]["worker"]["cleanup"],
            {"requested": True, "deleted": True, "sessions": ["ses_initial", "ses_retry"]},
        )

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
                    ],
                )
                self.assertEqual(run["workers"]["worker"]["cleanup"], {"requested": True, "deleted": True})

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

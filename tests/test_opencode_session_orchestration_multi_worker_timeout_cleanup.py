import tempfile
import threading
import unittest
from unittest import mock

from opencode_session.blocking_execution import BlockingProviderFailure
from opencode_session.multi_worker_orchestration import MultiWorkerRunOrchestrationService, MultiWorkerRunStartRequest
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            outcome = service.start(
                MultiWorkerRunStartRequest(name="demo", worker_id="alpha", role="build", cleanup=True)
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
        saves = []
        core = RunStartCore(
            save_run=lambda run: saves.append(run),
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
        self.assertEqual(client.requests, [("delete", "ses_alpha"), ("delete", "ses_beta")])
        self.assertEqual(
            run["workers"]["alpha"]["cleanup"],
            {"requested": True, "deleted": False, "error": first_error},
        )
        self.assertEqual(run["workers"]["alpha"]["status"], "failed")
        self.assertEqual(run["workers"]["alpha"]["failure_reason"], first_error)
        self.assertEqual(run["workers"]["beta"]["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(saves, [run])

    def test_timeout_retry_abandoned_callback_keeps_original_session_after_worker_rebind(self):
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            try:
                with mock.patch("opencode_session.worker_execution.TimeoutDeadline", DelayedFirstTimeoutDeadline):
                    outcome = service.start(MultiWorkerRunStartRequest(name="demo", worker_id="worker", role="worker"))
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

    def test_cleanup_deletes_initial_and_timeout_retry_sessions_for_created_worker(self):
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            with mock.patch("opencode_session.worker_execution.TimeoutDeadline", FirstAttemptTimeoutDeadline):
                outcome = service.start(
                    MultiWorkerRunStartRequest(name="demo", worker_id="worker", role="worker", cleanup=True)
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

    def test_cleanup_attempts_timeout_retry_session_after_initial_delete_fails(self):
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

            service = MultiWorkerRunOrchestrationService(
                store,
                client_factory=lambda url: client,
                capability_detector=lambda client: CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

            with mock.patch("opencode_session.worker_execution.TimeoutDeadline", FirstAttemptTimeoutDeadline):
                outcome = service.start(
                    MultiWorkerRunStartRequest(name="demo", worker_id="worker", role="worker", cleanup=True)
                )
            run = store.load_run("demo")

        self.assertEqual(outcome.exit_code, 69)
        self.assertEqual(outcome.error, f"api failure: disposable session cleanup failed: {first_error}")
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
        worker = run["workers"]["worker"]
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["failure_reason"], first_error)
        self.assertEqual(
            worker["cleanup"],
            {"requested": True, "deleted": False, "error": first_error, "sessions": ["ses_retry"]},
        )


if __name__ == "__main__":
    unittest.main()

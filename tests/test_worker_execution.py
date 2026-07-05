import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiError
from opencode_session.worker_execution import WorkerExecutionTimeout, execute_worker_attempts
from opencode_session.worker_state import ensure_worker


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
    def __init__(self, session_ids):
        self.requests = []
        self.session_ids = list(session_ids)

    def create_session_response(self, directory, *, agent=None, model=None):
        self.requests.append(("create", directory, agent, model))
        return FakeResponse({"id": self.session_ids.pop(0), "directory": directory})


class WorkerExecutionTest(unittest.TestCase):
    def test_execute_worker_attempts_rejects_create_response_without_session_id_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            client = FakeClient([None])

            def execute_prompt(client, session_id, prompt, capabilities):
                self.fail(f"worker executed with malformed session id {session_id!r}")

            with self.assertRaisesRegex(
                OpenCodeApiError,
                "session creation returned malformed response: missing session id",
            ):
                execute_worker_attempts(
                    client,
                    run,
                    worker,
                    "Finish the worker task",
                    CAPABILITIES,
                    executor=execute_prompt,
                    now=lambda: "2026-07-03T00:00:00Z",
                    agent="build",
                    model="openai/gpt-5.5",
                )

        self.assertEqual(client.requests, [("create", directory, "build", "openai/gpt-5.5")])
        self.assertIsNone(worker["session_id"])
        self.assertIsNone(worker["agent"])
        self.assertIsNone(worker["model"])
        self.assertNotIn("result", worker)

    def test_execute_worker_attempts_rejects_timeout_retry_create_response_without_session_id(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker["timeout_seconds"] = 0.05
            worker["retry_limit"] = 1
            worker["retryable_failures"] = ["timeout"]
            client = FakeClient(["ses_initial", None])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt))
                if session_id is None:
                    self.fail("worker executed timeout retry with malformed session id")
                raise WorkerExecutionTimeout()

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
            )

        self.assertEqual(outcome.kind, "failed")
        self.assertEqual(outcome.failure_category, "api")
        self.assertEqual(
            outcome.error,
            "api failure: timeout retry session creation failed: "
            "timeout retry session creation returned malformed response: missing session id",
        )
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
                ("create", directory, None, None),
            ],
        )
        self.assertEqual(worker["session_id"], "ses_initial")
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(
            worker["failure_reason"],
            "timeout retry session creation failed: "
            "timeout retry session creation returned malformed response: missing session id",
        )
        self.assertNotIn("timeout_retry_sessions", worker)
        self.assertNotIn("result", worker)

    def test_execute_worker_attempts_retries_timeout_in_isolated_session_and_applies_result(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker["timeout_seconds"] = 0.05
            worker["retry_limit"] = 1
            worker["retryable_failures"] = ["timeout"]
            client = FakeClient(["ses_initial", "ses_retry"])

            def execute_prompt(client, session_id, prompt, capabilities):
                client.requests.append(("execute", session_id, prompt, capabilities["legacy_fallback_available"]))
                if session_id == "ses_initial":
                    raise WorkerExecutionTimeout()
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

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
                agent="build",
                model="openai/gpt-5.5",
            )

        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.created_session_ids, ["ses_initial", "ses_retry"])
        self.assertEqual(
            client.requests,
            [
                ("create", directory, "build", "openai/gpt-5.5"),
                ("execute", "ses_initial", "Finish the worker task", True),
                ("create", directory, "build", "openai/gpt-5.5"),
                ("execute", "ses_retry", "Finish the worker task", True),
            ],
        )
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["session_id"], "ses_retry")
        self.assertEqual(worker["retry_count"], 1)
        self.assertEqual(worker["last_failure_category"], "timeout")
        self.assertEqual(worker["last_failure_reason"], "worker timed out after 0.05s")
        self.assertEqual(worker["prompt_ids"], ["msg_user_retry"])
        self.assertEqual(worker["output_refs"], ["assistant:msg_assistant_1"])
        self.assertEqual(worker["next_eligible_action"], "collect")
        self.assertEqual(worker["result"]["session_id"], "ses_retry")
        self.assertEqual(
            worker["timeout_retry_sessions"],
            [
                {
                    "timed_out_session_id": "ses_initial",
                    "retry_session_id": "ses_retry",
                    "reason": "worker timed out after 0.05s",
                    "created_at": "2026-07-03T00:00:00Z",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest

from opencode_session.api_client import OpenCodeApiError
from opencode_session.worker_execution import (
    WorkerExecutionTimeout,
    cleanup_created_worker_sessions,
    execute_worker_attempts,
)
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
    def __init__(self, session_ids, *, delete_failures=None):
        self.requests = []
        self.session_ids = list(session_ids)
        self.delete_failures = dict(delete_failures or {})

    def create_session_response(self, directory, *, agent=None, model=None):
        self.requests.append(("create", directory, agent, model))
        return FakeResponse({"id": self.session_ids.pop(0), "directory": directory})

    def delete_session_response(self, session_id):
        self.requests.append(("delete", session_id))
        if session_id in self.delete_failures:
            raise self.delete_failures[session_id]

    def delete_session(self, session_id):
        response = self.delete_session_response(session_id)
        return response.data if response is not None else None

    def get_session(self, session_id):
        self.requests.append(("get", session_id))
        raise OpenCodeApiError(f"session not found: {session_id}", status=404)


class WorkerExecutionTest(unittest.TestCase):
    def test_cleanup_created_worker_sessions_clears_stale_sessions_after_single_session_success(self):
        worker = {
            "cleanup": {
                "requested": True,
                "deleted": False,
                "error": "DELETE /api/session/ses_old failed: HTTP 500",
                "sessions": ["ses_old", "ses_retry"],
            }
        }
        client = FakeClient([])

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_new"])

        self.assertEqual(client.requests, [("delete", "ses_new"), ("get", "ses_new")])
        self.assertEqual(outcome.deleted_session_ids, ["ses_new"])
        self.assertIsNone(outcome.error)
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": True})

    def test_cleanup_created_worker_sessions_treats_missing_session_as_deleted(self):
        worker = {}
        client = FakeClient(
            [],
            delete_failures={"ses_missing": OpenCodeApiError("session not found", status=404)},
        )

        outcome = cleanup_created_worker_sessions(client, worker, ["ses_missing"])

        self.assertEqual(client.requests, [("delete", "ses_missing"), ("get", "ses_missing")])
        self.assertEqual(outcome.deleted_session_ids, ["ses_missing"])
        self.assertIsNone(outcome.error)
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": True})

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

    def test_execute_worker_attempts_skips_automatic_timeout_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker["timeout_seconds"] = 0.05
            worker["retry_limit"] = 1
            worker["retryable_failures"] = ["timeout"]
            client = FakeClient(["ses_initial", "ses_unused"])

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
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

        self.assertEqual(outcome.kind, "terminal_failure")
        self.assertEqual(outcome.failure_category, "timeout")
        self.assertIn("automatic timeout retry skipped", outcome.error)
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        self.assertEqual(worker["session_id"], "ses_initial")
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["retry_count"], 0)
        self.assertTrue(worker["manual_retry_required"])
        self.assertEqual(worker["next_eligible_action"], "retry")
        self.assertEqual(worker["failure_reason"], "worker timed out after 0.05s")
        self.assertNotIn("timeout_retry_sessions", worker)
        self.assertNotIn("result", worker)

    def test_execute_worker_attempts_does_not_start_retry_session_after_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker["timeout_seconds"] = 0.05
            worker["retry_limit"] = 1
            worker["retryable_failures"] = ["timeout"]
            client = FakeClient(["ses_initial", "ses_retry"])

            def execute_prompt(client, session_id, prompt, capabilities, *, timeout=None):
                client.requests.append(("execute", session_id, prompt, capabilities["legacy_fallback_available"]))
                self.assertLessEqual(timeout, 0.05)
                raise WorkerExecutionTimeout()

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

        self.assertEqual(outcome.failure_category, "timeout")
        self.assertIn("automatic timeout retry skipped", outcome.error)
        self.assertEqual(outcome.created_session_ids, ["ses_initial"])
        self.assertEqual(
            client.requests,
            [
                ("create", directory, "build", "openai/gpt-5.5"),
                ("execute", "ses_initial", "Finish the worker task", True),
            ],
        )
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["session_id"], "ses_initial")
        self.assertEqual(worker["retry_count"], 0)
        self.assertEqual(worker["last_failure_category"], "timeout")
        self.assertEqual(worker["last_failure_reason"], "worker timed out after 0.05s")
        self.assertEqual(worker["next_eligible_action"], "retry")
        self.assertTrue(worker["manual_retry_required"])
        self.assertNotIn("result", worker)
        self.assertNotIn("timeout_retry_sessions", worker)

    def test_execute_worker_attempts_does_not_schedule_timeout_retry_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"directory": directory, "workers": {}}
            worker = ensure_worker(run, "worker", role="worker")
            worker["timeout_seconds"] = 0.05
            worker["retry_limit"] = 1
            worker["retryable_failures"] = ["timeout"]
            client = FakeClient(["ses_initial", "ses_retry"])

            def execute_prompt(client, session_id, prompt, capabilities, *, deadline=None):
                client.requests.append(("execute", session_id, prompt))
                self.assertIsNotNone(deadline)
                raise WorkerExecutionTimeout()

            outcome = execute_worker_attempts(
                client,
                run,
                worker,
                "Finish the worker task",
                CAPABILITIES,
                executor=execute_prompt,
                now=lambda: "2026-07-03T00:00:00Z",
                stop_after_retry=True,
            )

        self.assertEqual(outcome.kind, "terminal_failure")
        self.assertIn("automatic timeout retry skipped", outcome.error)
        self.assertEqual(outcome.failure_category, "timeout")
        self.assertEqual(outcome.created_session_ids, ["ses_initial"])
        self.assertEqual(
            client.requests,
            [
                ("create", directory, None, None),
                ("execute", "ses_initial", "Finish the worker task"),
            ],
        )
        self.assertEqual(worker["status"], "timeout")
        self.assertEqual(worker["next_eligible_action"], "retry")
        self.assertEqual(worker["session_id"], "ses_initial")


if __name__ == "__main__":
    unittest.main()

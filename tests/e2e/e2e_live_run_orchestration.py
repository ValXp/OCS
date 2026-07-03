import json
import os
import tempfile
import unittest
import uuid

from harness import (
    AGENT_ENV,
    MODEL_ENV,
    assert_success,
    cleanup_session_fixture,
    format_completed_process,
    live_validate_selection_args,
    load_json,
    require_live_server_url,
    run_ocs,
)


PROMPT = "Reply exactly PONG."


class LiveRunBlockingAndOrchestrationE2ETest(unittest.TestCase):
    def test_run_blocking_json_replies_pong_and_cleans_disposable_session(self):
        server_url = require_live_server_url(self)
        marker = f"ocs-e2e-live-run-{uuid.uuid4().hex[:12]}"

        with tempfile.TemporaryDirectory(prefix=f"{marker}-target-") as directory:
            result = run_ocs(
                "run_blocking",
                "--directory",
                directory,
                "--json",
                *live_validate_selection_args(),
                "--server",
                server_url,
                PROMPT,
            )

        assert_success(self, result)
        payload = load_json(self, result, "run_blocking --json")
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.addCleanup(cleanup_session_fixture, self, server_url, session_id, label="live run_blocking session")
        context = self._context(payload)
        self._assert_terminal_pong_result(payload, context)

        inspect_result = run_ocs("inspect", session_id, "--json", "--server", server_url)
        self.assertNotEqual(inspect_result.returncode, 0, format_completed_process(inspect_result))

    def test_run_start_cleanup_persists_status_and_collects_pong_in_later_subprocesses(self):
        server_url = require_live_server_url(self)
        marker = f"ocs-e2e-live-run-store-{uuid.uuid4().hex[:12]}"
        run_name = f"live-{uuid.uuid4().hex[:12]}"
        worker_session_id = None

        with tempfile.TemporaryDirectory(prefix=f"{marker}-store-") as store, tempfile.TemporaryDirectory(
            prefix=f"{marker}-target-"
        ) as directory:
            try:
                start_result = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    run_name,
                    "--directory",
                    directory,
                    "--server",
                    server_url,
                    "--prompt",
                    PROMPT,
                    "--cleanup",
                    *live_validate_selection_args(),
                )
                status_result = run_ocs("run", "--store", store, "status", run_name, "--json")
                status_for_cleanup = self._maybe_json(status_result)
                worker_session_id = self._worker_session_id(status_for_cleanup)
                collect_result = run_ocs("run", "--store", store, "collect", run_name, "--json")

                assert_success(self, start_result)
                assert_success(self, status_result)
                status_payload = load_json(self, status_result, "run status --json")
                status_context = self._context(status_payload)
                self.assertEqual(status_payload.get("status"), "done", status_context)
                self.assertEqual(status_payload.get("directory"), directory, status_context)

                workers = status_payload.get("workers")
                self.assertIsInstance(workers, dict, status_context)
                worker = workers.get("worker")
                self.assertIsInstance(worker, dict, status_context)
                self.assertEqual(worker.get("status"), "done", status_context)
                self.assertEqual(worker.get("prompt"), PROMPT, status_context)
                self.assertEqual(worker.get("cleanup"), {"requested": True, "deleted": True}, status_context)
                self.assertEqual(worker.get("next_eligible_action"), "collect", status_context)
                self._assert_live_selection_recorded(worker, status_context)

                worker_session_id = worker.get("session_id")
                self.assert_nonempty_string(worker_session_id, "worker session_id", status_context)

                result_payload = worker.get("result")
                self._assert_terminal_pong_result(result_payload, status_context)
                self.assertEqual(result_payload.get("session_id"), worker_session_id, status_context)
                user_message_id = result_payload["message_ids"]["user"]
                assistant_message_id = result_payload["message_ids"]["assistant"]
                self.assertEqual(worker.get("prompt_ids"), [user_message_id], status_context)
                self.assertEqual(worker.get("output_refs"), [f"assistant:{assistant_message_id}"], status_context)
                self.assertEqual(status_payload.get("output_refs"), [f"worker:{assistant_message_id}"], status_context)

                assert_success(self, collect_result)
                collect_payload = load_json(self, collect_result, "run collect --json")
                collect_context = self._context(collect_payload)
                self.assertEqual(collect_payload, result_payload, collect_context)
                self._assert_terminal_pong_result(collect_payload, collect_context)
                self.assertEqual(collect_payload.get("session_id"), worker_session_id, collect_context)

                inspect_result = run_ocs("inspect", worker_session_id, "--json", "--server", server_url)
                self.assertNotEqual(inspect_result.returncode, 0, format_completed_process(inspect_result))
            finally:
                if worker_session_id:
                    cleanup_session_fixture(self, server_url, worker_session_id, label="live orchestration worker session")

    def _assert_terminal_pong_result(self, payload, context):
        self.assertIsInstance(payload, dict, context)
        self.assert_nonempty_string(payload.get("session_id"), "session_id", context)
        self.assertEqual(payload.get("status"), "done", context)
        self.assertEqual(payload.get("terminal_state"), "done", context)
        self.assertTrue((payload.get("fallback") or {}).get("used"), context)
        self.assertEqual(str(payload.get("text", "")).strip(), "PONG", context)

        message_ids = payload.get("message_ids")
        self.assertIsInstance(message_ids, dict, context)
        self.assert_nonempty_string(message_ids.get("user"), "user message id", context)
        self.assert_nonempty_string(message_ids.get("assistant"), "assistant message id", context)

    def _assert_live_selection_recorded(self, worker, context):
        expected_agent = os.environ.get(AGENT_ENV)
        expected_model = os.environ.get(MODEL_ENV)
        if expected_agent:
            self.assertEqual(worker.get("agent"), expected_agent, context)
        if expected_model:
            self.assertEqual(worker.get("model"), expected_model, context)

    def assert_nonempty_string(self, value, label, context):
        self.assertIsInstance(value, str, f"{label} was not a string\n{context}")
        self.assertNotEqual(value, "", f"{label} was empty\n{context}")

    def _maybe_json(self, result):
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def _worker_session_id(self, status_payload):
        if not isinstance(status_payload, dict):
            return None
        worker = (status_payload.get("workers") or {}).get("worker")
        if not isinstance(worker, dict):
            return None
        session_id = worker.get("session_id")
        return session_id if isinstance(session_id, str) and session_id else None

    def _context(self, payload):
        return json.dumps(payload, indent=2, sort_keys=True)


if __name__ == "__main__":
    unittest.main()

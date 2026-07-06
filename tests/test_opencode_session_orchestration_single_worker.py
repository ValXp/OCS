import unittest
import tempfile

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from tests.orchestration_cli_harness import (
        assert_worker_session_create_payload,
        configure_single_worker_server,
        payloads_for,
        request_paths,
    )
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from orchestration_cli_harness import (
        assert_worker_session_create_payload,
        configure_single_worker_server,
        payloads_for,
        request_paths,
    )


class SingleWorkerOrchestrationCliTest(unittest.TestCase):
    def test_start_named_run_creates_session_and_persists_success(self):
        events = [
            {
                "type": "session.prompt.admitted",
                "properties": {
                    "sessionID": "ses_new",
                    "messageID": "msg_user_1",
                    "delivery": "run",
                    "state": "admitted",
                },
            },
            {"type": "session.status", "properties": {"sessionID": "ses_new", "status": "completed"}},
        ]

        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(server, events=events)
                result = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(result.stderr, "")
        self.assertIn("run=demo status=done", result.stdout)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "done")
        self.assertNotIn("transcript", payload)
        worker = payload["workers"]["worker"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["role"], "worker")
        self.assertEqual(worker["prompt"], "Finish the worker task")
        self.assertEqual(worker["prompt_ids"], ["msg_user_1"])
        self.assertEqual(worker["output_refs"], ["assistant:msg_assistant_1"])
        self.assertEqual(
            worker["result"],
            {
                "session_id": "ses_new",
                "message_ids": {"user": "msg_user_1", "assistant": "msg_assistant_1"},
                "status": "done",
                "raw_status": "completed",
                "terminal_state": "done",
                "api_path": {
                    "run": "/session/{sessionID}/run",
                    "reply": "/session/{sessionID}/reply",
                },
                "execution_strategy": "legacy_run_reply",
                "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                "cost": 0.015,
                "tokens": {"input": 12, "output": 8, "total": 20},
                "text": "Worker finished.",
            },
        )
        self.assertEqual(payload["output_refs"], ["worker:msg_assistant_1"])
        paths = request_paths(requests)
        self.assertIn(("GET", "/global/health"), paths)
        self.assertIn(("GET", "/doc"), paths)
        self.assertLess(paths.index(("POST", "/api/session")), paths.index(("POST", "/session/ses_new/run")))
        self.assertLess(paths.index(("POST", "/session/ses_new/run")), paths.index(("POST", "/session/ses_new/reply")))
        session_payloads = payloads_for(requests, "POST", "/api/session")
        self.assertEqual(len(session_payloads), 1)
        assert_worker_session_create_payload(
            self,
            session_payloads[0],
            directory=directory,
            worker_id="worker",
        )
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_new/run"), [{"message": "Finish the worker task"}])

    def test_start_named_run_uses_modern_session_message_when_available(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(server, modern_message=True)
                result = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        worker = payload["workers"]["worker"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["prompt_ids"], [worker["result"]["message_ids"]["user"]])
        self.assertTrue(worker["prompt_ids"][0].startswith("msg_"))
        self.assertEqual(worker["output_refs"], ["assistant:msg_assistant_modern_1"])
        self.assertEqual(worker["result"]["execution_strategy"], "session_message")
        self.assertEqual(worker["result"]["text"], "Modern worker finished.")
        self.assertEqual(payload["output_refs"], ["worker:msg_assistant_modern_1"])
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_new/run"), [])
        message_payloads = payloads_for(requests, "POST", "/session/ses_new/message")
        self.assertEqual(len(message_payloads), 1)
        self.assertEqual(message_payloads[0]["parts"], [{"type": "text", "text": "Finish the worker task"}])
        self.assertTrue(message_payloads[0]["messageID"].startswith("msg_"))

    def test_start_stored_prompt_applies_agent_and_model_arguments(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(server)
                init = run_ocs(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = run_ocs(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "worker",
                    "--role",
                    "worker",
                    "--prompt",
                    "Finish the worker task",
                )
                start = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--agent",
                    "build",
                    "--model",
                    "openai/gpt-5.5",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        session_payloads = payloads_for(requests, "POST", "/api/session")
        self.assertEqual(len(session_payloads), 1)
        assert_worker_session_create_payload(
            self,
            session_payloads[0],
            directory=directory,
            worker_id="worker",
            agent="build",
            model="openai/gpt-5.5",
        )
        payload = load_json(self, status, "status")
        self.assertEqual(payload["workers"]["worker"]["agent"], "build")
        self.assertEqual(payload["workers"]["worker"]["model"], "openai/gpt-5.5")

    def test_start_prompt_returns_aborted_exit_code_when_worker_result_is_aborted(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(
                    server,
                    reply_payload={"id": "msg_abort", "status": "aborted", "error": "user aborted run"},
                )
                start = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--prompt",
                    "Finish the worker task",
                )
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(start.returncode, 130)
        self.assertEqual(start.stderr, "")
        self.assertIn("run=demo status=aborted", start.stdout)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "aborted")
        self.assertEqual(payload["workers"]["worker"]["status"], "aborted")
        self.assertEqual(payload["workers"]["worker"]["lifecycle_state"], "aborted")
        self.assertEqual(payload["workers"]["worker"]["result"]["terminal_state"], "aborted")
        self.assertEqual(payload["workers"]["worker"]["next_eligible_action"], "none")

    def test_start_persists_failed_state_and_prompt_reference_on_provider_failure(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(
                    server,
                    run_payload={"id": "msg_user_1", "status": "failed", "error": "provider rejected request"},
                )
                result = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("provider failure", result.stderr)
        self.assertIn("provider rejected request", result.stderr)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "failed")
        worker = payload["workers"]["worker"]
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["prompt_ids"], ["msg_user_1"])
        self.assertEqual(worker["output_refs"], [])
        self.assertEqual(worker["error"], "provider rejected request")
        self.assertNotIn("result", worker)
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_new/run"), [{"message": "Finish the worker task"}])
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_new/reply"), [])


if __name__ == "__main__":
    unittest.main()

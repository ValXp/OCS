import tempfile
import unittest

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from tests.orchestration_cli_harness import configure_retry_server, payloads_for
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from orchestration_cli_harness import configure_retry_server, payloads_for


class RetryTimeoutOrchestrationCliTest(unittest.TestCase):
    def test_start_retries_retryable_provider_failure_and_persists_success_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_retry_server(
                    server,
                    run_payloads=[
                        {"id": "msg_user_failed", "status": "failed", "error": "transient provider outage"},
                        {"id": "msg_user_retry", "status": "submitted"},
                    ],
                )
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
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "provider",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(
            payloads_for(requests, "POST", "/session/ses_retry/run"),
            [{"message": "Finish the worker task"}, {"message": "Finish the worker task"}],
        )
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_retry/reply"), [{}])
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "done")
        retry_worker = payload["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["provider"])
        self.assertEqual(retry_worker["prompt_ids"], ["msg_user_retry"])
        self.assertEqual(retry_worker["last_failure_category"], "provider")
        self.assertEqual(retry_worker["last_failure_reason"], "transient provider outage")
        self.assertIsNone(retry_worker["failure_reason"])
        self.assertNotIn("error", retry_worker)
        self.assertNotIn("failure_retryable", retry_worker)
        self.assertEqual(retry_worker["next_eligible_action"], "collect")
        self.assertEqual(retry_worker["result"]["message_ids"], {"user": "msg_user_retry", "assistant": "msg_assistant_1"})

    def test_start_stops_after_retry_exhaustion_and_records_failure_reason(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_retry_server(
                    server,
                    run_payloads=[
                        {"id": "msg_user_failed_1", "status": "failed", "error": "provider temporarily unavailable"},
                        {"id": "msg_user_failed_2", "status": "failed", "error": "provider still unavailable"},
                    ],
                )
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
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "provider",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(start.returncode, 69)
        self.assertEqual(start.stdout, "")
        self.assertIn("provider still unavailable", start.stderr)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(
            payloads_for(requests, "POST", "/session/ses_retry/run"),
            [{"message": "Finish the worker task"}, {"message": "Finish the worker task"}],
        )
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_retry/reply"), [])
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "failed")
        retry_worker = payload["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "failed")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["provider"])
        self.assertEqual(retry_worker["prompt_ids"], ["msg_user_failed_2"])
        self.assertEqual(retry_worker["failure_category"], "provider")
        self.assertEqual(retry_worker["failure_reason"], "provider still unavailable")
        self.assertEqual(retry_worker["last_failure_category"], "provider")
        self.assertEqual(retry_worker["last_failure_reason"], "provider still unavailable")
        self.assertEqual(retry_worker["next_eligible_action"], "none")

    def test_start_retries_retryable_api_failure_and_persists_success_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_retry_server(
                    server,
                    run_payloads=[
                        (503, {"error": "upstream overloaded"}),
                        {"id": "msg_user_retry", "status": "submitted"},
                    ],
                )
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
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "api",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(
            payloads_for(requests, "POST", "/session/ses_retry/run"),
            [{"message": "Finish the worker task"}, {"message": "Finish the worker task"}],
        )
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_retry/reply"), [{}])
        retry_worker = load_json(self, status, "status")["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["api"])
        self.assertEqual(retry_worker["last_failure_category"], "api")
        self.assertIn("HTTP 503", retry_worker["last_failure_reason"])
        self.assertEqual(retry_worker["next_eligible_action"], "collect")

    def test_start_prompt_uses_stored_worker_retry_policy(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_retry_server(
                    server,
                    run_payloads=[
                        {"id": "msg_user_failed", "status": "failed", "error": "transient provider outage"},
                        {"id": "msg_user_retry", "status": "submitted"},
                    ],
                )
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
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "provider",
                )
                start = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(start.returncode, 0, format_completed_process(start))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(
            payloads_for(requests, "POST", "/session/ses_retry/run"),
            [{"message": "Finish the worker task"}, {"message": "Finish the worker task"}],
        )
        retry_worker = load_json(self, status, "status")["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["last_failure_category"], "provider")
        self.assertEqual(retry_worker["next_eligible_action"], "collect")

    def test_start_skips_automatic_timeout_retry_and_persists_manual_retry_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_retry_server(
                    server,
                    session_ids=["ses_retry", "ses_retry_isolated"],
                    run_payloads=[
                        ("sleep", 0.2, {"id": "msg_user_late", "status": "submitted"}),
                        {"id": "msg_user_retry", "status": "submitted"},
                    ],
                )
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
                    "--timeout-seconds",
                    "0.05",
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "timeout",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(start.returncode, 124, format_completed_process(start))
        self.assertIn("automatic timeout retry skipped", start.stderr)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(
            payloads_for(requests, "POST", "/session/ses_retry/run"),
            [{"message": "Finish the worker task"}],
        )
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_retry/reply"), [])
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_retry_isolated/reply"), [])
        retry_worker = load_json(self, status, "status")["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "timeout")
        self.assertEqual(retry_worker["session_id"], "ses_retry")
        self.assertEqual(retry_worker["retry_count"], 0)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["timeout"])
        self.assertEqual(retry_worker["last_failure_category"], "timeout")
        self.assertEqual(retry_worker["last_failure_reason"], "worker timed out after 0.05s")
        self.assertEqual(retry_worker["next_eligible_action"], "retry")
        self.assertTrue(retry_worker["manual_retry_required"])
        self.assertNotIn("result", retry_worker)
        self.assertNotIn("timeout_retry_sessions", retry_worker)

    def test_start_times_out_stuck_worker_and_records_timeout_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_retry_server(
                    server,
                    run_payloads=[("sleep", 0.2, {"id": "msg_user_late", "status": "submitted"})],
                )
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
                    "--timeout-seconds",
                    "0.05",
                )
                start = run_ocs("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(start.returncode, 124)
        self.assertEqual(start.stdout, "")
        self.assertIn("timed out after 0.05s", start.stderr)
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_retry/run"), [{"message": "Finish the worker task"}])
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_retry/reply"), [])
        payload = load_json(self, status, "status")
        self.assertEqual(payload["status"], "timeout")
        timeout_worker = payload["workers"]["worker"]
        self.assertEqual(timeout_worker["status"], "timeout")
        self.assertEqual(timeout_worker["timeout_seconds"], 0.05)
        self.assertEqual(timeout_worker["timeout_policy"], "timeout")
        self.assertIsNotNone(timeout_worker["timeout_started_at"])
        self.assertIsNotNone(timeout_worker["timed_out_at"])
        self.assertEqual(timeout_worker["failure_category"], "timeout")
        self.assertEqual(timeout_worker["failure_reason"], "worker timed out after 0.05s")
        self.assertEqual(timeout_worker["next_eligible_action"], "none")
        self.assertNotIn("result", timeout_worker)

    def test_timeout_policy_maps_timed_out_worker_to_declared_terminal_status(self):
        expectations = {
            "blocked": (75, "blocked", "resolve_blocker", ["timeout"]),
            "failed": (69, "failed", "none", []),
            "aborted": (130, "aborted", "none", []),
        }
        for policy, (exit_code, expected_status, next_action, blockers) in expectations.items():
            with self.subTest(policy=policy):
                with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
                    with FakeOpenCodeServer() as server:
                        configure_retry_server(
                            server,
                            run_payloads=[("sleep", 0.2, {"id": "msg_user_late", "status": "submitted"})],
                        )
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
                            "--timeout-seconds",
                            "0.05",
                            "--timeout-policy",
                            policy,
                        )
                        start = run_ocs("run", "--store", store, "start", "demo")
                    status = run_ocs("run", "--store", store, "status", "demo", "--json")

                self.assertEqual(init.returncode, 0, format_completed_process(init))
                self.assertEqual(worker.returncode, 0, format_completed_process(worker))
                self.assertEqual(start.returncode, exit_code)
                self.assertEqual(status.returncode, 0, format_completed_process(status))
                payload = load_json(self, status, "status")
                self.assertEqual(payload["status"], expected_status)
                timeout_worker = payload["workers"]["worker"]
                self.assertEqual(timeout_worker["status"], expected_status)
                self.assertEqual(timeout_worker["timeout_policy"], policy)
                self.assertEqual(timeout_worker["failure_category"], "timeout")
                self.assertEqual(timeout_worker["failure_reason"], "worker timed out after 0.05s")
                self.assertEqual(timeout_worker["next_eligible_action"], next_action)
                self.assertEqual(timeout_worker["blockers"], blockers)


if __name__ == "__main__":
    unittest.main()

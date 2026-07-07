import tempfile
import unittest

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from tests.orchestration_cli_harness import configure_single_worker_server, payloads_for, request_paths
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
    from orchestration_cli_harness import configure_single_worker_server, payloads_for, request_paths


class CleanupRestartOrchestrationCliTest(unittest.TestCase):
    def test_start_attaches_session_then_reloads_it_from_store_on_restart(self):
        with tempfile.TemporaryDirectory() as store:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(server, session_ids=["ses_existing"])
                first = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--server",
                    server.url,
                    "--session",
                    "ses_existing",
                    "--prompt",
                    "First prompt",
                )
                second = run_ocs(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--server",
                    server.url,
                    "--prompt",
                    "Second prompt",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(first.returncode, 0, format_completed_process(first))
        self.assertEqual(second.returncode, 0, format_completed_process(second))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        worker = load_json(self, status, "status")["workers"]["worker"]
        self.assertEqual(worker["session_id"], "ses_existing")
        self.assertEqual(worker["status"], "done")
        self.assertEqual(
            [path for path in request_paths(requests) if path[0] == "POST"],
            [
                ("POST", "/session/ses_existing/run"),
                ("POST", "/session/ses_existing/reply"),
                ("POST", "/session/ses_existing/run"),
                ("POST", "/session/ses_existing/reply"),
            ],
        )
        self.assertEqual(
            payloads_for(requests, "POST", "/session/ses_existing/run"),
            [{"message": "First prompt"}, {"message": "Second prompt"}],
        )
        self.assertEqual(payloads_for(requests, "POST", "/api/session"), [])

    def test_start_with_cleanup_deletes_created_disposable_session_and_records_cleanup(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(server)
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
                    "--cleanup",
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        worker = load_json(self, status, "status")["workers"]["worker"]
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(payloads_for(requests, "DELETE", "/api/session/ses_new"), [None])
        self.assertEqual(payloads_for(requests, "POST", "/session/ses_new/run"), [{"message": "Finish the worker task"}])

    def test_cleanup_failure_policy_matches_prompt_and_stored_prompt_start(self):
        prompt_worker, prompt_start = self._start_with_failing_cleanup(stored_prompt=False)
        stored_worker, stored_start = self._start_with_failing_cleanup(stored_prompt=True)

        self.assertEqual(prompt_start.returncode, 69, format_completed_process(prompt_start))
        self.assertEqual(stored_start.returncode, 69, format_completed_process(stored_start))
        self.assertIn("disposable session cleanup failed", prompt_start.stderr)
        self.assertIn("disposable session cleanup failed", stored_start.stderr)

        expected_failure_reason = "DELETE /api/session/ses_new failed: HTTP 500"
        expected_worker_state = {
            "status": "done",
            "error": None,
            "failure_category": None,
            "failure_reason": None,
            "last_failure_category": None,
            "last_failure_reason": None,
            "failure_retryable": None,
            "next_eligible_action": "collect",
            "retry_limit": 1,
            "retryable_failures": ["api"],
            "cleanup": {
                "requested": True,
                "deleted": False,
                "error": expected_failure_reason,
                "sessions": ["ses_new"],
            },
        }
        self.assertEqual(_worker_failure_state(prompt_worker), expected_worker_state)
        self.assertEqual(_worker_failure_state(stored_worker), expected_worker_state)

    def _start_with_failing_cleanup(self, *, stored_prompt):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with FakeOpenCodeServer() as server:
                configure_single_worker_server(server)
                server.json("DELETE", "/api/session/ses_new", {"error": "delete failed"}, status=500)
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
                worker_args = [
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
                    "api",
                ]
                if stored_prompt:
                    worker_args.extend(["--prompt", "Finish the worker task"])
                worker = run_ocs(*worker_args)
                start_args = ["run", "--store", store, "start", "demo", "--cleanup"]
                if not stored_prompt:
                    start_args.extend(["--prompt", "Finish the worker task"])
                start = run_ocs(*start_args)
                requests = list(server.requests)
            status = run_ocs("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, format_completed_process(init))
        self.assertEqual(worker.returncode, 0, format_completed_process(worker))
        self.assertEqual(status.returncode, 0, format_completed_process(status))
        self.assertEqual(payloads_for(requests, "DELETE", "/api/session/ses_new"), [None])
        return load_json(self, status, "status")["workers"]["worker"], start


def _worker_failure_state(worker):
    return {
        "status": worker.get("status"),
        "error": worker.get("error"),
        "failure_category": worker.get("failure_category"),
        "failure_reason": worker.get("failure_reason"),
        "last_failure_category": worker.get("last_failure_category"),
        "last_failure_reason": worker.get("last_failure_reason"),
        "failure_retryable": worker.get("failure_retryable"),
        "next_eligible_action": worker.get("next_eligible_action"),
        "retry_limit": worker.get("retry_limit"),
        "retryable_failures": worker.get("retryable_failures"),
        "cleanup": worker.get("cleanup"),
    }


if __name__ == "__main__":
    unittest.main()

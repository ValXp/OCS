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


if __name__ == "__main__":
    unittest.main()

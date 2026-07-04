import subprocess
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs


class MockedCliHarnessTest(unittest.TestCase):
    def test_run_ocs_captures_process_output_for_assertion_context(self):
        result = run_ocs()

        self.assertEqual(result.returncode, 64, format_completed_process(result))
        self.assertEqual(result.stdout, "")
        self.assertIn("usage: ocs", result.stderr)
        self.assertIn("exit code: 64", format_completed_process(result))

    def test_fake_server_serves_json_and_sse_routes_to_mocked_cli(self):
        event = {"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}}
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/api/event": {"get": {}},
            },
        }

        with FakeOpenCodeServer() as server:
            server.json("GET", "/global/health", {"status": "ok", "version": "2.0.0"})
            server.json("GET", "/doc", doc)
            server.sse("/api/event", [event])

            result = run_ocs("watch", "ses_target", "--server", server.url)

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "status session=ses_target status=done\n")
        self.assertEqual(
            server.requests,
            [("GET", "/global/health", None), ("GET", "/doc", None), ("GET", "/api/event", None)],
        )

    def test_fake_server_reports_unexpected_requests_with_registered_routes(self):
        expected_error = (
            "unexpected OpenCode request(s):\n"
            "- GET /missing payload=None\n"
            "registered routes:\n"
            "- GET /known"
        )

        response_body = None
        with self.assertRaises(AssertionError) as failure:
            with FakeOpenCodeServer() as server:
                server.json("GET", "/known", {"ok": True})

                with self.assertRaises(HTTPError) as error_context:
                    urlopen(f"{server.url}/missing", timeout=2)

                response_body = error_context.exception.read().decode("utf-8")
        self.assertEqual(response_body, expected_error)
        self.assertEqual(str(failure.exception), expected_error)

    def test_load_json_parses_stdout_and_reports_cli_context_on_failure(self):
        result = subprocess.CompletedProcess(["ocs", "ok"], 0, stdout='{"ok": true}', stderr="")
        self.assertEqual(load_json(self, result), {"ok": True})

        bad_result = subprocess.CompletedProcess(["ocs", "bad"], 0, stdout="not json", stderr="warning")
        with self.assertRaises(AssertionError) as failure:
            load_json(self, bad_result, "mock CLI")

        message = str(failure.exception)
        self.assertIn("mock CLI did not emit valid JSON", message)
        self.assertIn("command: ocs bad", message)
        self.assertIn("stdout:\nnot json", message)
        self.assertIn("stderr:\nwarning", message)


if __name__ == "__main__":
    unittest.main()

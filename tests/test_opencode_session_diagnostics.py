import json
import unittest

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, format_completed_process, load_json, run_ocs


class ApiDiagnosticsCliTest(unittest.TestCase):
    def test_routes_are_sorted_filtered_and_ignore_openapi_metadata(self):
        with FakeOpenCodeServer() as server:
            server.json(
                "GET",
                "/doc",
                {
                    "paths": {
                        "/workspace/{workspaceID}": {"delete": {}, "get": {}, "parameters": []},
                        "/project": {"post": {}, "get": {}},
                    }
                },
            )
            result = run_ocs("diagnostics", "routes", "--filter", "project", "--server", server.url)

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "GET\t/project\nPOST\t/project\n")

    def test_routes_json_does_not_require_core_ocs_capabilities(self):
        with FakeOpenCodeServer() as server:
            server.json("GET", "/doc", {"paths": {"/project": {"get": {}}}})
            result = run_ocs("diagnostics", "routes", "--json", "--server", server.url)

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(load_json(self, result, "routes"), [{"methods": ["GET"], "path": "/project"}])

    def test_get_supports_advertised_template_and_raw_output(self):
        body = '{"id":"workspace-1",  "directory":"/tmp/project"}'
        with FakeOpenCodeServer() as server:
            server.json("GET", "/doc", {"paths": {"/workspace/{workspaceID}": {"get": {}}}})

            def raw_response(handler, _request):
                handler._write_text(body)

            server.route("GET", "/workspace/workspace-1", raw_response)
            result = run_ocs("diagnostics", "get", "/workspace/workspace-1", "--raw", "--server", server.url)

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(result.stdout, body)

    def test_get_prints_parsed_json(self):
        with FakeOpenCodeServer() as server:
            server.json("GET", "/doc", {"paths": {"/project": {"get": {}}}})
            server.json("GET", "/project", [{"id": "project-1"}])
            result = run_ocs("diagnostics", "get", "/project", "--json", "--server", server.url)

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(json.loads(result.stdout), [{"id": "project-1"}])

    def test_get_default_output_is_stable_compact_json(self):
        with FakeOpenCodeServer() as server:
            server.json("GET", "/doc", {"paths": {"/project": {"get": {}}}})
            server.json("GET", "/project", {"z": 2, "a": [1]})
            result = run_ocs("diagnostics", "get", "/project", "--server", server.url)

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, '{"a":[1],"z":2}\n')

    def test_get_requests_the_exact_validated_path_and_query(self):
        with FakeOpenCodeServer() as server:
            server.json("GET", "/doc", {"paths": {"/project": {"get": {}}}})
            server.json("GET", "/project?limit=1&name=ocs", {"id": "project-1"})
            result = run_ocs(
                "diagnostics",
                "get",
                "/project?limit=1&name=ocs",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertEqual(
            server.requests,
            [("GET", "/doc", None), ("GET", "/project?limit=1&name=ocs", None)],
        )

    def test_get_rejects_ambiguous_path_forms_before_openapi_discovery(self):
        paths = (
            "/../project",
            "/%2e%2e/project",
            "/%252e%252e/project",
            "/project\\..\\admin",
            "/project%5c..%5cadmin",
            "/project%255c..%255cadmin",
            "/project%2f..%2fadmin",
            "/project%252f..%252fadmin",
            "/http://example.test/project",
            "/https:%2f%2fexample.test/project",
            "/project%not-an-escape",
        )
        for path in paths:
            with self.subTest(path=path), FakeOpenCodeServer() as server:
                result = run_ocs("diagnostics", "get", path, "--server", server.url)

            self.assertEqual(result.returncode, 65, format_completed_process(result))
            self.assertNotEqual(result.stderr, "")
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(server.requests, [])

    def test_get_rejects_unadvertised_or_cross_server_paths_without_requesting_them(self):
        for path in ("https://example.test/project", "//example.test/project", "/project#fragment", "/missing"):
            with self.subTest(path=path), FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"paths": {"/project": {"get": {}}}})
                result = run_ocs("diagnostics", "get", path, "--json", "--server", server.url)

            self.assertEqual(result.returncode, 65, format_completed_process(result))
            self.assertNotEqual(result.stderr, "")
            expected_requests = [("GET", "/doc", None)] if path == "/missing" else []
            self.assertEqual(server.requests, expected_requests)

    def test_get_normalizes_remote_and_invalid_json_errors(self):
        cases = ((404, {"error": "missing"}, "HTTP 404"), (200, "not-json", "invalid JSON"))
        for status, payload, expected in cases:
            with self.subTest(status=status), FakeOpenCodeServer() as server:
                server.json("GET", "/doc", {"paths": {"/project": {"get": {}}}})
                if status == 200:
                    server.route("GET", "/project", lambda handler, _request: handler._write_text(payload))
                else:
                    server.json("GET", "/project", payload, status=status)
                result = run_ocs("diagnostics", "get", "/project", "--json", "--server", server.url)

            self.assertEqual(result.returncode, 69, format_completed_process(result))
            self.assertIn(expected, result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_invalid_server_url_is_reported_without_a_traceback(self):
        result = run_ocs("diagnostics", "routes", "--server", "not-a-url")

        self.assertEqual(result.returncode, 69, format_completed_process(result))
        self.assertIn("invalid OpenCode server URL", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_help_exposes_only_read_only_diagnostics_subcommands(self):
        result = run_ocs("diagnostics", "--help")

        self.assertEqual(result.returncode, 0, format_completed_process(result))
        self.assertIn("{routes,get}", result.stdout)
        for method in ("post", "put", "patch", "delete"):
            self.assertNotIn(method, result.stdout.lower())


if __name__ == "__main__":
    unittest.main()

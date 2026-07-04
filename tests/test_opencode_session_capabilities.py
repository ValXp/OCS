import unittest

try:
    from tests.mocked_cli_harness import FakeOpenCodeServer, load_json, run_ocs
except ModuleNotFoundError:
    from mocked_cli_harness import FakeOpenCodeServer, load_json, run_ocs


def capability_server(*, health=None, doc=None, health_path="/global/health"):
    server = FakeOpenCodeServer()
    server.json("GET", health_path, health or {"status": "ok", "version": "1.2.3"})
    server.json("GET", "/doc", doc or {"openapi": "3.1.0", "paths": {}})
    return server


class CapabilityProbeCliTest(unittest.TestCase):
    def test_compact_summary_reports_detected_paths(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/api/session/{sessionID}/wait": {"post": {}},
                "/api/event": {"get": {}},
                "/session/{sessionID}/message": {"post": {}},
            },
        }

        with capability_server(doc=doc) as server:
            result = run_ocs("capabilities", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout.strip(),
            "health=ok version=1.2.3 session=/api/session prompt=/api/session/{sessionID}/prompt "
            "wait=/api/session/{sessionID}/wait events=/api/event execution=/session/{sessionID}/message "
            "legacy=unsupported",
        )

    def test_json_output_exposes_capability_contract(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/api/session/{sessionID}/wait": {"post": {}},
                "/api/event": {"get": {}},
                "/session/{sessionID}/message": {"post": {}},
                "/session/{sessionID}/run": {"post": {}},
                "/session/{sessionID}/reply": {"post": {}},
            },
        }
        health = {"status": "ok", "version": "2.0.0"}

        with capability_server(health=health, doc=doc) as server:
            result = run_ocs("capabilities", "--server", server.url, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = load_json(self, result)
        self.assertEqual(payload["health"], "ok")
        self.assertEqual(payload["version"], "2.0.0")
        self.assertTrue(payload["v2_prompt_support"])
        self.assertTrue(payload["v2_wait_support"])
        self.assertTrue(payload["event_support"])
        self.assertTrue(payload["blocking_message_available"])
        self.assertTrue(payload["blocking_execution_available"])
        self.assertTrue(payload["legacy_fallback_available"])
        self.assertEqual(
            payload["route_availability"],
            {
                "session": {"path": "/api/session", "method": "POST", "available": True},
                "v2_prompt": {
                    "path": "/api/session/{sessionID}/prompt",
                    "method": "POST",
                    "available": True,
                },
                "v2_wait": {
                    "path": "/api/session/{sessionID}/wait",
                    "method": "POST",
                    "available": True,
                },
                "events": {"path": "/api/event", "method": "GET", "available": True},
                "blocking_message": {
                    "path": "/session/{sessionID}/message",
                    "method": "POST",
                    "available": True,
                },
                "legacy_run": {
                    "path": "/session/{sessionID}/run",
                    "method": "POST",
                    "available": True,
                },
                "legacy_reply": {
                    "path": "/session/{sessionID}/reply",
                    "method": "POST",
                    "available": True,
                },
            },
        )

    def test_unsupported_server_has_stable_exit_and_clear_error(self):
        doc = {"openapi": "3.1.0", "paths": {"/unrelated": {"get": {}}}}

        with capability_server(doc=doc) as server:
            result = run_ocs("capabilities", "--server", server.url)

        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported OpenCode server", result.stderr)
        self.assertIn("missing session control: POST /api/session or POST /session", result.stderr)
        self.assertIn(
            "missing prompt admission or blocking execution: POST /api/session/{sessionID}/prompt, POST /session/{sessionID}/message, or legacy POST /session/{sessionID}/run + POST /session/{sessionID}/reply",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()

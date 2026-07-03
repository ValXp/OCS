import json
import os
import unittest

from harness import assert_success, format_completed_process, require_server_url, run_ocs


SERVER_ENV = "OCS_E2E_SERVER_URL"
EXPECTED_ROUTE_METHODS = {
    "session": "POST",
    "v2_prompt": "POST",
    "v2_wait": "POST",
    "events": "GET",
    "legacy_run": "POST",
    "legacy_reply": "POST",
}


@unittest.skipUnless(os.environ.get(SERVER_ENV), f"set {SERVER_ENV} to run OpenCode E2E tests")
class CapabilitiesTracerE2ETest(unittest.TestCase):
    def test_capabilities_json_contract_against_configured_server(self):
        server_url = require_server_url(self)

        result = run_ocs("capabilities", "--server", server_url, "--json")

        assert_success(self, result)
        payload = self._load_json(result)
        context = self._payload_context(payload)
        self.assertIsInstance(payload, dict, context)
        for field in ("health", "version"):
            self.assertIn(field, payload, context)
            self.assertIsInstance(payload[field], str, context)
            self.assertNotEqual(payload[field], "", context)
        for field in (
            "v2_prompt_support",
            "v2_wait_support",
            "event_support",
            "legacy_fallback_available",
        ):
            self.assertIn(field, payload, context)
            self.assertIsInstance(payload[field], bool, context)

        self.assertIn("route_availability", payload, context)
        routes = payload["route_availability"]
        self.assertIsInstance(routes, dict, context)
        for name, method in EXPECTED_ROUTE_METHODS.items():
            self.assertIn(name, routes, context)
            route = routes[name]
            self.assertIsInstance(route, dict, context)
            self.assertIn("path", route, context)
            self.assertIn("method", route, context)
            self.assertIn("available", route, context)
            self.assertIsInstance(route["path"], str, context)
            self.assertNotEqual(route["path"], "", context)
            self.assertEqual(route["method"], method, context)
            self.assertIsInstance(route["available"], bool, context)

        self.assertTrue(routes["session"]["available"], context)
        self.assertEqual(payload["v2_prompt_support"], routes["v2_prompt"]["available"], context)
        self.assertEqual(payload["v2_wait_support"], routes["v2_wait"]["available"], context)
        self.assertEqual(payload["event_support"], routes["events"]["available"], context)
        self.assertEqual(
            payload["legacy_fallback_available"],
            routes["legacy_run"]["available"] and routes["legacy_reply"]["available"],
            context,
        )
        self.assertTrue(payload["v2_prompt_support"] or payload["legacy_fallback_available"], context)

    def _load_json(self, result):
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"capabilities --json did not emit valid JSON: {error}\n{format_completed_process(result)}")

    def _payload_context(self, payload):
        return "capabilities payload:\n" + json.dumps(payload, indent=2, sort_keys=True)


if __name__ == "__main__":
    unittest.main()

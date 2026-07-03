import json
import tempfile
import unittest
import uuid

from harness import (
    assert_success,
    format_completed_process,
    live_validate_selection_args,
    load_json,
    require_live_server_url,
    run_ocs,
)


STATUS_TERMS = {"queued", "active", "blocked", "done", "failed", "aborted", "timeout"}


class LiveValidateE2ETest(unittest.TestCase):
    def test_live_validate_json_pong_against_configured_server(self):
        server_url = require_live_server_url(self)
        marker = f"ocs-e2e-live-{uuid.uuid4().hex[:12]}"
        prefix = f"{marker}-"

        with tempfile.TemporaryDirectory(prefix=f"{marker}-target-") as directory:
            result = run_ocs(
                "live_validate",
                "--json",
                "--directory",
                directory,
                "--prefix",
                prefix,
                *live_validate_selection_args(),
                "--server",
                server_url,
            )

        assert_success(self, result)
        payload = load_json(self, result, "live_validate --json")
        context = self._context(payload)

        self.assertTrue(payload.get("ok"), context)
        self.assertEqual(payload.get("status"), "done", context)
        self.assertEqual(payload.get("mode"), "live-provider", context)
        self.assertEqual(
            payload.get("gate"),
            {"env": "OCS_LIVE_VALIDATE", "enabled": True, "required": "1"},
            context,
        )
        self.assertEqual(payload.get("prompt"), "Reply exactly PONG.", context)
        self.assertEqual(payload.get("directory"), directory, context)
        self.assertEqual(payload.get("prefix"), prefix, context)

        session_ids = payload.get("session_ids")
        self.assertIsInstance(session_ids, dict, context)
        for role in ("steer", "run_blocking"):
            self.assertIsInstance(session_ids.get(role), str, context)
            self.assertNotEqual(session_ids.get(role), "", context)

        steer = payload.get("checks", {}).get("v2_steer")
        self.assertIsInstance(steer, dict, context)
        self.assertEqual(steer.get("session_id"), session_ids.get("steer"), context)
        self.assertEqual(steer.get("delivery"), "steer", context)
        self.assertIn(steer.get("status"), STATUS_TERMS, context)
        self.assertIsNone(steer.get("terminal_state"), context)
        self.assertFalse((steer.get("fallback") or {}).get("used"), context)
        self.assertIn(steer.get("executed"), {True, False, "unknown"}, context)
        self.assertIsInstance(steer.get("execution_evidence"), dict, context)

        legacy = payload.get("checks", {}).get("legacy_run_reply")
        self.assertIsInstance(legacy, dict, context)
        self.assertEqual(legacy.get("session_id"), session_ids.get("run_blocking"), context)
        self.assertTrue(legacy.get("succeeded"), context)
        self.assertEqual(legacy.get("status"), "done", context)
        self.assertEqual(legacy.get("terminal_state"), "done", context)
        self.assertTrue((legacy.get("fallback") or {}).get("used"), context)
        self.assertTrue(legacy.get("pong"), context)
        self.assertEqual(str(legacy.get("text", "")).strip(), "PONG", context)
        self.assertIsInstance(legacy.get("message_ids"), dict, context)

        cleanup = payload.get("cleanup")
        self.assertIsInstance(cleanup, dict, context)
        self.assertEqual(cleanup.get("status"), "done", context)
        self.assertEqual(cleanup.get("errors"), [], context)
        self.assertEqual(
            cleanup.get("deleted"),
            [session_ids["steer"], session_ids["run_blocking"]],
            context,
        )
        self.assertEqual(
            cleanup.get("verified"),
            [session_ids["steer"], session_ids["run_blocking"]],
            context,
        )
        self.assertEqual(payload.get("checks", {}).get("cleanup"), cleanup, context)

        for session_id in session_ids.values():
            inspect_result = run_ocs("inspect", session_id, "--json", "--server", server_url)
            self.assertNotEqual(inspect_result.returncode, 0, format_completed_process(inspect_result))

    def _context(self, payload):
        return json.dumps(payload, indent=2, sort_keys=True)


if __name__ == "__main__":
    unittest.main()

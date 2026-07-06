import json
import tempfile
import unittest

try:
    from tests.mocked_cli_harness import (
        payload_directory,
        prompt_message_id,
        prompt_text,
        request_paths,
        run_ocs,
        smoke_open_code_server,
    )
except ModuleNotFoundError:
    from mocked_cli_harness import (
        payload_directory,
        prompt_message_id,
        prompt_text,
        request_paths,
        run_ocs,
        smoke_open_code_server,
    )


class SmokeCliTest(unittest.TestCase):
    def test_smoke_runs_end_to_end_and_verifies_disposable_cleanup(self):
        with tempfile.TemporaryDirectory() as directory, smoke_open_code_server() as server:
            result = run_ocs("smoke", "--directory", directory, "--prefix", "ocs-smoke-test-", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "smoke status=done health=ok version=2.0.0 session=ses_smoke_1 steer=queued "
            "run=skipped events=session.prompt.admitted,session.status blockers=0 cleanup=done no_live_model=true\n",
        )
        self.assertEqual(
            request_paths(server.requests),
            [
                ("GET", "/global/health"),
                ("GET", "/doc"),
                ("POST", "/api/session"),
                ("GET", "/api/event"),
                ("POST", "/api/session/ses_smoke_1/prompt"),
                ("GET", "/permission"),
                ("GET", "/question"),
                ("DELETE", "/api/session/ses_smoke_1"),
                ("GET", "/api/session/ses_smoke_1"),
            ],
        )
        create_payload = server.requests[2][2]
        self.assertEqual(payload_directory(create_payload), directory)
        self.assertTrue(create_payload["title"].startswith("ocs-smoke-test-"))
        self.assertEqual(create_payload["metadata"]["prefix"], "ocs-smoke-test-")
        steer_payload = server.requests[4][2]
        self.assertTrue(prompt_message_id(steer_payload).startswith("msg_ocs-smoke-test-"))
        self.assertEqual(prompt_text(steer_payload), "ocs smoke steer")
        self.assertEqual(steer_payload["delivery"], "steer")

    def test_cleanup_deletes_stale_disposable_sessions_in_target_directory(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other_directory:
            sessions = [
                {"id": "ses_keep", "title": "regular", "directory": directory},
                {"id": "ses_old", "title": "ocs-smoke-test-old", "directory": directory},
                {"id": "ocs-smoke-test-id", "title": "generated", "directory": directory},
                {"id": "ses_other_dir", "title": "ocs-smoke-test-other", "directory": other_directory},
            ]
            with smoke_open_code_server(sessions=sessions) as server:
                result = run_ocs(
                    "cleanup",
                    "--directory",
                    directory,
                    "--prefix",
                    "ocs-smoke-test-",
                    "--server",
                    server.url,
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            f"cleanup stale=2 deleted=2 verified=2 prefix=ocs-smoke-test- dir={directory}\n",
        )
        self.assertEqual(
            request_paths(server.requests),
            [
                ("GET", "/doc"),
                ("GET", "/api/session"),
                ("DELETE", "/api/session/ses_old"),
                ("GET", "/api/session/ses_old"),
                ("DELETE", "/api/session/ocs-smoke-test-id"),
                ("GET", "/api/session/ocs-smoke-test-id"),
            ],
        )

    def test_smoke_cleans_disposable_session_after_partial_failure(self):
        with tempfile.TemporaryDirectory() as directory, smoke_open_code_server(
            prompt_response={"error": "prompt admission rejected"}, prompt_status=422
        ) as server:
            result = run_ocs("smoke", "--directory", directory, "--prefix", "ocs-smoke-test-", "--server", server.url)

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("smoke failed", result.stderr)
        self.assertIn("POST /api/session/ses_smoke_1/prompt failed: HTTP 422", result.stderr)
        self.assertIn("cleanup=done deleted=1 verified=1", result.stderr)
        self.assertEqual(
            request_paths(server.requests),
            [
                ("GET", "/global/health"),
                ("GET", "/doc"),
                ("POST", "/api/session"),
                ("GET", "/api/event"),
                ("POST", "/api/session/ses_smoke_1/prompt"),
                ("DELETE", "/api/session/ses_smoke_1"),
                ("GET", "/api/session/ses_smoke_1"),
            ],
        )

    def test_smoke_json_reports_no_live_model_mode_and_check_metadata(self):
        with tempfile.TemporaryDirectory() as directory, smoke_open_code_server() as server:
            result = run_ocs(
                "smoke",
                "--directory",
                directory,
                "--prefix",
                "ocs-smoke-test-",
                "--no-live-model",
                "--json",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["mode"], "no-live-model")
        self.assertTrue(payload["no_live_model"])
        self.assertEqual(payload["health"], "ok")
        self.assertEqual(payload["version"], "2.0.0")
        self.assertEqual(payload["directory"], directory)
        self.assertEqual(payload["prefix"], "ocs-smoke-test-")
        self.assertEqual(payload["session_id"], "ses_smoke_1")
        self.assertEqual(payload["event_types"], ["session.prompt.admitted", "session.status"])
        self.assertEqual(payload["cleanup"]["status"], "done")
        self.assertEqual(payload["cleanup"]["deleted"], ["ses_smoke_1"])
        self.assertEqual(payload["cleanup"]["verified"], ["ses_smoke_1"])
        self.assertEqual(payload["capabilities"]["route_availability"]["events"]["path"], "/api/event")
        self.assertEqual(payload["checks"]["steer"]["status"], "queued")
        self.assertFalse(payload["checks"]["steer"]["fallback"]["used"])
        self.assertEqual(payload["checks"]["run_blocking"]["status"], "skipped")
        self.assertEqual(payload["checks"]["run_blocking"]["reason"], "no-live-model")
        self.assertTrue(payload["checks"]["run_blocking"]["fallback"]["available"])
        self.assertFalse(payload["checks"]["run_blocking"]["fallback"]["used"])
        self.assertEqual(payload["checks"]["blockers"], {"status": "done", "permissions": 0, "questions": 0, "total": 0})

    def test_default_smoke_does_not_call_legacy_run_reply_in_no_live_model_mode(self):
        with tempfile.TemporaryDirectory() as directory, smoke_open_code_server() as server:
            result = run_ocs(
                "smoke",
                "--directory",
                directory,
                "--prefix",
                "ocs-smoke-test-",
                "--json",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["no_live_model"])
        self.assertEqual(payload["checks"]["run_blocking"]["status"], "skipped")
        self.assertEqual(payload["checks"]["run_blocking"]["reason"], "no-live-model")
        self.assertTrue(payload["checks"]["run_blocking"]["fallback"]["available"])
        self.assertFalse(payload["checks"]["run_blocking"]["fallback"]["used"])
        self.assertEqual(
            payload["checks"]["run_blocking"]["api_path"],
            {"run": "/session/{sessionID}/run", "reply": "/session/{sessionID}/reply"},
        )
        self.assertNotIn(("POST", "/session/ses_smoke_1/run"), request_paths(server.requests))
        self.assertNotIn(("POST", "/session/ses_smoke_1/reply"), request_paths(server.requests))


if __name__ == "__main__":
    unittest.main()

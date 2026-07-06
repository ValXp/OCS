import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from tests.mocked_cli_harness import (
        live_validation_open_code_server,
        payload_directory,
        prompt_text,
        request_paths,
    )
except ModuleNotFoundError:
    from mocked_cli_harness import (
        live_validation_open_code_server,
        payload_directory,
        prompt_text,
        request_paths,
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class LiveValidateCliTest(unittest.TestCase):
    def run_cli(self, *args, env=None):
        command_env = os.environ.copy()
        command_env.pop("OCS_LIVE_VALIDATE", None)
        if env:
            command_env.update(env)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=REPO_ROOT,
            env=command_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_live_validate_requires_env_flag_before_server_requests(self):
        with live_validation_open_code_server() as server:
            result = self.run_cli("live_validate", "--server", server.url)

        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stdout, "")
        self.assertIn("live-provider validation disabled", result.stderr)
        self.assertIn("OCS_LIVE_VALIDATE=1", result.stderr)
        self.assertEqual(server.requests, [])

    def test_live_validate_runs_pong_validation_and_cleans_sessions(self):
        with tempfile.TemporaryDirectory() as directory, live_validation_open_code_server() as server:
            result = self.run_cli(
                "live_validate",
                "--directory",
                directory,
                "--prefix",
                "ocs-live-test-",
                "--json",
                "--server",
                server.url,
                env={"OCS_LIVE_VALIDATE": "1"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["mode"], "live-provider")
        self.assertEqual(payload["gate"], {"env": "OCS_LIVE_VALIDATE", "enabled": True, "required": "1"})
        self.assertEqual(payload["prompt"], "Reply exactly PONG.")
        self.assertEqual(payload["directory"], directory)
        self.assertEqual(payload["prefix"], "ocs-live-test-")
        self.assertEqual(payload["session_ids"], {"steer": "ses_live_1", "run_blocking": "ses_live_2"})
        self.assertEqual(payload["checks"]["v2_steer"]["executed"], "unknown")
        self.assertEqual(payload["checks"]["v2_steer"]["status"], "queued")
        self.assertEqual(payload["checks"]["v2_steer"]["delivery"], "steer")
        self.assertEqual(
            payload["checks"]["wait"],
            {"available": True, "api_path": "/api/session/{sessionID}/wait", "status": "available"},
        )
        self.assertTrue(payload["checks"]["run_blocking"]["succeeded"])
        self.assertTrue(payload["checks"]["run_blocking"]["pong"])
        self.assertEqual(payload["checks"]["run_blocking"]["text"], "PONG")
        self.assertEqual(payload["checks"]["run_blocking"]["execution_strategy"], "session_message")
        self.assertEqual(
            payload["cleanup"],
            {
                "status": "done",
                "deleted": ["ses_live_1", "ses_live_2"],
                "verified": ["ses_live_1", "ses_live_2"],
                "errors": [],
            },
        )
        self.assertEqual(
            request_paths(server.requests),
            [
                ("GET", "/global/health"),
                ("GET", "/doc"),
                ("POST", "/api/session"),
                ("POST", "/api/session/ses_live_1/prompt"),
                ("POST", "/api/session/ses_live_1/wait"),
                ("GET", "/api/session/ses_live_1"),
                ("POST", "/api/session"),
                ("POST", "/session/ses_live_2/message"),
                ("DELETE", "/api/session/ses_live_1"),
                ("GET", "/api/session/ses_live_1"),
                ("DELETE", "/api/session/ses_live_2"),
                ("GET", "/api/session/ses_live_2"),
            ],
        )
        self.assertTrue(server.requests[2][2]["title"].startswith("ocs-live-test-"))
        self.assertEqual(server.requests[2][2]["metadata"]["kind"], "live-provider-validation")
        self.assertEqual(prompt_text(server.requests[3][2]), "Reply exactly PONG.")
        self.assertEqual(server.requests[3][2]["delivery"], "steer")
        self.assertEqual(server.requests[4][2], {})
        self.assertTrue(server.requests[6][2]["title"].startswith("ocs-live-test-"))
        self.assertTrue(server.requests[7][2]["messageID"].startswith("msg_"))
        self.assertEqual(server.requests[7][2]["parts"], [{"type": "text", "text": "Reply exactly PONG."}])

    def test_live_validate_passes_agent_and_model_to_disposable_sessions(self):
        with tempfile.TemporaryDirectory() as directory, live_validation_open_code_server() as server:
            result = self.run_cli(
                "live_validate",
                "--directory",
                directory,
                "--agent",
                "build",
                "--model",
                "openai/gpt-5.5",
                "--json",
                "--server",
                server.url,
                env={"OCS_LIVE_VALIDATE": "1"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        create_payloads = [payload for method, path, payload in server.requests if method == "POST" and path == "/api/session"]
        self.assertEqual(len(create_payloads), 2)
        for payload in create_payloads:
            self.assertEqual(payload_directory(payload), directory)
            self.assertEqual(payload["agent"], "build")
            self.assertEqual(payload["model"], "openai/gpt-5.5")

    def test_live_validate_marks_steer_executed_true_from_wait_completion_evidence(self):
        with tempfile.TemporaryDirectory() as directory, live_validation_open_code_server(
            wait_payload={"sessionID": "ses_live_1", "status": "completed"}
        ) as server:
            result = self.run_cli(
                "live_validate",
                "--directory",
                directory,
                "--json",
                "--server",
                server.url,
                env={"OCS_LIVE_VALIDATE": "1"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIs(payload["checks"]["v2_steer"]["executed"], True)
        self.assertEqual(
            payload["checks"]["v2_steer"]["execution_evidence"],
            {"source": "wait", "status": "done", "reason": "observed_execution_state"},
        )

    def test_live_validate_marks_steer_executed_false_from_wait_queued_evidence(self):
        with tempfile.TemporaryDirectory() as directory, live_validation_open_code_server(
            wait_payload={"sessionID": "ses_live_1", "status": "admitted"}
        ) as server:
            result = self.run_cli(
                "live_validate",
                "--directory",
                directory,
                "--json",
                "--server",
                server.url,
                env={"OCS_LIVE_VALIDATE": "1"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIs(payload["checks"]["v2_steer"]["executed"], False)
        self.assertEqual(
            payload["checks"]["v2_steer"]["execution_evidence"],
            {"source": "wait", "status": "queued", "reason": "observed_not_executed_state"},
        )

    def test_live_validate_marks_steer_executed_true_from_session_message_evidence(self):
        with tempfile.TemporaryDirectory() as directory, live_validation_open_code_server(
            wait_available=False,
            session_payloads={
                "ses_live_1": {
                    "messages": [{"role": "assistant", "status": "completed", "text": "PONG"}],
                }
            },
        ) as server:
            result = self.run_cli(
                "live_validate",
                "--directory",
                directory,
                "--json",
                "--server",
                server.url,
                env={"OCS_LIVE_VALIDATE": "1"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["checks"]["wait"],
            {"available": False, "api_path": "/api/session/{sessionID}/wait", "status": "unavailable"},
        )
        self.assertIs(payload["checks"]["v2_steer"]["executed"], True)
        self.assertEqual(
            payload["checks"]["v2_steer"]["execution_evidence"],
            {"source": "message", "status": "done", "reason": "observed_assistant_message"},
        )

    def test_live_validate_uses_message_evidence_after_inconclusive_wait(self):
        with tempfile.TemporaryDirectory() as directory, live_validation_open_code_server(
            wait_payload={},
            session_payloads={
                "ses_live_1": {
                    "messages": [{"role": "assistant", "status": "completed", "text": "PONG"}],
                }
            },
        ) as server:
            result = self.run_cli(
                "live_validate",
                "--directory",
                directory,
                "--json",
                "--server",
                server.url,
                env={"OCS_LIVE_VALIDATE": "1"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIs(payload["checks"]["v2_steer"]["executed"], True)
        self.assertEqual(
            payload["checks"]["v2_steer"]["execution_evidence"],
            {"source": "message", "status": "done", "reason": "observed_assistant_message"},
        )

    def test_live_validate_marks_steer_executed_true_from_event_evidence(self):
        events = [
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_live_1",
                    "messageID": "msg_assistant_live",
                    "message": {"role": "assistant", "status": "completed", "text": "PONG"},
                },
            }
        ]
        with tempfile.TemporaryDirectory() as directory, live_validation_open_code_server(
            wait_available=False,
            events=events,
        ) as server:
            result = self.run_cli(
                "live_validate",
                "--directory",
                directory,
                "--json",
                "--server",
                server.url,
                env={"OCS_LIVE_VALIDATE": "1"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIs(payload["checks"]["v2_steer"]["executed"], True)
        self.assertEqual(
            payload["checks"]["v2_steer"]["execution_evidence"],
            {"source": "event", "status": "done", "reason": "observed_execution_event"},
        )


if __name__ == "__main__":
    unittest.main()

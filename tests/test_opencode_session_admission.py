import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class AdmissionOpenCodeServer:
    def __init__(self, *, doc=None, prompt_response=None, prompt_status=200):
        self.doc = doc or {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
            },
        }
        self.prompt_response = prompt_response or {}
        self.prompt_status = prompt_status
        self.requests = []
        self.server = None
        self.thread = None

    def __enter__(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                parent.requests.append(("GET", self.path, None))
                if self.path == "/global/health":
                    self._write_json({"status": "ok", "version": "2.0.0"})
                    return
                if self.path == "/doc":
                    self._write_json(parent.doc)
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session/ses_1/prompt":
                    self._write_json(parent.prompt_response, status=parent.prompt_status)
                    return
                self.send_error(404)

            def _write_json(self, payload, *, status=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"


class AdmissionCliTest(unittest.TestCase):
    def run_cli(self, *args, env=None):
        command_env = os.environ.copy()
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

    def test_steer_admits_input_and_prints_queued_admission_status(self):
        response = {
            "sessionID": "ses_1",
            "messageID": "msg_steer_1",
            "delivery": "steer",
            "state": "admitted",
            "admittedSequence": 4,
        }

        with AdmissionOpenCodeServer(prompt_response=response) as server:
            result = self.run_cli(
                "steer",
                "ses_1",
                "Actually use the v2 prompt API.",
                "--message-id",
                "msg_steer_1",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "steer session=ses_1 message=msg_steer_1 delivery=steer status=queued admitted=4 promoted=-\n",
        )
        self.assertEqual(
            server.requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                (
                    "POST",
                    "/api/session/ses_1/prompt",
                    {
                        "messageID": "msg_steer_1",
                        "parts": [{"type": "text", "text": "Actually use the v2 prompt API."}],
                        "delivery": "steer",
                    },
                ),
            ],
        )

    def test_steer_delivery_queue_json_reports_admission_metadata_when_promoted(self):
        response = {
            "sessionID": "ses_1",
            "promptID": "prompt_queue_1",
            "delivery": "queue",
            "state": "promoted",
            "admittedSequence": 5,
            "promotedSequence": 6,
        }

        with AdmissionOpenCodeServer(prompt_response=response) as server:
            result = self.run_cli(
                "steer",
                "ses_1",
                "After this, run the benchmark.",
                "--delivery",
                "queue",
                "--message-id",
                "msg_queue_1",
                "--json",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "session_id": "ses_1",
                "message_id": "prompt_queue_1",
                "delivery": "queue",
                "state": "promoted",
                "status": "active",
                "raw_state": "promoted",
                "terminal_state": None,
                "api_path": "/api/session/{sessionID}/prompt",
                "fallback": {"available": False, "strategy": "legacy_run_reply", "used": False},
                "admitted_sequence": 5,
                "promoted_sequence": 6,
            },
        )
        self.assertEqual(
            server.requests[-1],
            (
                "POST",
                "/api/session/ses_1/prompt",
                {
                    "messageID": "msg_queue_1",
                    "parts": [{"type": "text", "text": "After this, run the benchmark."}],
                    "delivery": "queue",
                },
            ),
        )

    def test_idempotent_replay_response_reports_existing_admitted_message(self):
        response = {
            "sessionID": "ses_1",
            "messageID": "msg_repeat_1",
            "delivery": "steer",
            "state": "admitted",
            "admittedSequence": 8,
            "duplicate": True,
        }

        with AdmissionOpenCodeServer(prompt_response=response, prompt_status=409) as server:
            result = self.run_cli(
                "steer",
                "ses_1",
                "Keep going, but preserve the existing public interface.",
                "--message-id",
                "msg_repeat_1",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "steer session=ses_1 message=msg_repeat_1 delivery=steer status=queued admitted=8 promoted=-\n",
        )

    def test_unsupported_v2_prompt_behavior_reports_no_legacy_fallback(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/session/{sessionID}/run": {"post": {}},
                "/session/{sessionID}/reply": {"post": {}},
            },
        }

        with AdmissionOpenCodeServer(
            doc=doc,
            prompt_response={"error": "unsupported delivery mode: steer"},
            prompt_status=422,
        ) as server:
            result = self.run_cli(
                "steer",
                "ses_1",
                "Use auth-v2.ts; do not touch legacy-auth.ts.",
                "--message-id",
                "msg_unsupported_1",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported v2 prompt behavior", result.stderr)
        self.assertIn("unsupported delivery mode: steer", result.stderr)
        self.assertIn("legacy run/reply fallback is not used", result.stderr)
        self.assertEqual(
            [path for method, path, _ in server.requests if method == "POST"],
            ["/api/session/ses_1/prompt"],
        )

    def test_missing_v2_prompt_capability_does_not_use_legacy_run_reply(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/session/{sessionID}/run": {"post": {}},
                "/session/{sessionID}/reply": {"post": {}},
            },
        }

        with AdmissionOpenCodeServer(doc=doc) as server:
            result = self.run_cli(
                "steer",
                "ses_1",
                "After this, write tests for the module.",
                "--delivery",
                "queue",
                "--message-id",
                "msg_no_prompt_1",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported v2 prompt capability", result.stderr)
        self.assertIn("legacy run/reply fallback is not used", result.stderr)
        self.assertEqual([path for method, path, _ in server.requests if method == "POST"], [])


if __name__ == "__main__":
    unittest.main()

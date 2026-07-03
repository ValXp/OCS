import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class LiveValidationOpenCodeServer:
    def __init__(
        self,
        *,
        reply_payload=None,
        wait_payload=None,
        wait_available=True,
        session_payloads=None,
        events=None,
    ):
        self.reply_payload = reply_payload or {
            "id": "msg_assistant_live",
            "status": "completed",
            "cost": 0.001,
            "tokens": {"input": 4, "output": 1, "total": 5},
            "text": "PONG",
        }
        self.wait_payload = wait_payload or {}
        self.wait_available = wait_available
        self.session_payloads = session_payloads or {}
        self.events = events
        self.sessions = []
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
                    paths = {
                        "/api/session": {"get": {}, "post": {}},
                        "/api/session/{sessionID}/prompt": {"post": {}},
                        "/session/{sessionID}/run": {"post": {}},
                        "/session/{sessionID}/reply": {"post": {}},
                    }
                    if parent.wait_available:
                        paths["/api/session/{sessionID}/wait"] = {"post": {}}
                    if parent.events is not None:
                        paths["/api/event"] = {"get": {}}
                    self._write_json(
                        {
                            "openapi": "3.1.0",
                            "paths": paths,
                        }
                    )
                    return
                if self.path == "/api/event":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for event in parent.events:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return
                if self.path.startswith("/api/session/"):
                    session_id = self.path.rsplit("/", 1)[-1]
                    for session in parent.sessions:
                        if session["id"] == session_id:
                            payload = dict(session)
                            payload.update(parent.session_payloads.get(session_id, {}))
                            self._write_json(payload)
                            return
                    self.send_error(404)
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session":
                    session_id = f"ses_live_{len(parent.sessions) + 1}"
                    session = {
                        "id": session_id,
                        "title": payload["title"],
                        "directory": _payload_directory(payload),
                        "metadata": payload["metadata"],
                    }
                    parent.sessions.append(session)
                    self._write_json(session)
                    return
                if self.path == "/api/session/ses_live_1/prompt":
                    self._write_json(
                        {
                            "sessionID": "ses_live_1",
                            "messageID": _prompt_message_id(payload),
                            "delivery": "steer",
                            "state": "admitted",
                            "admittedSequence": 1,
                        }
                    )
                    return
                if self.path == "/api/session/ses_live_1/wait":
                    self._write_json(parent.wait_payload)
                    return
                if self.path == "/session/ses_live_2/run":
                    self._write_json({"id": "msg_user_live", "status": "submitted"})
                    return
                if self.path == "/session/ses_live_2/reply":
                    self._write_json(parent.reply_payload)
                    return
                self.send_error(404)

            def do_DELETE(self):
                parent.requests.append(("DELETE", self.path, None))
                if self.path.startswith("/api/session/"):
                    session_id = self.path.rsplit("/", 1)[-1]
                    for index, session in enumerate(parent.sessions):
                        if session["id"] == session_id:
                            del parent.sessions[index]
                            self._write_json({"id": session_id, "deleted": True})
                            return
                    self.send_error(404)
                    return
                self.send_error(404)

            def _write_json(self, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
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
        with LiveValidationOpenCodeServer() as server:
            result = self.run_cli("live_validate", "--server", server.url)

        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stdout, "")
        self.assertIn("live-provider validation disabled", result.stderr)
        self.assertIn("OCS_LIVE_VALIDATE=1", result.stderr)
        self.assertEqual(server.requests, [])

    def test_live_validate_runs_pong_validation_and_cleans_sessions(self):
        with tempfile.TemporaryDirectory() as directory, LiveValidationOpenCodeServer() as server:
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
        self.assertTrue(payload["checks"]["legacy_run_reply"]["succeeded"])
        self.assertTrue(payload["checks"]["legacy_run_reply"]["pong"])
        self.assertEqual(payload["checks"]["legacy_run_reply"]["text"], "PONG")
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
            parent_paths(server.requests),
            [
                ("GET", "/global/health"),
                ("GET", "/doc"),
                ("POST", "/api/session"),
                ("POST", "/api/session/ses_live_1/prompt"),
                ("POST", "/api/session/ses_live_1/wait"),
                ("GET", "/api/session/ses_live_1"),
                ("POST", "/api/session"),
                ("POST", "/session/ses_live_2/run"),
                ("POST", "/session/ses_live_2/reply"),
                ("DELETE", "/api/session/ses_live_1"),
                ("GET", "/api/session/ses_live_1"),
                ("DELETE", "/api/session/ses_live_2"),
                ("GET", "/api/session/ses_live_2"),
            ],
        )
        self.assertTrue(server.requests[2][2]["title"].startswith("ocs-live-test-"))
        self.assertEqual(server.requests[2][2]["metadata"]["kind"], "live-provider-validation")
        self.assertEqual(_prompt_text(server.requests[3][2]), "Reply exactly PONG.")
        self.assertEqual(server.requests[3][2]["delivery"], "steer")
        self.assertEqual(server.requests[4][2], {})
        self.assertTrue(server.requests[6][2]["title"].startswith("ocs-live-test-"))
        self.assertEqual(server.requests[7][2], {"message": "Reply exactly PONG."})

    def test_live_validate_passes_agent_and_model_to_disposable_sessions(self):
        with tempfile.TemporaryDirectory() as directory, LiveValidationOpenCodeServer() as server:
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
            self.assertEqual(_payload_directory(payload), directory)
            self.assertEqual(payload["agent"], "build")
            self.assertEqual(payload["model"], "openai/gpt-5.5")

    def test_live_validate_marks_steer_executed_true_from_wait_completion_evidence(self):
        with tempfile.TemporaryDirectory() as directory, LiveValidationOpenCodeServer(
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
        with tempfile.TemporaryDirectory() as directory, LiveValidationOpenCodeServer(
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
        with tempfile.TemporaryDirectory() as directory, LiveValidationOpenCodeServer(
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
        with tempfile.TemporaryDirectory() as directory, LiveValidationOpenCodeServer(
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
        with tempfile.TemporaryDirectory() as directory, LiveValidationOpenCodeServer(
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


def parent_paths(requests):
    return [(method, path) for method, path, _payload in requests]


def _payload_directory(payload):
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    return location.get("directory") or payload.get("directory")


def _prompt_message_id(payload):
    return payload.get("messageID") or payload.get("id")


def _prompt_text(payload):
    prompt = payload.get("prompt") if isinstance(payload.get("prompt"), dict) else {}
    if prompt.get("text") is not None:
        return prompt.get("text")
    parts = payload.get("parts") if isinstance(payload.get("parts"), list) else []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


if __name__ == "__main__":
    unittest.main()

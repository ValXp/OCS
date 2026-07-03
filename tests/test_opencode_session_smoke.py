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


class SmokeOpenCodeServer:
    def __init__(self, *, sessions=None, prompt_response=None, prompt_status=200):
        self.prompt_response = prompt_response
        self.prompt_status = prompt_status
        self.sessions = list(sessions or [])
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
                    self._write_json(
                        {
                            "openapi": "3.1.0",
                            "paths": {
                                "/api/session": {"get": {}, "post": {}},
                                "/api/session/{sessionID}/prompt": {"post": {}},
                                "/api/event": {"get": {}},
                                "/session/{sessionID}/run": {"post": {}},
                                "/session/{sessionID}/reply": {"post": {}},
                            },
                        }
                    )
                    return
                if self.path == "/api/event":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    events = [
                        {
                            "type": "session.prompt.admitted",
                            "properties": {
                                "sessionID": "ses_smoke_1",
                                "messageID": "msg_smoke_steer",
                                "delivery": "steer",
                                "state": "admitted",
                            },
                        },
                        {"type": "session.status", "properties": {"sessionID": "ses_smoke_1", "status": "completed"}},
                    ]
                    for event in events:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return
                if self.path == "/api/session":
                    self._write_json({"sessions": parent.sessions})
                    return
                if self.path.startswith("/api/session/"):
                    session_id = self.path.rsplit("/", 1)[-1]
                    for session in parent.sessions:
                        if session["id"] == session_id:
                            self._write_json(session)
                            return
                    self.send_error(404)
                    return
                if self.path == "/permission":
                    self._write_json([])
                    return
                if self.path == "/question":
                    self._write_json([])
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session":
                    session = {
                        "id": "ses_smoke_1",
                        "title": payload["title"],
                        "directory": payload["directory"],
                        "metadata": payload["metadata"],
                    }
                    parent.sessions.append(session)
                    self._write_json(session)
                    return
                if self.path == "/api/session/ses_smoke_1/prompt":
                    self._write_json(
                        parent.prompt_response
                        or {
                            "sessionID": "ses_smoke_1",
                            "messageID": payload["messageID"],
                            "delivery": "steer",
                            "state": "admitted",
                            "admittedSequence": 1,
                        },
                        status=parent.prompt_status,
                    )
                    return
                if self.path == "/session/ses_smoke_1/run":
                    self._write_json({"id": "msg_user_smoke", "status": "submitted"})
                    return
                if self.path == "/session/ses_smoke_1/reply":
                    self._write_json({"id": "msg_assistant_smoke", "status": "completed", "text": "ok"})
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


class SmokeCliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=REPO_ROOT,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_smoke_runs_end_to_end_and_verifies_disposable_cleanup(self):
        with tempfile.TemporaryDirectory() as directory, SmokeOpenCodeServer() as server:
            result = self.run_cli("smoke", "--directory", directory, "--prefix", "ocs-smoke-test-", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "smoke status=done health=ok version=2.0.0 session=ses_smoke_1 steer=queued "
            "run=skipped events=session.prompt.admitted,session.status blockers=0 cleanup=done no_live_model=true\n",
        )
        self.assertEqual(parent_paths(server.requests), [
            ("GET", "/global/health"),
            ("GET", "/doc"),
            ("POST", "/api/session"),
            ("POST", "/api/session/ses_smoke_1/prompt"),
            ("GET", "/api/event"),
            ("GET", "/permission"),
            ("GET", "/question"),
            ("DELETE", "/api/session/ses_smoke_1"),
            ("GET", "/api/session/ses_smoke_1"),
        ])
        create_payload = server.requests[2][2]
        self.assertEqual(create_payload["directory"], directory)
        self.assertTrue(create_payload["title"].startswith("ocs-smoke-test-"))
        self.assertEqual(create_payload["metadata"]["prefix"], "ocs-smoke-test-")
        steer_payload = server.requests[3][2]
        self.assertTrue(steer_payload["messageID"].startswith("ocs-smoke-test-"))
        self.assertEqual(steer_payload["delivery"], "steer")

    def test_cleanup_deletes_stale_disposable_sessions_in_target_directory(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other_directory:
            sessions = [
                {"id": "ses_keep", "title": "regular", "directory": directory},
                {"id": "ses_old", "title": "ocs-smoke-test-old", "directory": directory},
                {"id": "ocs-smoke-test-id", "title": "generated", "directory": directory},
                {"id": "ses_other_dir", "title": "ocs-smoke-test-other", "directory": other_directory},
            ]
            with SmokeOpenCodeServer(sessions=sessions) as server:
                result = self.run_cli("cleanup", "--directory", directory, "--prefix", "ocs-smoke-test-", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            f"cleanup stale=2 deleted=2 verified=2 prefix=ocs-smoke-test- dir={directory}\n",
        )
        self.assertEqual(parent_paths(server.requests), [
            ("GET", "/api/session"),
            ("DELETE", "/api/session/ses_old"),
            ("GET", "/api/session/ses_old"),
            ("DELETE", "/api/session/ocs-smoke-test-id"),
            ("GET", "/api/session/ocs-smoke-test-id"),
        ])

    def test_smoke_cleans_disposable_session_after_partial_failure(self):
        with tempfile.TemporaryDirectory() as directory, SmokeOpenCodeServer(
            prompt_response={"error": "prompt admission rejected"}, prompt_status=422
        ) as server:
            result = self.run_cli("smoke", "--directory", directory, "--prefix", "ocs-smoke-test-", "--server", server.url)

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("smoke failed", result.stderr)
        self.assertIn("POST /api/session/ses_smoke_1/prompt failed: HTTP 422", result.stderr)
        self.assertIn("cleanup=done deleted=1 verified=1", result.stderr)
        self.assertEqual(parent_paths(server.requests), [
            ("GET", "/global/health"),
            ("GET", "/doc"),
            ("POST", "/api/session"),
            ("POST", "/api/session/ses_smoke_1/prompt"),
            ("DELETE", "/api/session/ses_smoke_1"),
            ("GET", "/api/session/ses_smoke_1"),
        ])

    def test_smoke_json_reports_no_live_model_mode_and_check_metadata(self):
        with tempfile.TemporaryDirectory() as directory, SmokeOpenCodeServer() as server:
            result = self.run_cli(
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
        with tempfile.TemporaryDirectory() as directory, SmokeOpenCodeServer() as server:
            result = self.run_cli(
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
        self.assertNotIn(("POST", "/session/ses_smoke_1/run"), parent_paths(server.requests))
        self.assertNotIn(("POST", "/session/ses_smoke_1/reply"), parent_paths(server.requests))


def parent_paths(requests):
    return [(method, path) for method, path, _payload in requests]


if __name__ == "__main__":
    unittest.main()

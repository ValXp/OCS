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


class InventoryOpenCodeServer:
    def __init__(self, *, sessions=None, raw_session_bodies=None):
        self.sessions = sessions or []
        self.raw_session_bodies = raw_session_bodies or {}
        self.requests = []
        self.server = None
        self.thread = None

    def __enter__(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session":
                    self._write_json(
                        {
                            "id": "ses_new",
                            "title": "New session",
                            "directory": payload["directory"],
                            "agent": payload["agent"],
                            "model": payload["model"],
                            "cost": 0,
                            "tokens": {"input": 0, "output": 0, "total": 0},
                            "createdAt": "2026-07-02T00:00:00Z",
                            "updatedAt": "2026-07-02T00:00:01Z",
                        }
                    )
                    return
                self.send_error(404)

            def do_GET(self):
                parent.requests.append(("GET", self.path, None))
                if self.path == "/api/session":
                    self._write_json({"sessions": parent.sessions, "next": None})
                    return
                if self.path.startswith("/api/session/"):
                    session_id = self.path.rsplit("/", 1)[-1]
                    for session in parent.sessions:
                        if session["id"] == session_id:
                            if session_id in parent.raw_session_bodies:
                                self._write_raw_json(parent.raw_session_bodies[session_id])
                                return
                            self._write_json(session)
                            return
                    self.send_error(404)
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
                self._write_body(body)

            def _write_raw_json(self, body):
                self._write_body(body.encode("utf-8"))

            def _write_body(self, body):
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


class SessionInventoryCliTest(unittest.TestCase):
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

    def test_create_posts_target_directory_and_prints_compact_session(self):
        with tempfile.TemporaryDirectory() as directory, InventoryOpenCodeServer() as server:
            result = self.run_cli(
                "create",
                directory,
                "--agent",
                "build",
                "--model",
                "openai/gpt-5.5",
                "--server",
                server.url,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout.strip(),
            f'id=ses_new title="New session" dir={directory} agent=build model=openai/gpt-5.5 '
            "cost=0 tokens=0 created=2026-07-02T00:00:00Z updated=2026-07-02T00:00:01Z",
        )
        self.assertEqual(
            server.requests,
            [
                (
                    "POST",
                    "/api/session",
                    {"directory": directory, "agent": "build", "model": "openai/gpt-5.5"},
                )
            ],
        )

    def test_list_filters_sessions_and_prints_compact_lines(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other_directory:
            sessions = [
                {
                    "id": "ses_build",
                    "title": "Build task",
                    "directory": directory,
                    "agent": "build",
                    "model": "openai/gpt-5.5",
                    "cost": 1.25,
                    "tokens": {"input": 100, "output": 50, "total": 150},
                    "createdAt": "2026-07-02T00:00:00Z",
                    "updatedAt": "2026-07-02T00:00:02Z",
                },
                {
                    "id": "ses_plan",
                    "title": "Plan task",
                    "directory": other_directory,
                    "agent": "plan",
                    "model": "openai/gpt-5.5",
                    "cost": 0.5,
                    "tokens": {"input": 20, "output": 10, "total": 30},
                    "createdAt": "2026-07-02T00:01:00Z",
                    "updatedAt": "2026-07-02T00:01:02Z",
                },
            ]
            with InventoryOpenCodeServer(sessions=sessions) as server:
                result = self.run_cli(
                    "list",
                    "--directory",
                    directory,
                    "--agent",
                    "build",
                    "--server",
                    server.url,
                )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            f'id=ses_build title="Build task" dir={directory} agent=build model=openai/gpt-5.5 '
            "cost=1.25 tokens=150 created=2026-07-02T00:00:00Z updated=2026-07-02T00:00:02Z\n",
        )
        self.assertEqual(server.requests, [("GET", "/api/session", None)])

    def test_list_multiple_sessions_prints_compact_table(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other_directory:
            sessions = [
                {
                    "id": "ses_build",
                    "title": "Build task",
                    "directory": directory,
                    "agent": "build",
                    "model": "openai/gpt-5.5",
                    "cost": 1.25,
                    "tokens": {"input": 100, "output": 50, "total": 150},
                    "updatedAt": "2026-07-02T00:00:02Z",
                },
                {
                    "id": "ses_plan",
                    "title": "Plan task",
                    "directory": other_directory,
                    "agent": "plan",
                    "model": "openai/gpt-5.5",
                    "cost": 0.5,
                    "tokens": {"input": 20, "output": 10, "total": 30},
                    "updatedAt": "2026-07-02T00:01:02Z",
                },
            ]
            with InventoryOpenCodeServer(sessions=sessions) as server:
                result = self.run_cli("list", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "id\ttitle\tdir\tagent\tmodel\tcost\ttokens\tupdated\n"
            f"ses_build\t\"Build task\"\t{directory}\tbuild\topenai/gpt-5.5\t1.25\t150\t2026-07-02T00:00:02Z\n"
            f"ses_plan\t\"Plan task\"\t{other_directory}\tplan\topenai/gpt-5.5\t0.5\t30\t2026-07-02T00:01:02Z\n",
        )
        self.assertEqual(server.requests, [("GET", "/api/session", None)])

    def test_inspect_prints_one_session_with_full_compact_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            session = {
                "id": "ses_build",
                "title": "Build task",
                "directory": directory,
                "agent": "build",
                "model": "openai/gpt-5.5",
                "cost": 1.25,
                "tokens": {"input": 100, "output": 50, "total": 150},
                "createdAt": "2026-07-02T00:00:00Z",
                "updatedAt": "2026-07-02T00:00:02Z",
            }
            with InventoryOpenCodeServer(sessions=[session]) as server:
                result = self.run_cli("inspect", "ses_build", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            f'id=ses_build title="Build task" dir={directory} agent=build model=openai/gpt-5.5 '
            "cost=1.25 tokens=150 created=2026-07-02T00:00:00Z updated=2026-07-02T00:00:02Z\n",
        )
        self.assertEqual(server.requests, [("GET", "/api/session/ses_build", None)])

    def test_get_alias_prints_one_session(self):
        session = {
            "id": "ses_build",
            "title": "Build task",
            "directory": "/tmp/project",
            "agent": "build",
            "model": "openai/gpt-5.5",
            "cost": 1.25,
            "tokens": {"input": 100, "output": 50, "total": 150},
            "createdAt": "2026-07-02T00:00:00Z",
            "updatedAt": "2026-07-02T00:00:02Z",
        }
        with InventoryOpenCodeServer(sessions=[session]) as server:
            result = self.run_cli("get", "ses_build", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("id=ses_build", result.stdout)
        self.assertEqual(server.requests, [("GET", "/api/session/ses_build", None)])

    def test_delete_verifies_session_is_no_longer_readable(self):
        session = {
            "id": "ses_build",
            "title": "Build task",
            "directory": "/tmp/project",
            "agent": "build",
            "model": "openai/gpt-5.5",
            "cost": 1.25,
            "tokens": {"input": 100, "output": 50, "total": 150},
            "createdAt": "2026-07-02T00:00:00Z",
            "updatedAt": "2026-07-02T00:00:02Z",
        }
        with InventoryOpenCodeServer(sessions=[session]) as server:
            result = self.run_cli("delete", "ses_build", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "deleted id=ses_build verified=unreadable\n")
        self.assertEqual(
            server.requests,
            [("DELETE", "/api/session/ses_build", None), ("GET", "/api/session/ses_build", None)],
        )

    def test_inspect_json_exposes_complete_session_data(self):
        session = {
            "id": "ses_build",
            "title": "Build task",
            "directory": "/tmp/project",
            "agent": "build",
            "model": "openai/gpt-5.5",
            "cost": 1.25,
            "tokens": {"input": 100, "output": 50, "total": 150},
            "createdAt": "2026-07-02T00:00:00Z",
            "updatedAt": "2026-07-02T00:00:02Z",
            "provider": {"id": "openai", "name": "OpenAI"},
            "metadata": {"branch": "main"},
        }
        with InventoryOpenCodeServer(sessions=[session]) as server:
            result = self.run_cli("inspect", "ses_build", "--json", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), session)
        self.assertEqual(server.requests, [("GET", "/api/session/ses_build", None)])

    def test_inspect_raw_exposes_exact_api_response_body(self):
        raw_body = '{"id":"ses_build",  "title":"Build task","metadata":{"b":2,"a":1}}'
        with InventoryOpenCodeServer(
            sessions=[{"id": "ses_build"}], raw_session_bodies={"ses_build": raw_body}
        ) as server:
            result = self.run_cli("inspect", "ses_build", "--raw", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, raw_body)
        self.assertEqual(server.requests, [("GET", "/api/session/ses_build", None)])


if __name__ == "__main__":
    unittest.main()

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


class LifecycleOpenCodeServer:
    def __init__(self, *, children=None):
        self.children = children or []
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
                if self.path == "/session/ses_active/abort":
                    self._write_json({"sessionID": "ses_active", "accepted": True, "status": "aborting"})
                    return
                if self.path == "/session/ses_parent/fork":
                    self._write_json({"id": "ses_child", "parentID": "ses_parent", "messageID": "msg_branch"})
                    return
                self.send_error(404)

            def do_GET(self):
                parent.requests.append(("GET", self.path, None))
                if self.path == "/session/ses_parent/children":
                    self._write_json({"children": parent.children})
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


class LifecycleCliTest(unittest.TestCase):
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

    def test_abort_posts_legacy_route_and_reports_acceptance(self):
        with LifecycleOpenCodeServer() as server:
            result = self.run_cli("abort", "ses_active", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "abort session=ses_active accepted=true status=active\n")
        self.assertEqual(server.requests, [("POST", "/session/ses_active/abort", {})])

    def test_fork_posts_legacy_route_with_message_id_and_prints_child_session(self):
        with LifecycleOpenCodeServer() as server:
            result = self.run_cli("fork", "ses_parent", "--message-id", "msg_branch", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "forked parent=ses_parent child=ses_child message=msg_branch\n")
        self.assertEqual(server.requests, [("POST", "/session/ses_parent/fork", {"messageID": "msg_branch"})])

    def test_children_filters_by_directory_and_prints_compact_sessions(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other_directory:
            children = [
                {
                    "id": "ses_child_1",
                    "title": "Child one",
                    "directory": directory,
                    "agent": "build",
                    "model": "openai/gpt-5.5",
                    "cost": 0.2,
                    "tokens": {"input": 10, "output": 5, "total": 15},
                    "createdAt": "2026-07-02T00:00:00Z",
                    "updatedAt": "2026-07-02T00:00:03Z",
                },
                {
                    "id": "ses_child_2",
                    "title": "Child two",
                    "directory": other_directory,
                    "agent": "plan",
                    "model": "openai/gpt-5.5",
                },
            ]
            with LifecycleOpenCodeServer(children=children) as server:
                result = self.run_cli("children", "ses_parent", "--directory", directory, "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            f'id=ses_child_1 title="Child one" dir={directory} agent=build model=openai/gpt-5.5 '
            "cost=0.2 tokens=15 created=2026-07-02T00:00:00Z updated=2026-07-02T00:00:03Z\n",
        )
        self.assertEqual(server.requests, [("GET", "/session/ses_parent/children", None)])

    def test_children_json_outputs_complete_child_session_data(self):
        children = [
            {
                "id": "ses_child_1",
                "title": "Child one",
                "directory": "/tmp/project",
                "agent": "build",
                "model": "openai/gpt-5.5",
                "metadata": {"branch": "experiment-a"},
            },
            {
                "id": "ses_child_2",
                "title": "Child two",
                "directory": "/tmp/project",
                "agent": "plan",
                "model": "openai/gpt-5.5",
                "metadata": {"branch": "experiment-b"},
            },
        ]
        with LifecycleOpenCodeServer(children=children) as server:
            result = self.run_cli("children", "ses_parent", "--json", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), children)
        self.assertEqual(server.requests, [("GET", "/session/ses_parent/children", None)])

    def test_children_normalizes_session_aliases_for_compact_and_json_output(self):
        with tempfile.TemporaryDirectory() as directory:
            child = {
                "sessionID": "ses_child_1",
                "name": "Child one",
                "location": {"directory": directory},
                "agentID": "build",
                "modelID": "openai/gpt-5.5",
                "tokenUsage": {"input": 10, "output": 5},
                "time": {"created": "2026-07-02T00:00:00Z", "updated": "2026-07-02T00:00:03Z"},
            }
            with LifecycleOpenCodeServer(children=[child]) as server:
                compact_result = self.run_cli("children", "ses_parent", "--directory", directory, "--server", server.url)
                json_result = self.run_cli("children", "ses_parent", "--json", "--server", server.url)

        self.assertEqual(compact_result.returncode, 0, compact_result.stderr)
        self.assertEqual(compact_result.stderr, "")
        self.assertEqual(
            compact_result.stdout,
            f'id=ses_child_1 title="Child one" dir={directory} agent=build model=openai/gpt-5.5 '
            "cost=- tokens=15 created=2026-07-02T00:00:00Z updated=2026-07-02T00:00:03Z\n",
        )

        self.assertEqual(json_result.returncode, 0, json_result.stderr)
        self.assertEqual(json_result.stderr, "")
        payload = json.loads(json_result.stdout)
        self.assertEqual(len(payload), 1, payload)
        self.assertEqual(payload[0]["id"], "ses_child_1")
        self.assertEqual(payload[0]["directory"], directory)
        self.assertEqual(payload[0]["title"], "Child one")
        self.assertEqual(payload[0]["agent"], "build")
        self.assertEqual(payload[0]["model"], "openai/gpt-5.5")
        self.assertEqual(payload[0]["tokens"], {"input": 10, "output": 5, "total": 15})
        self.assertEqual(payload[0]["createdAt"], "2026-07-02T00:00:00Z")
        self.assertEqual(payload[0]["updatedAt"], "2026-07-02T00:00:03Z")
        self.assertEqual(payload[0]["sessionID"], "ses_child_1")
        self.assertEqual(
            server.requests,
            [("GET", "/session/ses_parent/children", None), ("GET", "/session/ses_parent/children", None)],
        )

    def test_lifecycle_commands_report_missing_sessions_consistently(self):
        cases = [
            ("abort", ("abort", "ses_missing"), ("POST", "/session/ses_missing/abort", {})),
            (
                "fork",
                ("fork", "ses_missing", "--message-id", "msg_missing"),
                ("POST", "/session/ses_missing/fork", {"messageID": "msg_missing"}),
            ),
            ("children", ("children", "ses_missing"), ("GET", "/session/ses_missing/children", None)),
        ]
        for command, args, request in cases:
            with self.subTest(command=command):
                with LifecycleOpenCodeServer() as server:
                    result = self.run_cli(*args, "--server", server.url)

                self.assertEqual(result.returncode, 69)
                self.assertEqual(result.stdout, "")
                self.assertIn("session not found", result.stderr)
                self.assertIn("ses_missing", result.stderr)
                self.assertEqual(server.requests, [request])


if __name__ == "__main__":
    unittest.main()

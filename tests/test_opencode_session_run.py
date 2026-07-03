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


class RunOpenCodeServer:
    def __init__(self, *, doc_paths=None, doc_body=None, doc_status=200, run_payload=None, reply_payload=None):
        self.doc_paths = doc_paths or {
            "/session/{sessionID}/run": {"post": {}},
            "/session/{sessionID}/reply": {"post": {}},
        }
        self.doc_body = doc_body
        self.doc_status = doc_status
        self.run_payload = run_payload or {"id": "msg_user_1", "status": "submitted"}
        self.reply_payload = reply_payload or {
            "id": "msg_assistant_1",
            "status": "completed",
            "cost": 0.015,
            "tokens": {"input": 12, "output": 8, "total": 20},
            "text": "Worker finished.",
        }
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
                if self.path == "/doc":
                    if parent.doc_status != 200:
                        self.send_error(parent.doc_status)
                        return
                    if parent.doc_body is not None:
                        self._write_body(parent.doc_body.encode("utf-8"))
                        return
                    self._write_json(
                        {
                            "openapi": "3.1.0",
                            "paths": parent.doc_paths,
                        }
                    )
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session":
                    self._write_json({"id": "ses_new", "directory": payload["directory"]})
                    return
                if self.path in ("/session/ses_existing/run", "/session/ses_new/run"):
                    self._write_json(parent.run_payload)
                    return
                if self.path in ("/session/ses_existing/reply", "/session/ses_new/reply"):
                    self._write_json(parent.reply_payload)
                    return
                self.send_error(404)

            def do_DELETE(self):
                parent.requests.append(("DELETE", self.path, None))
                if self.path == "/api/session/ses_new":
                    self._write_json({"id": "ses_new", "deleted": True})
                    return
                self.send_error(404)

            def _write_json(self, payload):
                body = json.dumps(payload).encode("utf-8")
                self._write_body(body)

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


class RunCliTest(unittest.TestCase):
    def run_cli(self, *args, input_text=None, env=None):
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=REPO_ROOT,
            env=command_env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_run_blocking_existing_session_prints_compact_terminal_reply(self):
        with RunOpenCodeServer() as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_existing",
                "--server",
                server.url,
                "Finish the worker task",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "run_blocking session=ses_existing status=done user=msg_user_1 assistant=msg_assistant_1 "
            "cost=0.015 tokens=20 text=\"Worker finished.\"\n",
        )
        self.assertEqual(
            server.requests,
            [
                ("GET", "/doc", None),
                ("POST", "/session/ses_existing/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_existing/reply", {}),
            ],
        )

    def test_run_without_session_creates_and_cleans_up_disposable_session(self):
        with tempfile.TemporaryDirectory() as directory, RunOpenCodeServer() as server:
            result = self.run_cli(
                "run_blocking",
                "--directory",
                directory,
                "--server",
                server.url,
                "Run in a disposable session",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("session=ses_new", result.stdout)
        self.assertEqual(
            server.requests,
            [
                ("GET", "/doc", None),
                ("POST", "/api/session", {"directory": directory}),
                ("POST", "/session/ses_new/run", {"message": "Run in a disposable session"}),
                ("POST", "/session/ses_new/reply", {}),
                ("DELETE", "/api/session/ses_new", None),
            ],
        )

    def test_run_missing_session_has_distinct_error(self):
        with RunOpenCodeServer() as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_missing",
                "--server",
                server.url,
                "Prompt for missing session",
            )

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("session not found", result.stderr)
        self.assertIn("ses_missing", result.stderr)
        self.assertEqual(
            server.requests,
            [
                ("GET", "/doc", None),
                ("POST", "/session/ses_missing/run", {"message": "Prompt for missing session"}),
            ],
        )

    def test_run_rejects_v2_prompt_only_server_as_unsupported_execution_route(self):
        with RunOpenCodeServer(
            doc_paths={
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/api/session/{sessionID}/wait": {"post": {}},
            }
        ) as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_existing",
                "--server",
                server.url,
                "Prompt for v2-only server",
            )

        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported route behavior", result.stderr)
        self.assertIn("v2 prompt admission is not execution", result.stderr)
        self.assertEqual(server.requests, [("GET", "/doc", None)])

    def test_run_invalid_api_response_has_distinct_api_failure(self):
        with RunOpenCodeServer(doc_body="not json") as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_existing",
                "--server",
                server.url,
                "Prompt for invalid API response",
            )

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("api failure", result.stderr)
        self.assertIn("invalid JSON", result.stderr)
        self.assertNotIn("unsupported route behavior", result.stderr)
        self.assertEqual(server.requests, [("GET", "/doc", None)])

    def test_run_doc_404_is_api_failure_not_missing_session(self):
        with RunOpenCodeServer(doc_status=404) as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_existing",
                "--server",
                server.url,
                "Prompt when docs are missing",
            )

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("api failure", result.stderr)
        self.assertIn("GET /doc failed: HTTP 404", result.stderr)
        self.assertNotIn("session not found", result.stderr)
        self.assertEqual(server.requests, [("GET", "/doc", None)])

    def test_run_failed_by_provider_has_distinct_error(self):
        with RunOpenCodeServer(
            run_payload={"id": "msg_user_1", "status": "failed", "error": "provider rejected request"}
        ) as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_existing",
                "--server",
                server.url,
                "Finish the worker task",
            )

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("provider failure", result.stderr)
        self.assertIn("provider rejected request", result.stderr)
        self.assertEqual(
            server.requests,
            [
                ("GET", "/doc", None),
                ("POST", "/session/ses_existing/run", {"message": "Finish the worker task"}),
            ],
        )

    def test_run_failed_assistant_reply_has_distinct_provider_error(self):
        with RunOpenCodeServer(
            reply_payload={
                "id": "msg_assistant_1",
                "status": "failed",
                "error": {"message": "provider timed out while replying"},
            }
        ) as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_existing",
                "--server",
                server.url,
                "Finish the worker task",
            )

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("provider failure", result.stderr)
        self.assertIn("provider timed out while replying", result.stderr)
        self.assertEqual(
            server.requests,
            [
                ("GET", "/doc", None),
                ("POST", "/session/ses_existing/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_existing/reply", {}),
            ],
        )

    def test_run_reads_prompt_from_stdin_when_arguments_are_omitted(self):
        with RunOpenCodeServer() as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_existing",
                "--server",
                server.url,
                input_text="Prompt from stdin\n",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            server.requests,
            [
                ("GET", "/doc", None),
                ("POST", "/session/ses_existing/run", {"message": "Prompt from stdin"}),
                ("POST", "/session/ses_existing/reply", {}),
            ],
        )

    def test_run_blocking_json_output_includes_paths_fallback_and_terminal_state(self):
        with RunOpenCodeServer() as server:
            result = self.run_cli(
                "run_blocking",
                "--session",
                "ses_existing",
                "--json",
                "--server",
                server.url,
                "Finish the worker task",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "session_id": "ses_existing",
                "message_ids": {"user": "msg_user_1", "assistant": "msg_assistant_1"},
                "status": "done",
                "raw_status": "completed",
                "terminal_state": "done",
                "api_path": {
                    "run": "/session/{sessionID}/run",
                    "reply": "/session/{sessionID}/reply",
                },
                "fallback": {"available": True, "strategy": "legacy_run_reply", "used": True},
                "cost": 0.015,
                "tokens": {"input": 12, "output": 8, "total": 20},
                "text": "Worker finished.",
            },
        )
        self.assertEqual(
            server.requests,
            [
                ("GET", "/doc", None),
                ("POST", "/session/ses_existing/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_existing/reply", {}),
            ],
        )


if __name__ == "__main__":
    unittest.main()

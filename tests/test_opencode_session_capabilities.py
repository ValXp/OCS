import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "opencode-session"


class MockOpenCodeServer:
    def __init__(self, *, health=None, doc=None, health_path="/global/health"):
        self.health = health or {"status": "ok", "version": "1.2.3"}
        self.doc = doc or {"openapi": "3.1.0", "paths": {}}
        self.health_path = health_path
        self.server = None
        self.thread = None

    def __enter__(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                if self.path == parent.health_path:
                    self._write_json(parent.health)
                    return
                if self.path == "/doc":
                    self._write_json(parent.doc)
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
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class CapabilityProbeCliTest(unittest.TestCase):
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

    def test_compact_summary_reports_detected_paths(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/api/session/{sessionID}/wait": {"post": {}},
                "/api/event": {"get": {}},
            },
        }

        with MockOpenCodeServer(doc=doc) as server_url:
            result = self.run_cli("capabilities", "--server", server_url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout.strip(),
            "health=ok version=1.2.3 session=/api/session prompt=/api/session/{sessionID}/prompt "
            "wait=/api/session/{sessionID}/wait events=/api/event legacy=unsupported",
        )

    def test_json_output_exposes_capability_contract(self):
        doc = {
            "openapi": "3.1.0",
            "paths": {
                "/api/session": {"get": {}, "post": {}},
                "/api/session/{sessionID}/prompt": {"post": {}},
                "/api/session/{sessionID}/wait": {"post": {}},
                "/api/event": {"get": {}},
                "/session/{sessionID}/run": {"post": {}},
                "/session/{sessionID}/reply": {"post": {}},
            },
        }
        health = {"status": "ok", "version": "2.0.0"}

        with MockOpenCodeServer(health=health, doc=doc) as server_url:
            result = self.run_cli("capabilities", "--server", server_url, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["health"], "ok")
        self.assertEqual(payload["version"], "2.0.0")
        self.assertTrue(payload["v2_prompt_support"])
        self.assertTrue(payload["v2_wait_support"])
        self.assertTrue(payload["event_support"])
        self.assertTrue(payload["legacy_fallback_available"])
        self.assertEqual(
            payload["route_availability"],
            {
                "session": {"path": "/api/session", "method": "POST", "available": True},
                "v2_prompt": {
                    "path": "/api/session/{sessionID}/prompt",
                    "method": "POST",
                    "available": True,
                },
                "v2_wait": {
                    "path": "/api/session/{sessionID}/wait",
                    "method": "POST",
                    "available": True,
                },
                "events": {"path": "/api/event", "method": "GET", "available": True},
                "legacy_run": {
                    "path": "/session/{sessionID}/run",
                    "method": "POST",
                    "available": True,
                },
                "legacy_reply": {
                    "path": "/session/{sessionID}/reply",
                    "method": "POST",
                    "available": True,
                },
            },
        )

    def test_unsupported_server_has_stable_exit_and_clear_error(self):
        doc = {"openapi": "3.1.0", "paths": {"/unrelated": {"get": {}}}}

        with MockOpenCodeServer(doc=doc) as server_url:
            result = self.run_cli("capabilities", "--server", server_url)

        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported OpenCode server", result.stderr)
        self.assertIn("missing session control: POST /api/session or POST /session", result.stderr)
        self.assertIn(
            "missing prompt admission: POST /api/session/{sessionID}/prompt or legacy POST /session/{sessionID}/run + POST /session/{sessionID}/reply",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()

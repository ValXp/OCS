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


class OrchestrationOpenCodeServer:
    def __init__(self, *, events=None, run_payload=None, reply_payload=None, session_ids=None):
        self.events = events or []
        self.session_ids = session_ids or {"ses_new"}
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
                if self.path == "/global/health":
                    self._write_json({"status": "ok", "version": "2.0.0"})
                    return
                if self.path == "/doc":
                    self._write_json(
                        {
                            "openapi": "3.1.0",
                            "paths": {
                                "/api/session": {"get": {}, "post": {}},
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
                    for event in parent.events:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session":
                    self._write_json({"id": "ses_new", "directory": payload["directory"]})
                    return
                for session_id in parent.session_ids:
                    if self.path == f"/session/{session_id}/run":
                        self._write_json(parent.run_payload)
                        return
                    if self.path == f"/session/{session_id}/reply":
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


class SingleRunOrchestrationCliTest(unittest.TestCase):
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

    def test_start_named_run_creates_session_streams_progress_and_persists_success(self):
        events = [
            {
                "type": "session.prompt.admitted",
                "properties": {
                    "sessionID": "ses_new",
                    "messageID": "msg_user_1",
                    "delivery": "run",
                    "state": "admitted",
                },
            },
            {"type": "session.status", "properties": {"sessionID": "ses_new", "status": "completed"}},
        ]

        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with OrchestrationOpenCodeServer(events=events) as server:
                result = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("admission session=ses_new message=msg_user_1 delivery=run status=queued\n", result.stdout)
        self.assertIn("status session=ses_new status=done\n", result.stdout)
        self.assertIn("run=demo status=done", result.stdout)
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "done")
        self.assertNotIn("transcript", payload)
        worker = payload["workers"]["worker"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["role"], "worker")
        self.assertEqual(worker["prompt_ids"], ["msg_user_1"])
        self.assertEqual(worker["output_refs"], ["assistant:msg_assistant_1"])
        self.assertEqual(
            worker["result"],
            {
                "session_id": "ses_new",
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
        self.assertEqual(payload["output_refs"], ["worker:msg_assistant_1"])
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"directory": directory}),
                ("POST", "/session/ses_new/run", {"message": "Finish the worker task"}),
                ("GET", "/api/event", None),
                ("POST", "/session/ses_new/reply", {}),
            ],
        )

    def test_start_persists_failed_state_and_prompt_reference_on_provider_failure(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with OrchestrationOpenCodeServer(
                run_payload={"id": "msg_user_1", "status": "failed", "error": "provider rejected request"}
            ) as server:
                result = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, "")
        self.assertIn("provider failure", result.stderr)
        self.assertIn("provider rejected request", result.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "failed")
        worker = payload["workers"]["worker"]
        self.assertEqual(worker["status"], "failed")
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["prompt_ids"], ["msg_user_1"])
        self.assertEqual(worker["output_refs"], [])
        self.assertEqual(worker["error"], "provider rejected request")
        self.assertNotIn("result", worker)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"directory": directory}),
                ("POST", "/session/ses_new/run", {"message": "Finish the worker task"}),
            ],
        )

    def test_collect_returns_stored_compact_worker_result_without_server(self):
        events = [{"type": "session.status", "properties": {"sessionID": "ses_new", "status": "completed"}}]

        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with OrchestrationOpenCodeServer(events=events) as server:
                start = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--prompt",
                    "Finish the worker task",
                )
            collect = self.run_cli("run", "--store", store, "collect", "demo")

        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(collect.returncode, 0, collect.stderr)
        self.assertEqual(collect.stderr, "")
        self.assertEqual(
            collect.stdout,
            "run_blocking session=ses_new status=done user=msg_user_1 assistant=msg_assistant_1 "
            "cost=0.015 tokens=20 text=\"Worker finished.\"\n",
        )

    def test_start_attaches_session_then_reloads_it_from_store_on_restart(self):
        events = [{"type": "session.status", "properties": {"sessionID": "ses_existing", "status": "completed"}}]

        with tempfile.TemporaryDirectory() as store:
            with OrchestrationOpenCodeServer(events=events, session_ids={"ses_existing"}) as server:
                first = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--server",
                    server.url,
                    "--session",
                    "ses_existing",
                    "--prompt",
                    "First prompt",
                )
                second = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--server",
                    server.url,
                    "--prompt",
                    "Second prompt",
                )
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        worker = json.loads(status.stdout)["workers"]["worker"]
        self.assertEqual(worker["session_id"], "ses_existing")
        self.assertEqual(worker["status"], "done")
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/session/ses_existing/run", {"message": "First prompt"}),
                ("GET", "/api/event", None),
                ("POST", "/session/ses_existing/reply", {}),
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/session/ses_existing/run", {"message": "Second prompt"}),
                ("GET", "/api/event", None),
                ("POST", "/session/ses_existing/reply", {}),
            ],
        )

    def test_start_with_cleanup_deletes_created_disposable_session_and_records_cleanup(self):
        events = [{"type": "session.status", "properties": {"sessionID": "ses_new", "status": "completed"}}]

        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with OrchestrationOpenCodeServer(events=events) as server:
                result = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                    "--cleanup",
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        worker = json.loads(status.stdout)["workers"]["worker"]
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["cleanup"], {"requested": True, "deleted": True})
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"directory": directory}),
                ("POST", "/session/ses_new/run", {"message": "Finish the worker task"}),
                ("GET", "/api/event", None),
                ("POST", "/session/ses_new/reply", {}),
                ("DELETE", "/api/session/ses_new", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class OrchestrationOpenCodeServer:
    def __init__(
        self,
        *,
        events=None,
        run_payload=None,
        reply_payload=None,
        message_payload=None,
        session_ids=None,
        modern_message=False,
    ):
        self.events = events or []
        self.session_ids = session_ids or {"ses_new"}
        self.modern_message = modern_message
        self.run_payload = run_payload or {"id": "msg_user_1", "status": "submitted"}
        self.reply_payload = reply_payload or {
            "id": "msg_assistant_1",
            "status": "completed",
            "cost": 0.015,
            "tokens": {"input": 12, "output": 8, "total": 20},
            "text": "Worker finished.",
        }
        self.message_payload = message_payload or {
            "info": {
                "id": "msg_assistant_modern_1",
                "sessionID": "ses_new",
                "role": "assistant",
                "cost": 0.02,
                "tokens": {"input": 3, "output": 2, "total": 5},
            },
            "parts": [{"type": "text", "text": "Modern worker finished."}],
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
                    paths = {
                        "/api/session": {"get": {}, "post": {}},
                        "/api/event": {"get": {}},
                    }
                    if parent.modern_message:
                        paths["/session/{sessionID}/message"] = {"post": {}}
                    else:
                        paths["/session/{sessionID}/run"] = {"post": {}}
                        paths["/session/{sessionID}/reply"] = {"post": {}}
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
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session":
                    self._write_json({"id": "ses_new", "directory": _payload_directory(payload)})
                    return
                for session_id in parent.session_ids:
                    if self.path == f"/session/{session_id}/run":
                        self._write_json(parent.run_payload)
                        return
                    if self.path == f"/session/{session_id}/reply":
                        self._write_json(parent.reply_payload)
                        return
                    if self.path == f"/session/{session_id}/message":
                        self._write_json(parent.message_payload)
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
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    return

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


class RetryPolicyOpenCodeServer:
    def __init__(self, *, run_payloads, reply_payloads=None, session_id="ses_retry"):
        self.run_payloads = list(run_payloads)
        self.reply_payloads = list(
            reply_payloads
            or [
                {
                    "id": "msg_assistant_1",
                    "status": "completed",
                    "cost": 0.015,
                    "tokens": {"total": 20},
                    "text": "Worker finished after retry.",
                }
            ]
        )
        self.session_id = session_id
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
                                "/session/{sessionID}/run": {"post": {}},
                                "/session/{sessionID}/reply": {"post": {}},
                            },
                        }
                    )
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session":
                    self._write_json({"id": parent.session_id, "directory": _payload_directory(payload)})
                    return
                if self.path == f"/session/{parent.session_id}/run":
                    self._write_retry_payload(parent.run_payloads.pop(0))
                    return
                if self.path == f"/session/{parent.session_id}/reply":
                    self._write_retry_payload(parent.reply_payloads.pop(0))
                    return
                self.send_error(404)

            def _write_retry_payload(self, payload):
                status = 200
                if isinstance(payload, tuple) and payload and payload[0] == "sleep":
                    _, delay, payload = payload
                    time.sleep(delay)
                if isinstance(payload, tuple):
                    status, payload = payload
                self._write_json(payload, status=status)

            def _write_json(self, payload, *, status=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    return

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


class MultiWorkerOrchestrationServer:
    def __init__(self, *, session_ids=None, run_payloads=None, reply_payloads=None):
        self.session_ids = session_ids or ["ses_docs", "ses_plan"]
        self.run_payloads = run_payloads or {
            "ses_plan": {"id": "msg_plan_user", "status": "submitted"},
            "ses_docs": {"id": "msg_docs_user", "status": "submitted"},
        }
        self.reply_payloads = reply_payloads or {
            "ses_plan": {
                "id": "msg_plan_assistant",
                "status": "completed",
                "cost": 0.01,
                "tokens": {"input": 8, "output": 4, "total": 12},
                "text": "Plan ready.",
            },
            "ses_docs": {
                "id": "msg_docs_assistant",
                "status": "completed",
                "cost": 0.02,
                "tokens": {"input": 10, "output": 7, "total": 17},
                "text": "Docs ready.",
            },
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
                                "/session/{sessionID}/run": {"post": {}},
                                "/session/{sessionID}/reply": {"post": {}},
                            },
                        }
                    )
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session":
                    session_id = parent.session_ids.pop(0)
                    response = {"id": session_id, "directory": _payload_directory(payload)}
                    self._write_json(response)
                    return
                for session_id, run_payload in parent.run_payloads.items():
                    if self.path == f"/session/{session_id}/run":
                        self._write_json(run_payload)
                        return
                    if self.path == f"/session/{session_id}/reply":
                        self._write_json(parent.reply_payloads[session_id])
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


class WorkerControlOpenCodeServer:
    def __init__(self, *, prompt_response=None, prompt_status=200, abort_response=None):
        self.prompt_response = prompt_response or {}
        self.prompt_status = prompt_status
        self.abort_response = abort_response or {}
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
                                "/session/{sessionID}/abort": {"post": {}},
                            },
                        }
                    )
                    return
                self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                payload = json.loads(body or "{}")
                parent.requests.append(("POST", self.path, payload))
                if self.path == "/api/session/ses_plan/prompt":
                    self._write_json(parent.prompt_response, status=parent.prompt_status)
                    return
                if self.path == "/session/ses_plan/abort":
                    self._write_json(parent.abort_response)
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

    def test_start_named_run_creates_session_and_persists_success(self):
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
        self.assertIn("run=demo status=done", result.stdout)
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "done")
        self.assertNotIn("transcript", payload)
        worker = payload["workers"]["worker"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["session_id"], "ses_new")
        self.assertEqual(worker["role"], "worker")
        self.assertEqual(worker["prompt"], "Finish the worker task")
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
                "execution_strategy": "legacy_run_reply",
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
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_new/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_new/reply", {}),
            ],
        )

    def test_start_named_run_uses_modern_session_message_when_available(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with OrchestrationOpenCodeServer(modern_message=True) as server:
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
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        worker = payload["workers"]["worker"]
        self.assertEqual(worker["status"], "done")
        self.assertEqual(worker["prompt_ids"], [worker["result"]["message_ids"]["user"]])
        self.assertTrue(worker["prompt_ids"][0].startswith("msg_"))
        self.assertEqual(worker["output_refs"], ["assistant:msg_assistant_modern_1"])
        self.assertEqual(worker["result"]["execution_strategy"], "session_message")
        self.assertEqual(worker["result"]["text"], "Modern worker finished.")
        self.assertEqual(payload["output_refs"], ["worker:msg_assistant_modern_1"])
        self.assertEqual(
            [(method, path) for method, path, _payload in requests],
            [("GET", "/global/health"), ("GET", "/doc"), ("POST", "/api/session"), ("POST", "/session/ses_new/message")],
        )
        self.assertEqual(requests[3][2]["parts"], [{"type": "text", "text": "Finish the worker task"}])
        self.assertTrue(requests[3][2]["messageID"].startswith("msg_"))

    def test_start_executes_each_ready_worker_through_blocking_executor(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with MultiWorkerOrchestrationServer() as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                planner = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--prompt",
                    "Create the implementation plan",
                    "--agent",
                    "plan",
                    "--model",
                    "openai/gpt-5.5",
                )
                docs = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "docs",
                    "--role",
                    "write",
                    "--prompt",
                    "Draft the release notes",
                    "--agent",
                    "build",
                    "--model",
                    "openai/gpt-5.5-mini",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(planner.returncode, 0, planner.stderr)
        self.assertEqual(docs.returncode, 0, docs.stderr)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                (
                    "POST",
                    "/api/session",
                    {"location": {"directory": directory}, "agent": "build", "model": "openai/gpt-5.5-mini"},
                ),
                (
                    "POST",
                    "/api/session",
                    {"location": {"directory": directory}, "agent": "plan", "model": "openai/gpt-5.5"},
                ),
                ("POST", "/session/ses_docs/run", {"message": "Draft the release notes"}),
                ("POST", "/session/ses_docs/reply", {}),
                ("POST", "/session/ses_plan/run", {"message": "Create the implementation plan"}),
                ("POST", "/session/ses_plan/reply", {}),
            ],
        )
        self.assertIn("run=demo status=done", start.stdout)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["output_refs"], ["docs:msg_docs_assistant", "planner:msg_plan_assistant"])
        self.assertEqual(payload["workers"]["planner"]["status"], "done")
        self.assertEqual(payload["workers"]["planner"]["session_id"], "ses_plan")
        self.assertEqual(payload["workers"]["planner"]["prompt_ids"], ["msg_plan_user"])
        self.assertEqual(payload["workers"]["planner"]["output_refs"], ["assistant:msg_plan_assistant"])
        self.assertEqual(payload["workers"]["planner"]["result"]["text"], "Plan ready.")
        self.assertEqual(payload["workers"]["docs"]["status"], "done")
        self.assertEqual(payload["workers"]["docs"]["session_id"], "ses_docs")
        self.assertEqual(payload["workers"]["docs"]["prompt_ids"], ["msg_docs_user"])
        self.assertEqual(payload["workers"]["docs"]["output_refs"], ["assistant:msg_docs_assistant"])
        self.assertEqual(payload["workers"]["docs"]["result"]["text"], "Docs ready.")

    def test_start_blocks_dependent_worker_when_prerequisite_fails(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with MultiWorkerOrchestrationServer(
                session_ids=["ses_build"],
                run_payloads={"ses_build": {"id": "msg_build_user", "status": "failed", "error": "tests failed"}},
                reply_payloads={},
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                build = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "build",
                    "--role",
                    "build",
                    "--prompt",
                    "Run the implementation",
                )
                review = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "review",
                    "--role",
                    "review",
                    "--prompt",
                    "Review the implementation",
                    "--depends-on",
                    "build",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertEqual(review.returncode, 0, review.stderr)
        self.assertEqual(start.returncode, 69)
        self.assertIn("provider failure", start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_build/run", {"message": "Run the implementation"}),
            ],
        )
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["output_refs"], [])
        self.assertEqual(payload["workers"]["build"]["status"], "failed")
        self.assertEqual(payload["workers"]["build"]["error"], "tests failed")
        self.assertEqual(payload["workers"]["review"]["status"], "blocked")
        self.assertEqual(payload["workers"]["review"]["session_id"], None)
        self.assertEqual(payload["workers"]["review"]["blockers"], ["dependency:build"])

    def test_start_persists_dependency_blocking_when_prerequisite_is_already_failed(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with MultiWorkerOrchestrationServer() as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                build = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "build",
                    "--role",
                    "build",
                    "--prompt",
                    "Run the implementation",
                    "--status",
                    "failed",
                )
                review = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "review",
                    "--role",
                    "review",
                    "--prompt",
                    "Review the implementation",
                    "--depends-on",
                    "build",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(build.returncode, 0, build.stderr)
        self.assertEqual(review.returncode, 0, review.stderr)
        self.assertEqual(start.returncode, 69)
        self.assertIn("run=demo status=failed", start.stdout)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertFalse(any(method == "POST" for method, _path, _payload in requests))
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["workers"]["build"]["status"], "failed")
        self.assertEqual(payload["workers"]["review"]["status"], "blocked")
        self.assertEqual(payload["workers"]["review"]["blockers"], ["dependency:build"])
        self.assertEqual(payload["workers"]["review"]["next_eligible_action"], "resolve_blocker")

    def test_start_returns_partial_failure_exit_code_when_some_workers_complete_before_failure(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with MultiWorkerOrchestrationServer(
                session_ids=["ses_docs", "ses_plan"],
                run_payloads={
                    "ses_docs": {"id": "msg_docs_user", "status": "submitted"},
                    "ses_plan": {"id": "msg_plan_user", "status": "submitted"},
                },
                reply_payloads={
                    "ses_docs": {
                        "id": "msg_docs_assistant",
                        "status": "completed",
                        "cost": 0.02,
                        "tokens": {"total": 17},
                        "text": "Docs ready.",
                    },
                    "ses_plan": {"id": "msg_plan_assistant", "status": "failed", "error": "planner failed"},
                },
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                docs = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "docs",
                    "--role",
                    "write",
                    "--prompt",
                    "Draft the release notes",
                )
                planner = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--prompt",
                    "Create the implementation plan",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(docs.returncode, 0, docs.stderr)
        self.assertEqual(planner.returncode, 0, planner.stderr)
        self.assertEqual(start.returncode, 1)
        self.assertIn("planner failed", start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["output_refs"], ["docs:msg_docs_assistant"])
        self.assertEqual(payload["workers"]["docs"]["status"], "done")
        self.assertEqual(payload["workers"]["planner"]["status"], "failed")
        self.assertEqual(payload["workers"]["planner"]["failure_category"], "provider")
        self.assertEqual(payload["workers"]["planner"]["failure_reason"], "planner failed")

    def test_start_retries_retryable_provider_failure_and_persists_success_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with RetryPolicyOpenCodeServer(
                run_payloads=[
                    {"id": "msg_user_failed", "status": "failed", "error": "transient provider outage"},
                    {"id": "msg_user_retry", "status": "submitted"},
                ]
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "worker",
                    "--role",
                    "worker",
                    "--prompt",
                    "Finish the worker task",
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "provider",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/reply", {}),
            ],
        )
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "done")
        retry_worker = payload["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["provider"])
        self.assertEqual(retry_worker["last_failure_category"], "provider")
        self.assertEqual(retry_worker["last_failure_reason"], "transient provider outage")
        self.assertIsNone(retry_worker["failure_reason"])
        self.assertEqual(retry_worker["next_eligible_action"], "collect")
        self.assertEqual(retry_worker["result"]["message_ids"], {"user": "msg_user_retry", "assistant": "msg_assistant_1"})

    def test_start_stops_after_retry_exhaustion_and_records_failure_reason(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with RetryPolicyOpenCodeServer(
                run_payloads=[
                    {"id": "msg_user_failed_1", "status": "failed", "error": "provider temporarily unavailable"},
                    {"id": "msg_user_failed_2", "status": "failed", "error": "provider still unavailable"},
                ]
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "worker",
                    "--role",
                    "worker",
                    "--prompt",
                    "Finish the worker task",
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "provider",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(start.returncode, 69)
        self.assertEqual(start.stdout, "")
        self.assertIn("provider still unavailable", start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
            ],
        )
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "failed")
        retry_worker = payload["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "failed")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["provider"])
        self.assertEqual(retry_worker["prompt_ids"], ["msg_user_failed_2"])
        self.assertEqual(retry_worker["failure_category"], "provider")
        self.assertEqual(retry_worker["failure_reason"], "provider still unavailable")
        self.assertEqual(retry_worker["last_failure_category"], "provider")
        self.assertEqual(retry_worker["last_failure_reason"], "provider still unavailable")
        self.assertEqual(retry_worker["next_eligible_action"], "none")

    def test_start_retries_retryable_api_failure_and_persists_success_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with RetryPolicyOpenCodeServer(
                run_payloads=[
                    (503, {"error": "upstream overloaded"}),
                    {"id": "msg_user_retry", "status": "submitted"},
                ]
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "worker",
                    "--role",
                    "worker",
                    "--prompt",
                    "Finish the worker task",
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "api",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/reply", {}),
            ],
        )
        retry_worker = json.loads(status.stdout)["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["api"])
        self.assertEqual(retry_worker["last_failure_category"], "api")
        self.assertIn("HTTP 503", retry_worker["last_failure_reason"])
        self.assertEqual(retry_worker["next_eligible_action"], "collect")

    def test_start_prompt_uses_stored_worker_retry_policy(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with RetryPolicyOpenCodeServer(
                run_payloads=[
                    {"id": "msg_user_failed", "status": "failed", "error": "transient provider outage"},
                    {"id": "msg_user_retry", "status": "submitted"},
                ]
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "worker",
                    "--role",
                    "worker",
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "provider",
                )
                start = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "start",
                    "demo",
                    "--prompt",
                    "Finish the worker task",
                )
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/reply", {}),
            ],
        )
        retry_worker = json.loads(status.stdout)["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["last_failure_category"], "provider")
        self.assertEqual(retry_worker["next_eligible_action"], "collect")

    def test_start_retries_retryable_timeout_and_persists_success_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with RetryPolicyOpenCodeServer(
                run_payloads=[
                    ("sleep", 2, {"id": "msg_user_late", "status": "submitted"}),
                    {"id": "msg_user_retry", "status": "submitted"},
                ]
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "worker",
                    "--role",
                    "worker",
                    "--prompt",
                    "Finish the worker task",
                    "--timeout-seconds",
                    "1",
                    "--retry-limit",
                    "1",
                    "--retryable",
                    "timeout",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_retry/reply", {}),
            ],
        )
        retry_worker = json.loads(status.stdout)["workers"]["worker"]
        self.assertEqual(retry_worker["status"], "done")
        self.assertEqual(retry_worker["retry_count"], 1)
        self.assertEqual(retry_worker["retry_limit"], 1)
        self.assertEqual(retry_worker["retryable_failures"], ["timeout"])
        self.assertEqual(retry_worker["last_failure_category"], "timeout")
        self.assertEqual(retry_worker["last_failure_reason"], "worker timed out after 1s")
        self.assertEqual(retry_worker["next_eligible_action"], "collect")
        self.assertEqual(retry_worker["result"]["message_ids"], {"user": "msg_user_retry", "assistant": "msg_assistant_1"})

    def test_start_times_out_stuck_worker_and_records_timeout_metadata(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with RetryPolicyOpenCodeServer(
                run_payloads=[("sleep", 2, {"id": "msg_user_late", "status": "submitted"})]
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "worker",
                    "--role",
                    "worker",
                    "--prompt",
                    "Finish the worker task",
                    "--timeout-seconds",
                    "1",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(start.returncode, 124)
        self.assertEqual(start.stdout, "")
        self.assertIn("timed out after 1s", start.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_retry/run", {"message": "Finish the worker task"}),
            ],
        )
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "timeout")
        timeout_worker = payload["workers"]["worker"]
        self.assertEqual(timeout_worker["status"], "timeout")
        self.assertEqual(timeout_worker["timeout_seconds"], 1)
        self.assertEqual(timeout_worker["timeout_policy"], "timeout")
        self.assertIsNotNone(timeout_worker["timeout_started_at"])
        self.assertIsNotNone(timeout_worker["timed_out_at"])
        self.assertEqual(timeout_worker["failure_category"], "timeout")
        self.assertEqual(timeout_worker["failure_reason"], "worker timed out after 1s")
        self.assertEqual(timeout_worker["next_eligible_action"], "none")
        self.assertNotIn("result", timeout_worker)

    def test_timeout_policy_maps_timed_out_worker_to_declared_terminal_status(self):
        expectations = {
            "blocked": (75, "blocked", "resolve_blocker", ["timeout"]),
            "failed": (69, "failed", "none", []),
            "aborted": (130, "aborted", "none", []),
        }
        for policy, (exit_code, expected_status, next_action, blockers) in expectations.items():
            with self.subTest(policy=policy):
                with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
                    with RetryPolicyOpenCodeServer(
                        run_payloads=[("sleep", 2, {"id": "msg_user_late", "status": "submitted"})]
                    ) as server:
                        init = self.run_cli(
                            "run",
                            "--store",
                            store,
                            "init",
                            "demo",
                            "--directory",
                            directory,
                            "--server",
                            server.url,
                        )
                        worker = self.run_cli(
                            "run",
                            "--store",
                            store,
                            "worker",
                            "demo",
                            "worker",
                            "--role",
                            "worker",
                            "--prompt",
                            "Finish the worker task",
                            "--timeout-seconds",
                            "1",
                            "--timeout-policy",
                            policy,
                        )
                        start = self.run_cli("run", "--store", store, "start", "demo")
                    status = self.run_cli("run", "--store", store, "status", "demo", "--json")

                self.assertEqual(init.returncode, 0, init.stderr)
                self.assertEqual(worker.returncode, 0, worker.stderr)
                self.assertEqual(start.returncode, exit_code)
                self.assertEqual(status.returncode, 0, status.stderr)
                payload = json.loads(status.stdout)
                self.assertEqual(payload["status"], expected_status)
                timeout_worker = payload["workers"]["worker"]
                self.assertEqual(timeout_worker["status"], expected_status)
                self.assertEqual(timeout_worker["timeout_policy"], policy)
                self.assertEqual(timeout_worker["failure_category"], "timeout")
                self.assertEqual(timeout_worker["failure_reason"], "worker timed out after 1s")
                self.assertEqual(timeout_worker["next_eligible_action"], next_action)
                self.assertEqual(timeout_worker["blockers"], blockers)

    def test_start_prompt_returns_aborted_exit_code_when_worker_result_is_aborted(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with OrchestrationOpenCodeServer(
                reply_payload={"id": "msg_abort", "status": "aborted", "error": "user aborted run"}
            ) as server:
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
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(start.returncode, 130)
        self.assertEqual(start.stderr, "")
        self.assertIn("run=demo status=aborted", start.stdout)
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "aborted")
        self.assertEqual(payload["workers"]["worker"]["status"], "aborted")
        self.assertEqual(payload["workers"]["worker"]["result"]["terminal_state"], "aborted")
        self.assertEqual(payload["workers"]["worker"]["next_eligible_action"], "none")

    def test_collect_prints_completed_worker_outputs_in_dependency_order(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with MultiWorkerOrchestrationServer(
                session_ids=["ses_plan", "ses_review"],
                run_payloads={
                    "ses_plan": {"id": "msg_plan_user", "status": "submitted"},
                    "ses_review": {"id": "msg_review_user", "status": "submitted"},
                },
                reply_payloads={
                    "ses_plan": {
                        "id": "msg_plan_assistant",
                        "status": "completed",
                        "cost": 0.01,
                        "tokens": {"total": 12},
                        "text": "Plan ready.",
                    },
                    "ses_review": {
                        "id": "msg_review_assistant",
                        "status": "completed",
                        "cost": 0.03,
                        "tokens": {"total": 15},
                        "text": "Review done.",
                    },
                },
            ) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                plan = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "plan",
                    "--role",
                    "plan",
                    "--prompt",
                    "Plan the work",
                )
                review = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "review",
                    "--role",
                    "review",
                    "--prompt",
                    "Review the plan",
                    "--depends-on",
                    "plan",
                )
                start = self.run_cli("run", "--store", store, "start", "demo")
                requests = list(server.requests)
            collect = self.run_cli("run", "--store", store, "collect", "demo")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(review.returncode, 0, review.stderr)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(collect.returncode, 0, collect.stderr)
        self.assertEqual(collect.stderr, "")
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_plan/run", {"message": "Plan the work"}),
                ("POST", "/session/ses_plan/reply", {}),
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_review/run", {"message": "Review the plan"}),
                ("POST", "/session/ses_review/reply", {}),
            ],
        )
        self.assertEqual(
            collect.stdout,
            "worker=plan role=plan session=ses_plan status=done user=msg_plan_user "
            "assistant=msg_plan_assistant cost=0.01 tokens=12 text=\"Plan ready.\"\n"
            "worker=review role=review session=ses_review status=done user=msg_review_user "
            "assistant=msg_review_assistant cost=0.03 tokens=15 text=\"Review done.\"\n",
        )

    def test_run_steer_targets_individual_worker_session_and_records_prompt(self):
        prompt_response = {
            "sessionID": "ses_plan",
            "messageID": "msg_steer_1",
            "delivery": "steer",
            "state": "admitted",
            "admittedSequence": 4,
        }
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with WorkerControlOpenCodeServer(prompt_response=prompt_response) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--session",
                    "ses_plan",
                    "--status",
                    "active",
                )
                steer = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "steer",
                    "demo",
                    "planner",
                    "Incorporate the review feedback",
                    "--message-id",
                    "msg_steer_1",
                )
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(steer.returncode, 0, steer.stderr)
        self.assertEqual(steer.stderr, "")
        self.assertEqual(
            steer.stdout,
            "run=demo worker=planner steer session=ses_plan message=msg_steer_1 "
            "delivery=steer status=queued admitted=4 promoted=-\n",
        )
        self.assertEqual(
            requests,
            [
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                (
                    "POST",
                    "/api/session/ses_plan/prompt",
                    {
                        "id": "msg_steer_1",
                        "prompt": {"text": "Incorporate the review feedback"},
                        "delivery": "steer",
                    },
                ),
            ],
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["workers"]["planner"]["status"], "active")
        self.assertEqual(payload["workers"]["planner"]["prompt_ids"], ["msg_steer_1"])

    def test_run_steer_treats_matching_replay_as_admitted_prompt(self):
        prompt_response = {
            "sessionID": "ses_plan",
            "messageID": "msg_repeat_1",
            "delivery": "queue",
            "state": "admitted",
            "admittedSequence": 7,
            "duplicate": True,
        }
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with WorkerControlOpenCodeServer(prompt_response=prompt_response, prompt_status=409) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--session",
                    "ses_plan",
                    "--status",
                    "active",
                )
                steer = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "steer",
                    "demo",
                    "planner",
                    "Queue this without duplicating it",
                    "--delivery",
                    "queue",
                    "--message-id",
                    "msg_repeat_1",
                )
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(steer.returncode, 0, steer.stderr)
        self.assertEqual(steer.stderr, "")
        self.assertEqual(
            steer.stdout,
            "run=demo worker=planner steer session=ses_plan message=msg_repeat_1 "
            "delivery=queue status=queued admitted=7 promoted=-\n",
        )
        payload = json.loads(status.stdout)
        self.assertEqual(payload["workers"]["planner"]["prompt_ids"], ["msg_repeat_1"])

    def test_run_abort_targets_individual_worker_session_and_marks_worker_aborted(self):
        abort_response = {"sessionID": "ses_plan", "accepted": True, "status": "aborted"}
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with WorkerControlOpenCodeServer(abort_response=abort_response) as server:
                init = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "init",
                    "demo",
                    "--directory",
                    directory,
                    "--server",
                    server.url,
                )
                worker = self.run_cli(
                    "run",
                    "--store",
                    store,
                    "worker",
                    "demo",
                    "planner",
                    "--role",
                    "plan",
                    "--session",
                    "ses_plan",
                    "--status",
                    "active",
                )
                abort = self.run_cli("run", "--store", store, "abort", "demo", "planner")
                requests = list(server.requests)
            status = self.run_cli("run", "--store", store, "status", "demo", "--json")

        self.assertEqual(init.returncode, 0, init.stderr)
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(abort.returncode, 0, abort.stderr)
        self.assertEqual(abort.stderr, "")
        self.assertEqual(abort.stdout, "run=demo worker=planner abort session=ses_plan accepted=true status=aborted\n")
        self.assertEqual(requests, [("POST", "/session/ses_plan/abort", {})])
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["status"], "aborted")
        self.assertEqual(payload["workers"]["planner"]["status"], "aborted")

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
                ("POST", "/api/session", {"location": {"directory": directory}}),
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
        with tempfile.TemporaryDirectory() as store:
            with OrchestrationOpenCodeServer(session_ids={"ses_existing"}) as server:
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
                ("POST", "/session/ses_existing/reply", {}),
                ("GET", "/global/health", None),
                ("GET", "/doc", None),
                ("POST", "/session/ses_existing/run", {"message": "Second prompt"}),
                ("POST", "/session/ses_existing/reply", {}),
            ],
        )

    def test_start_with_cleanup_deletes_created_disposable_session_and_records_cleanup(self):
        with tempfile.TemporaryDirectory() as store, tempfile.TemporaryDirectory() as directory:
            with OrchestrationOpenCodeServer() as server:
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
                ("POST", "/api/session", {"location": {"directory": directory}}),
                ("POST", "/session/ses_new/run", {"message": "Finish the worker task"}),
                ("POST", "/session/ses_new/reply", {}),
                ("DELETE", "/api/session/ses_new", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()


def _payload_directory(payload):
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    return location.get("directory") or payload.get("directory")

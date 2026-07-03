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
                    response = {"id": session_id, "directory": payload["directory"]}
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
    def __init__(self, *, prompt_response=None, abort_response=None):
        self.prompt_response = prompt_response or {}
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
                    self._write_json(parent.prompt_response)
                    return
                if self.path == "/session/ses_plan/abort":
                    self._write_json(parent.abort_response)
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

    def test_start_posts_all_ready_worker_runs_before_waiting_for_replies(self):
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
                    {"directory": directory, "agent": "build", "model": "openai/gpt-5.5-mini"},
                ),
                (
                    "POST",
                    "/api/session",
                    {"directory": directory, "agent": "plan", "model": "openai/gpt-5.5"},
                ),
                ("POST", "/session/ses_docs/run", {"message": "Draft the release notes"}),
                ("POST", "/session/ses_plan/run", {"message": "Create the implementation plan"}),
                ("POST", "/session/ses_docs/reply", {}),
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
                ("POST", "/api/session", {"directory": directory}),
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
                ("POST", "/api/session", {"directory": directory}),
                ("POST", "/session/ses_plan/run", {"message": "Plan the work"}),
                ("POST", "/session/ses_plan/reply", {}),
                ("POST", "/api/session", {"directory": directory}),
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
                        "messageID": "msg_steer_1",
                        "parts": [{"type": "text", "text": "Incorporate the review feedback"}],
                        "delivery": "steer",
                    },
                ),
            ],
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["workers"]["planner"]["status"], "active")
        self.assertEqual(payload["workers"]["planner"]["prompt_ids"], ["msg_steer_1"])

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

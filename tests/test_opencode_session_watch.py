import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class WatchOpenCodeServer:
    def __init__(
        self,
        *,
        events=None,
        event_status=200,
        raw_event_body=None,
        keep_open_seconds=0,
        event_start_delay_seconds=0,
    ):
        self.events = events or []
        self.event_status = event_status
        self.raw_event_body = raw_event_body
        self.keep_open_seconds = keep_open_seconds
        self.event_start_delay_seconds = event_start_delay_seconds
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
                            },
                        }
                    )
                    return
                if self.path == "/api/event":
                    self.send_response(parent.event_status)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if parent.event_status != 200:
                        return
                    if parent.raw_event_body is not None:
                        self.wfile.write(parent.raw_event_body.encode("utf-8"))
                        self.wfile.flush()
                        return
                    if parent.event_start_delay_seconds:
                        time.sleep(parent.event_start_delay_seconds)
                    for event in parent.events:
                        try:
                            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                    if parent.keep_open_seconds:
                        time.sleep(parent.keep_open_seconds)
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


class WatchCliTest(unittest.TestCase):
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

    def test_watch_filters_session_and_prints_normalized_progress_events(self):
        events = [
            {
                "type": "session.prompt.admitted",
                "properties": {
                    "sessionID": "ses_other",
                    "messageID": "msg_ignore",
                    "delivery": "queue",
                    "state": "admitted",
                },
            },
            {
                "type": "session.prompt.admitted",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_1",
                    "delivery": "queue",
                    "state": "admitted",
                },
            },
            {
                "type": "tool.execute.started",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_1",
                    "callID": "call_1",
                    "tool": "bash",
                    "state": "running",
                },
            },
            {"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}},
        ]

        with WatchOpenCodeServer(events=events) as server:
            result = self.run_cli("watch", "ses_target", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "admission session=ses_target message=msg_1 delivery=queue status=queued\n"
            "tool session=ses_target message=msg_1 call=call_1 tool=bash status=active\n"
            "status session=ses_target status=done\n",
        )
        self.assertEqual(
            server.requests,
            [("GET", "/global/health", None), ("GET", "/doc", None), ("GET", "/api/event", None)],
        )

    def test_watch_coalesces_text_deltas_in_compact_output(self):
        events = [
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_assistant",
                    "part": {"type": "text", "text": "Hello"},
                },
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_assistant",
                    "part": {"type": "text", "text": " "},
                },
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_other",
                    "messageID": "msg_ignore",
                    "part": {"type": "text", "text": "ignored"},
                },
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_assistant",
                    "part": {"type": "text", "text": "world"},
                },
            },
            {"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}},
        ]

        with WatchOpenCodeServer(events=events) as server:
            result = self.run_cli("watch", "ses_target", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "text session=ses_target message=msg_assistant chars=11 text=\"Hello world\"\n"
            "status session=ses_target status=done\n",
        )

    def test_watch_json_outputs_normalized_events_for_automation(self):
        events = [
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_assistant",
                    "part": {"type": "text", "text": "Hi"},
                },
            },
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_assistant",
                    "part": {"type": "text", "text": "!"},
                },
            },
            {
                "type": "permission.requested",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_assistant",
                    "permissionID": "perm_1",
                    "question": "Allow bash?",
                    "status": "pending",
                },
            },
            {"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}},
        ]

        with WatchOpenCodeServer(events=events) as server:
            result = self.run_cli("watch", "ses_target", "--json", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            [json.loads(line) for line in result.stdout.splitlines()],
            [
                {
                    "kind": "text",
                    "session_id": "ses_target",
                    "type": "message.part.updated",
                    "message_id": "msg_assistant",
                    "text": "Hi",
                },
                {
                    "kind": "text",
                    "session_id": "ses_target",
                    "type": "message.part.updated",
                    "message_id": "msg_assistant",
                    "text": "!",
                },
                {
                    "kind": "blocker",
                    "session_id": "ses_target",
                    "type": "permission.requested",
                    "message_id": "msg_assistant",
                    "status": "queued",
                    "raw_status": "pending",
                    "blocker": "permission",
                    "blocker_id": "perm_1",
                    "question": "Allow bash?",
                },
                {
                    "kind": "status",
                    "session_id": "ses_target",
                    "type": "session.status",
                    "status": "done",
                    "raw_status": "completed",
                },
            ],
        )

    def test_watch_timeout_exits_with_stable_code(self):
        with WatchOpenCodeServer(keep_open_seconds=2) as server:
            result = self.run_cli("watch", "ses_target", "--timeout", "0.1", "--server", server.url)

        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "")
        self.assertIn("watch timed out after 0.1s", result.stderr)

    def test_watch_idle_stream_can_emit_terminal_event_after_api_client_default_timeout(self):
        events = [{"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}}]

        with WatchOpenCodeServer(events=events, event_start_delay_seconds=3.2) as server:
            result = self.run_cli("watch", "ses_target", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "status session=ses_target status=done\n")

    def test_watch_malformed_stream_exits_with_stable_data_error(self):
        with WatchOpenCodeServer(raw_event_body="data: {not-json\n\n") as server:
            result = self.run_cli("watch", "ses_target", "--server", server.url)

        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stdout, "")
        self.assertIn("event stream failure", result.stderr)
        self.assertIn("invalid JSON", result.stderr)

    def test_watch_prints_prompt_step_and_error_progress_events(self):
        events = [
            {
                "type": "session.prompt.started",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_user",
                    "status": "running",
                },
            },
            {
                "type": "session.step.started",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_assistant",
                    "stepID": "step_1",
                    "title": "Plan changes",
                    "status": "started",
                },
            },
            {
                "type": "message.error",
                "properties": {
                    "sessionID": "ses_target",
                    "messageID": "msg_assistant",
                    "status": "failed",
                    "error": "provider overloaded",
                },
            },
            {"type": "session.status", "properties": {"sessionID": "ses_target", "status": "completed"}},
        ]

        with WatchOpenCodeServer(events=events) as server:
            result = self.run_cli("watch", "ses_target", "--server", server.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            result.stdout,
            "prompt session=ses_target message=msg_user status=active\n"
            "step session=ses_target message=msg_assistant step=step_1 status=active title=\"Plan changes\"\n"
            "error session=ses_target message=msg_assistant status=failed error=\"provider overloaded\"\n"
            "status session=ses_target status=done\n",
        )


if __name__ == "__main__":
    unittest.main()

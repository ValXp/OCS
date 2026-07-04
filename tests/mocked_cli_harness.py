import json
import os
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"
DEFAULT_TIMEOUT_SECONDS = 20.0


class FakeRequest:
    def __init__(self, method, path, payload, headers):
        self.method = method
        self.path = path
        self.payload = payload
        self.headers = headers


class FakeOpenCodeServer:
    def __init__(self):
        self._routes = {}
        self.requests = []
        self.unexpected_requests = []
        self.server = None
        self.thread = None

    def __enter__(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def do_PATCH(self):
                self._handle()

            def do_DELETE(self):
                self._handle()

            def _handle(self):
                payload = self._read_payload()
                request = FakeRequest(self.command, self.path, payload, dict(self.headers.items()))
                parent.requests.append((request.method, request.path, request.payload))
                responder = parent._routes.get((request.method, request.path))
                if responder is None:
                    parent.unexpected_requests.append((request.method, request.path, request.payload))
                    self._write_text(parent._format_unexpected_requests(), status=500)
                    return
                responder(self, request)

            def _read_payload(self):
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    if self.command in {"POST", "PATCH", "PUT"}:
                        return {}
                    return None
                body = self.rfile.read(length).decode("utf-8")
                if not body:
                    return {}
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return body

            def _write_json(self, payload, *, status=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _write_sse(
                self,
                events,
                *,
                status=200,
                raw_body=None,
                keep_open_seconds=0,
                event_start_delay_seconds=0,
            ):
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if status != 200:
                    return
                if raw_body is not None:
                    self.wfile.write(raw_body.encode("utf-8"))
                    self.wfile.flush()
                    return
                if event_start_delay_seconds:
                    time.sleep(event_start_delay_seconds)
                for event in events:
                    try:
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                if keep_open_seconds:
                    time.sleep(keep_open_seconds)

            def _write_text(self, text, *, status=200):
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
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
        if exc_type is None and self.unexpected_requests:
            raise AssertionError(self._format_unexpected_requests())

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def json(self, method, path, payload, *, status=200):
        def responder(handler, request):
            response_payload = payload(request) if callable(payload) else payload
            response_status = status(request) if callable(status) else status
            handler._write_json(response_payload, status=response_status)

        self._routes[(method.upper(), path)] = responder
        return self

    def sse(
        self,
        path,
        events=None,
        *,
        method="GET",
        status=200,
        raw_body=None,
        keep_open_seconds=0,
        event_start_delay_seconds=0,
    ):
        def responder(handler, request):
            response_events = events(request) if callable(events) else events
            response_status = status(request) if callable(status) else status
            handler._write_sse(
                response_events or [],
                status=response_status,
                raw_body=raw_body,
                keep_open_seconds=keep_open_seconds,
                event_start_delay_seconds=event_start_delay_seconds,
            )

        self._routes[(method.upper(), path)] = responder
        return self

    def _format_unexpected_requests(self):
        lines = ["unexpected OpenCode request(s):"]
        lines.extend(f"- {method} {path} payload={payload!r}" for method, path, payload in self.unexpected_requests)
        lines.append("registered routes:")
        registered_routes = [f"- {method} {path}" for method, path in sorted(self._routes)]
        lines.extend(registered_routes or ["- none"])
        return "\n".join(lines)


def run_ocs(*args, input_text=None, env=None, timeout_seconds=None):
    command = [sys.executable, str(CLI), *args]
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    timeout = DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=command_env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(_format_timeout(error, command)) from error


def format_completed_process(result):
    return "\n".join(
        [
            f"command: {_format_command(result.args)}",
            f"exit code: {result.returncode}",
            "stdout:",
            _format_stream(result.stdout),
            "stderr:",
            _format_stream(result.stderr),
        ]
    )


def load_json(testcase, result, description="CLI"):
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        testcase.fail(f"{description} did not emit valid JSON: {error}\n{format_completed_process(result)}")


def _format_timeout(error, command):
    return "\n".join(
        [
            f"command: {_format_command(command)}",
            f"exit code: timeout after {error.timeout:g}s",
            "stdout:",
            _format_stream(error.stdout),
            "stderr:",
            _format_stream(error.stderr),
        ]
    )


def _format_command(command):
    return shlex.join(str(part) for part in command)


def _format_stream(value):
    if value is None or value == "":
        return "(empty)"
    return str(value).rstrip("\n")

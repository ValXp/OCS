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
    def __init__(self, method, path, payload, headers, params=None):
        self.method = method
        self.path = path
        self.payload = payload
        self.headers = headers
        self.params = params or {}


class FakeOpenCodeServer:
    def __init__(self):
        self._routes = {}
        self._route_templates = []
        self.requests = []
        self.unexpected_requests = []
        self.server = None
        self.thread = None

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenCodeRequestHandler)
        self.server.fake_opencode_server = self
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

        return self.route(method, path, responder)

    def route(self, method, path, responder):
        method = method.upper()
        if _is_route_template(path):
            self._route_templates.append((method, path, _compile_route_template(path), responder))
        else:
            self._routes[(method, path)] = responder
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

        return self.route(method, path, responder)

    def _format_unexpected_requests(self):
        lines = ["unexpected OpenCode request(s):"]
        lines.extend(f"- {method} {path} payload={payload!r}" for method, path, payload in self.unexpected_requests)
        lines.append("registered routes:")
        registered_routes = [f"- {method} {path}" for method, path in sorted(self._routes)]
        registered_routes.extend(
            f"- {method} {path}" for method, path, _matcher, _responder in self._route_templates
        )
        lines.extend(registered_routes or ["- none"])
        return "\n".join(lines)

    def _match_template_route(self, method, path):
        for route_method, _route_path, matcher, responder in self._route_templates:
            if route_method != method:
                continue
            params = matcher(path)
            if params is not None:
                return responder, params
        return None, {}


class _FakeOpenCodeRequestHandler(BaseHTTPRequestHandler):
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

    @property
    def _fake_server(self):
        return self.server.fake_opencode_server

    def _handle(self):
        payload = self._read_payload()
        request = FakeRequest(self.command, self.path, payload, dict(self.headers.items()))
        fake_server = self._fake_server
        fake_server.requests.append((request.method, request.path, request.payload))
        responder = fake_server._routes.get((request.method, request.path))
        if responder is None:
            responder, params = fake_server._match_template_route(request.method, request.path)
            request.params = params
        if responder is None:
            fake_server.unexpected_requests.append((request.method, request.path, request.payload))
            self._write_text(fake_server._format_unexpected_requests(), status=500)
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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def request_paths(requests):
    return [(method, path) for method, path, _payload in requests]


def payload_directory(payload):
    payload = payload or {}
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    return location.get("directory") or payload.get("directory")


def prompt_message_id(payload):
    return (payload or {}).get("messageID") or (payload or {}).get("id")


def prompt_text(payload):
    payload = payload or {}
    prompt = payload.get("prompt") if isinstance(payload.get("prompt"), dict) else {}
    if prompt.get("text") is not None:
        return prompt.get("text")
    parts = payload.get("parts") if isinstance(payload.get("parts"), list) else []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def smoke_open_code_server(*, sessions=None, prompt_response=None, prompt_status=200):
    return configure_smoke_open_code_server(
        FakeOpenCodeServer(),
        sessions=sessions,
        prompt_response=prompt_response,
        prompt_status=prompt_status,
    )


def configure_smoke_open_code_server(server, *, sessions=None, prompt_response=None, prompt_status=200):
    server.sessions = list(sessions or [])
    _register_core_routes(
        server,
        {
            "/api/session": {"get": {}, "post": {}},
            "/api/session/{sessionID}/prompt": {"post": {}},
            "/api/event": {"get": {}},
            "/session/{sessionID}/run": {"post": {}},
            "/session/{sessionID}/reply": {"post": {}},
        },
    )
    server.sse(
        "/api/event",
        [
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
        ],
    )
    server.json("GET", "/api/session", lambda _request: {"sessions": server.sessions})
    server.route("GET", "/api/session/{sessionID}", _session_getter(server))
    server.route("DELETE", "/api/session/{sessionID}", _session_deleter(server))
    server.json("GET", "/permission", [])
    server.json("GET", "/question", [])
    server.json("POST", "/api/session", lambda request: _create_smoke_session(server, request.payload))
    server.json(
        "POST",
        "/api/session/ses_smoke_1/prompt",
        lambda request: prompt_response
        or {
            "sessionID": "ses_smoke_1",
            "messageID": prompt_message_id(request.payload),
            "delivery": "steer",
            "state": "admitted",
            "admittedSequence": 1,
        },
        status=prompt_status,
    )
    server.json("POST", "/session/ses_smoke_1/run", {"id": "msg_user_smoke", "status": "submitted"})
    server.json(
        "POST",
        "/session/ses_smoke_1/reply",
        {"id": "msg_assistant_smoke", "status": "completed", "text": "ok"},
    )
    return server


def live_validation_open_code_server(
    *,
    reply_payload=None,
    message_payload=None,
    wait_payload=None,
    wait_available=True,
    session_payloads=None,
    events=None,
):
    return configure_live_validation_open_code_server(
        FakeOpenCodeServer(),
        reply_payload=reply_payload,
        message_payload=message_payload,
        wait_payload=wait_payload,
        wait_available=wait_available,
        session_payloads=session_payloads,
        events=events,
    )


def configure_live_validation_open_code_server(
    server,
    *,
    reply_payload=None,
    message_payload=None,
    wait_payload=None,
    wait_available=True,
    session_payloads=None,
    events=None,
):
    server.sessions = []
    session_payloads = session_payloads or {}
    reply_payload = reply_payload or {
        "id": "msg_assistant_live",
        "status": "completed",
        "cost": 0.001,
        "tokens": {"input": 4, "output": 1, "total": 5},
        "text": "PONG",
    }
    message_payload = message_payload or {
        "info": {
            "id": "msg_assistant_live",
            "sessionID": "ses_live_2",
            "role": "assistant",
            "cost": 0.001,
            "tokens": {"input": 4, "output": 1, "total": 5},
        },
        "parts": [{"type": "text", "text": "PONG"}],
    }
    paths = {
        "/api/session": {"get": {}, "post": {}},
        "/api/session/{sessionID}/prompt": {"post": {}},
        "/session/{sessionID}/message": {"post": {}},
        "/session/{sessionID}/run": {"post": {}},
        "/session/{sessionID}/reply": {"post": {}},
    }
    if wait_available:
        paths["/api/session/{sessionID}/wait"] = {"post": {}}
    if events is not None:
        paths["/api/event"] = {"get": {}}
    _register_core_routes(server, paths)
    if events is not None:
        server.sse("/api/event", events)
    server.json("GET", "/api/session", {"error": "not found"}, status=404)
    server.route("GET", "/api/session/{sessionID}", _session_getter(server, session_payloads=session_payloads))
    server.route("DELETE", "/api/session/{sessionID}", _session_deleter(server))
    server.json("POST", "/api/session", lambda request: _create_live_validation_session(server, request.payload))
    server.json(
        "POST",
        "/api/session/ses_live_1/prompt",
        lambda request: {
            "sessionID": "ses_live_1",
            "messageID": prompt_message_id(request.payload),
            "delivery": "steer",
            "state": "admitted",
            "admittedSequence": 1,
        },
    )
    server.json("POST", "/api/session/ses_live_1/wait", wait_payload or {})
    server.json("POST", "/session/ses_live_2/run", {"id": "msg_user_live", "status": "submitted"})
    server.json("POST", "/session/ses_live_2/reply", reply_payload)
    server.json("POST", "/session/ses_live_2/message", message_payload)
    return server


def _register_core_routes(server, paths):
    server.json("GET", "/global/health", {"status": "ok", "version": "2.0.0"})
    server.json("GET", "/doc", {"openapi": "3.1.0", "paths": paths})


def _create_smoke_session(server, payload):
    session = {
        "id": "ses_smoke_1",
        "title": payload["title"],
        "directory": payload_directory(payload),
        "metadata": payload["metadata"],
    }
    server.sessions.append(session)
    return session


def _create_live_validation_session(server, payload):
    session_id = f"ses_live_{len(server.sessions) + 1}"
    session = {
        "id": session_id,
        "title": payload["title"],
        "directory": payload_directory(payload),
        "metadata": payload["metadata"],
    }
    server.sessions.append(session)
    return session


def _session_getter(server, *, session_payloads=None):
    session_payloads = session_payloads or {}

    def responder(handler, request):
        session_id = request.params["sessionID"]
        for session in server.sessions:
            if session["id"] == session_id:
                payload = dict(session)
                payload.update(session_payloads.get(session_id, {}))
                handler._write_json(payload)
                return
        handler._write_json({"error": "not found"}, status=404)

    return responder


def _session_deleter(server):
    def responder(handler, request):
        session_id = request.params["sessionID"]
        for index, session in enumerate(server.sessions):
            if session["id"] == session_id:
                del server.sessions[index]
                handler._write_json({"id": session_id, "deleted": True})
                return
        handler._write_json({"error": "not found"}, status=404)

    return responder


def _is_route_template(path):
    return "{" in path and "}" in path


def _compile_route_template(path):
    template_parts = path.strip("/").split("/")

    def matcher(candidate):
        candidate_parts = candidate.strip("/").split("/")
        if len(candidate_parts) != len(template_parts):
            return None
        params = {}
        for template_part, candidate_part in zip(template_parts, candidate_parts):
            if template_part.startswith("{") and template_part.endswith("}"):
                params[template_part[1:-1]] = candidate_part
            elif template_part != candidate_part:
                return None
        return params

    return matcher


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

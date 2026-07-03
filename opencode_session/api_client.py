import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from opencode_session.events import EventStreamError, iter_event_stream


class OpenCodeApiError(Exception):
    def __init__(self, message, *, status=None, method=None, path=None, body=None, data=None):
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        self.data = data


class OpenCodeApiResponse:
    def __init__(self, data, body):
        self.data = data
        self.body = body


class OpenCodeApiClient:
    def __init__(self, base_url, *, timeout=3):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def get_health(self):
        errors = []
        for path in ("global/health", "api/health", "health"):
            try:
                return self.get_json(path)
            except OpenCodeApiError as error:
                errors.append(str(error))
        raise OpenCodeApiError("; ".join(errors))

    def get_openapi_doc(self):
        try:
            return self.get_json("doc")
        except OpenCodeApiError:
            return {"paths": {}}

    def require_openapi_doc(self):
        return self.get_json("doc")

    def get_json(self, path):
        return self.get_response(path).data

    def get_response(self, path):
        return self._request_json("GET", path)

    def post_json(self, path, payload):
        return self.post_response(path, payload).data

    def post_response(self, path, payload):
        return self._request_json("POST", path, payload)

    def delete_json(self, path):
        return self.delete_response(path).data

    def delete_response(self, path):
        return self._request_json("DELETE", path)

    def stream_events(self, path):
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {"Accept": "text/event-stream, application/json"}
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                # SSE reads are long-lived; watch --timeout is the user-facing deadline.
                response.fp.raw._sock.settimeout(None)
                yield from iter_event_stream(response)
        except EventStreamError as error:
            raise OpenCodeApiError(
                f"GET /{path.lstrip('/')} returned invalid event stream: {error}",
                method="GET",
                path=f"/{path.lstrip('/')}",
                data={"kind": "invalid_event_stream"},
            ) from error
        except HTTPError as error:
            error_body = error.read().decode("utf-8")
            error_data = None
            try:
                error_data = json.loads(error_body or "{}")
            except json.JSONDecodeError:
                pass
            raise OpenCodeApiError(
                f"GET /{path.lstrip('/')} failed: HTTP {error.code}",
                status=error.code,
                method="GET",
                path=f"/{path.lstrip('/')}",
                body=error_body,
                data=error_data,
            ) from error
        except URLError as error:
            raise OpenCodeApiError(f"cannot reach OpenCode server at {self.base_url.rstrip('/')}: {error.reason}") from error
        except TimeoutError as error:
            raise OpenCodeApiError(f"OpenCode event stream timed out at {self.base_url.rstrip('/')}") from error

    def _request_json(self, method, path, payload=None):
        response_body = self._request_body(method, path, payload)
        try:
            data = json.loads(response_body or "{}")
        except json.JSONDecodeError as error:
            raise OpenCodeApiError(
                f"{method} /{path.lstrip('/')} returned invalid JSON",
                method=method,
                path=f"/{path.lstrip('/')}",
            ) from error
        return OpenCodeApiResponse(data, response_body)

    def _request_body(self, method, path, payload=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            error_body = error.read().decode("utf-8")
            error_data = None
            try:
                error_data = json.loads(error_body or "{}")
            except json.JSONDecodeError:
                pass
            raise OpenCodeApiError(
                f"{method} /{path.lstrip('/')} failed: HTTP {error.code}",
                status=error.code,
                method=method,
                path=f"/{path.lstrip('/')}",
                body=error_body,
                data=error_data,
            ) from error
        except URLError as error:
            raise OpenCodeApiError(f"cannot reach OpenCode server at {self.base_url.rstrip('/')}: {error.reason}") from error
        except TimeoutError as error:
            raise OpenCodeApiError(f"OpenCode server timed out at {self.base_url.rstrip('/')}") from error

    def create_session(self, directory, *, agent=None, model=None, title=None, metadata=None):
        return self.create_session_response(directory, agent=agent, model=model, title=title, metadata=metadata).data

    def create_session_response(self, directory, *, agent=None, model=None, title=None, metadata=None):
        payload = {"directory": directory}
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model
        if title is not None:
            payload["title"] = title
        if metadata is not None:
            payload["metadata"] = metadata
        return self.post_response("api/session", payload)

    def list_sessions(self):
        return self.list_sessions_response().data

    def list_sessions_response(self):
        return self.get_response("api/session")

    def get_session(self, session_id):
        return self.get_session_response(session_id).data

    def get_session_response(self, session_id):
        return self.get_response(f"api/session/{quote(session_id, safe='')}")

    def delete_session(self, session_id):
        return self.delete_session_response(session_id).data

    def delete_session_response(self, session_id):
        return self.delete_response(f"api/session/{quote(session_id, safe='')}")

    def abort_session_response(self, session_id):
        return self.post_response(f"session/{quote(session_id, safe='')}/abort", {})

    def fork_session_response(self, session_id, *, message_id=None):
        payload = {}
        if message_id is not None:
            payload["messageID"] = message_id
        return self.post_response(f"session/{quote(session_id, safe='')}/fork", payload)

    def list_child_sessions_response(self, session_id):
        return self.get_response(f"session/{quote(session_id, safe='')}/children")

    def run_session_response(self, session_id, message):
        return self.post_response(f"session/{quote(session_id, safe='')}/run", {"message": message})

    def reply_session_response(self, session_id):
        return self.post_response(f"session/{quote(session_id, safe='')}/reply", {})

    def admit_prompt_response(self, session_id, payload, prompt_path):
        return self.post_response(_session_prompt_path(prompt_path, session_id), payload)

    def list_permissions_response(self):
        return self.get_response("permission")

    def reply_permission_response(self, request_id, reply, *, message=None):
        payload = {"reply": reply}
        if message is not None:
            payload["message"] = message
        return self.post_response(f"permission/{quote(request_id, safe='')}/reply", payload)

    def list_questions_response(self):
        return self.get_response("question")

    def answer_question_response(self, request_id, answers):
        return self.post_response(f"question/{quote(request_id, safe='')}/reply", {"answers": answers})

    def reject_question_response(self, request_id):
        return self.post_response(f"question/{quote(request_id, safe='')}/reject", {})


def _session_prompt_path(prompt_path, session_id):
    path = prompt_path.lstrip("/")
    quoted_session_id = quote(session_id, safe="")
    for placeholder in ("{sessionID}", ":sessionID", "{id}", ":id"):
        path = path.replace(placeholder, quoted_session_id)
    return path

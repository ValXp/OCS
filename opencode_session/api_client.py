import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from opencode_session.events import EventStreamError, iter_event_stream
from opencode_session.schema_normalization import normalize_session_payload
from opencode_session.timeout_boundary import TimeoutExpired
from opencode_session.urllib_compat import set_response_socket_timeout


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


DEFAULT_ROUTE_PLAN = {
    "session_collection": "/api/session",
    "session_item": "/api/session/{sessionID}",
    "blocking_message": "/session/{sessionID}/message",
    "legacy_run": "/session/{sessionID}/run",
    "legacy_reply": "/session/{sessionID}/reply",
}


class OpenCodeApiClient:
    def __init__(self, base_url, *, timeout=3):
        _validate_base_url(base_url)
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.route_plan = None

    def configure_route_plan(self, route_plan):
        self.route_plan = {**DEFAULT_ROUTE_PLAN, **(route_plan or {})}
        return self

    def get_health(self, *, deadline=None):
        errors = []
        for path in ("global/health", "api/health", "health"):
            try:
                return self.get_json(path, deadline=deadline)
            except OpenCodeApiError as error:
                errors.append(str(error))
        raise OpenCodeApiError("; ".join(errors))

    def get_openapi_doc(self, *, deadline=None):
        try:
            return self.get_json("doc", deadline=deadline)
        except OpenCodeApiError:
            return {"paths": {}}

    def require_openapi_doc(self, *, deadline=None):
        return self.get_json("doc", deadline=deadline)

    def get_json(self, path, *, timeout=None, deadline=None):
        return self.get_response(path, timeout=timeout, deadline=deadline).data

    def get_response(self, path, *, timeout=None, deadline=None):
        return self._request_json("GET", path, timeout=timeout, deadline=deadline)

    def post_json(self, path, payload, *, timeout=None, deadline=None):
        return self.post_response(path, payload, timeout=timeout, deadline=deadline).data

    def post_response(self, path, payload, *, timeout=None, deadline=None):
        return self._request_json("POST", path, payload, timeout=timeout, deadline=deadline)

    def delete_json(self, path, *, timeout=None, deadline=None):
        return self.delete_response(path, timeout=timeout, deadline=deadline).data

    def delete_response(self, path, *, timeout=None, deadline=None):
        return self._request_json("DELETE", path, timeout=timeout, deadline=deadline)

    def stream_events(self, path, *, on_open=None, deadline=None, stop_event=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {"Accept": "text/event-stream, application/json"}
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self._stream_open_timeout(deadline)) as response:
                stream_timeout = _event_stream_read_timeout(deadline, stop_event)
                set_response_socket_timeout(response, stream_timeout)
                if on_open is not None:
                    on_open()
                lines = _event_stream_lines(response, deadline, stop_event)
                yield from iter_event_stream(lines)
        except TimeoutExpired:
            raise
        except EventStreamError as error:
            raise OpenCodeApiError(
                f"GET /{path.lstrip('/')} returned invalid event stream: {error}",
                method="GET",
                path=f"/{path.lstrip('/')}",
                data={"kind": "invalid_event_stream"},
            ) from error
        except HTTPError as error:
            _raise_http_error(error, "GET", path)
        except URLError as error:
            _raise_transport_error(error, base_url=self.base_url, deadline=deadline, stream=True)
        except TimeoutError as error:
            _raise_transport_error(error, base_url=self.base_url, deadline=deadline, stream=True)

    def _request_json(self, method, path, payload=None, *, timeout=None, deadline=None):
        response_body = self._request_body(method, path, payload, timeout=timeout, deadline=deadline)
        try:
            data = json.loads(response_body or "{}")
        except json.JSONDecodeError as error:
            raise OpenCodeApiError(
                f"{method} /{path.lstrip('/')} returned invalid JSON",
                method=method,
                path=f"/{path.lstrip('/')}",
            ) from error
        return OpenCodeApiResponse(data, response_body)

    def _request_body(self, method, path, payload=None, *, timeout=None, deadline=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._request_timeout(timeout, deadline)) as response:
                return response.read().decode("utf-8")
        except TimeoutExpired:
            raise
        except HTTPError as error:
            _raise_http_error(error, method, path)
        except URLError as error:
            _raise_transport_error(error, base_url=self.base_url, deadline=deadline)
        except TimeoutError as error:
            _raise_transport_error(error, base_url=self.base_url, deadline=deadline)

    def create_session(self, directory, *, agent=None, model=None, title=None, metadata=None):
        return self.create_session_response(directory, agent=agent, model=model, title=title, metadata=metadata).data

    def create_session_response(self, directory, *, agent=None, model=None, title=None, metadata=None):
        payload = {"location": {"directory": directory}}
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model
        if title is not None:
            payload["title"] = title
        if metadata is not None:
            payload["metadata"] = metadata
        return _with_session_payload(self.post_response(self._route_path("session_collection"), payload), self.route_plan)

    def list_sessions(self):
        return self.list_sessions_response().data

    def list_sessions_response(self):
        return _with_session_payload(self.get_response(self._route_path("session_collection")), self.route_plan)

    def get_session(self, session_id):
        return self.get_session_response(session_id).data

    def get_session_response(self, session_id):
        return _with_session_payload(self.get_response(self._route_path("session_item", session_id=session_id)), self.route_plan)

    def delete_session(self, session_id):
        return self.delete_session_response(session_id).data

    def delete_session_response(self, session_id):
        return self.delete_response(self._route_path("session_item", session_id=session_id))

    def abort_session_response(self, session_id):
        return self.post_response(f"session/{quote(session_id, safe='')}/abort", {})

    def fork_session_response(self, session_id, *, message_id=None):
        payload = {}
        if message_id is not None:
            payload["messageID"] = message_id
        return self.post_response(f"session/{quote(session_id, safe='')}/fork", payload)

    def list_child_sessions_response(self, session_id):
        return _with_session_payload(self.get_response(f"session/{quote(session_id, safe='')}/children"), route_path="/session")

    def run_session_response(self, session_id, message, *, timeout=None, deadline=None):
        return self.post_response(
            self._route_path("legacy_run", session_id=session_id),
            {"message": message},
            timeout=timeout,
            deadline=deadline,
        )

    def reply_session_response(self, session_id, *, timeout=None, deadline=None):
        return self.post_response(
            self._route_path("legacy_reply", session_id=session_id),
            {},
            timeout=timeout,
            deadline=deadline,
        )

    def message_session_response(self, session_id, message, *, message_id=None, timeout=None, deadline=None):
        payload = {"parts": [{"type": "text", "text": message}]}
        if message_id is not None:
            payload["messageID"] = message_id
        return self.post_response(
            self._route_path("blocking_message", session_id=session_id),
            payload,
            timeout=timeout,
            deadline=deadline,
        )

    def admit_prompt_response(self, session_id, payload, prompt_path):
        return self.post_response(_session_prompt_path(prompt_path, session_id), payload)

    def wait_session_response(self, session_id, wait_path, *, deadline=None):
        return self.post_response(_session_prompt_path(wait_path, session_id), {}, deadline=deadline)

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

    def _request_timeout(self, timeout, deadline):
        if deadline is not None:
            return deadline.require_time()
        if timeout is None:
            return self.timeout
        return timeout

    def _stream_open_timeout(self, deadline):
        if deadline is None:
            return self.timeout
        return deadline.require_time()

    def _route_path(self, name, *, session_id=None):
        route_plan = self._require_route_plan()
        path = route_plan.get(name) or DEFAULT_ROUTE_PLAN[name]
        if session_id is not None:
            path = _session_prompt_path(path, session_id)
        return path.lstrip("/")

    def _require_route_plan(self):
        if self.route_plan is None:
            raise OpenCodeApiError(
                "client route plan is not configured; discover capabilities and configure routes before session calls",
                data={"kind": "route_plan_required"},
            )
        return self.route_plan


def _session_prompt_path(prompt_path, session_id):
    path = prompt_path.lstrip("/")
    quoted_session_id = quote(session_id, safe="")
    for placeholder in ("{sessionID}", ":sessionID", "{id}", ":id"):
        path = path.replace(placeholder, quoted_session_id)
    return path


def _with_session_payload(response, route_plan=None, *, route_path=None):
    return OpenCodeApiResponse(normalize_session_payload(response.data, route_plan=route_plan, route_path=route_path), response.body)


def _validate_base_url(base_url):
    parsed = urlparse(base_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OpenCodeApiError(f"invalid OpenCode server URL {base_url!r}: expected http(s) URL")


def _raise_http_error(error, method, path):
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


def _raise_transport_error(error, *, base_url, deadline=None, stream=False):
    if isinstance(error, URLError):
        if deadline is not None and _url_error_is_timeout(error):
            raise TimeoutExpired() from error
        raise OpenCodeApiError(f"cannot reach OpenCode server at {base_url.rstrip('/')}: {error.reason}") from error
    if deadline is not None:
        raise TimeoutExpired() from error
    target = "event stream" if stream else "server"
    raise OpenCodeApiError(f"OpenCode {target} timed out at {base_url.rstrip('/')}") from error


def _url_error_is_timeout(error):
    return isinstance(getattr(error, "reason", None), TimeoutError)


def _iter_response_lines_until_deadline(response, deadline):
    while True:
        set_response_socket_timeout(response, deadline.require_time())
        try:
            line = response.readline()
        except TimeoutError as error:
            raise TimeoutExpired() from error
        if line == b"":
            return
        yield line


def _event_stream_lines(response, deadline, stop_event):
    if stop_event is None:
        return response if deadline is None else _iter_response_lines_until_deadline(response, deadline)
    return _iter_response_lines_until_stop(response, deadline, stop_event)


def _iter_response_lines_until_stop(response, deadline, stop_event):
    while not stop_event.is_set():
        set_response_socket_timeout(response, _event_stream_read_timeout(deadline, stop_event))
        try:
            line = response.readline()
        except TimeoutError as error:
            if deadline is not None and deadline.expired():
                raise TimeoutExpired() from error
            continue
        if line == b"":
            return
        yield line


def _event_stream_read_timeout(deadline, stop_event):
    if stop_event is None:
        return None if deadline is None else deadline.require_time()
    if deadline is None:
        return 0.2
    return min(0.2, deadline.require_time())

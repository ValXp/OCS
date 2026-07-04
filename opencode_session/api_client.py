import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from opencode_session.events import EventStreamError, iter_event_stream
from opencode_session.records import first_present as _first_present
from opencode_session.timeout_boundary import TimeoutExpired


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
        _validate_base_url(base_url)
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

    def get_json(self, path, *, timeout=None):
        return self.get_response(path, timeout=timeout).data

    def get_response(self, path, *, timeout=None):
        return self._request_json("GET", path, timeout=timeout)

    def post_json(self, path, payload, *, timeout=None):
        return self.post_response(path, payload, timeout=timeout).data

    def post_response(self, path, payload, *, timeout=None):
        return self._request_json("POST", path, payload, timeout=timeout)

    def delete_json(self, path, *, timeout=None):
        return self.delete_response(path, timeout=timeout).data

    def delete_response(self, path, *, timeout=None):
        return self._request_json("DELETE", path, timeout=timeout)

    def stream_events(self, path, *, on_open=None, deadline=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {"Accept": "text/event-stream, application/json"}
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self._stream_open_timeout(deadline)) as response:
                _set_response_socket_timeout(response, None if deadline is None else deadline.require_time())
                if on_open is not None:
                    on_open()
                lines = response if deadline is None else _iter_response_lines_until_deadline(response, deadline)
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
            if deadline is not None and deadline.expired():
                raise TimeoutExpired() from error
            raise OpenCodeApiError(f"OpenCode event stream timed out at {self.base_url.rstrip('/')}") from error

    def _request_json(self, method, path, payload=None, *, timeout=None):
        response_body = self._request_body(method, path, payload, timeout=timeout)
        try:
            data = json.loads(response_body or "{}")
        except json.JSONDecodeError as error:
            raise OpenCodeApiError(
                f"{method} /{path.lstrip('/')} returned invalid JSON",
                method=method,
                path=f"/{path.lstrip('/')}",
            ) from error
        return OpenCodeApiResponse(data, response_body)

    def _request_body(self, method, path, payload=None, *, timeout=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._request_timeout(timeout)) as response:
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
        payload = {"location": {"directory": directory}}
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model
        if title is not None:
            payload["title"] = title
        if metadata is not None:
            payload["metadata"] = metadata
        return _with_session_payload(self.post_response("api/session", payload))

    def list_sessions(self):
        return self.list_sessions_response().data

    def list_sessions_response(self):
        return _with_session_payload(self.get_response("api/session"))

    def get_session(self, session_id):
        return self.get_session_response(session_id).data

    def get_session_response(self, session_id):
        return _with_session_payload(self.get_response(f"api/session/{quote(session_id, safe='')}"))

    def delete_session(self, session_id):
        return self.delete_session_response(session_id).data

    def delete_session_response(self, session_id):
        quoted_session_id = quote(session_id, safe="")
        try:
            return self.delete_response(f"api/session/{quoted_session_id}")
        except OpenCodeApiError as error:
            if error.method == "DELETE" and error.path == f"/api/session/{quoted_session_id}" and "invalid JSON" in str(error):
                return self.delete_response(f"session/{quoted_session_id}")
            raise

    def abort_session_response(self, session_id):
        return self.post_response(f"session/{quote(session_id, safe='')}/abort", {})

    def fork_session_response(self, session_id, *, message_id=None):
        payload = {}
        if message_id is not None:
            payload["messageID"] = message_id
        return self.post_response(f"session/{quote(session_id, safe='')}/fork", payload)

    def list_child_sessions_response(self, session_id):
        return _with_session_payload(self.get_response(f"session/{quote(session_id, safe='')}/children"))

    def run_session_response(self, session_id, message, *, timeout=None):
        return self.post_response(f"session/{quote(session_id, safe='')}/run", {"message": message}, timeout=timeout)

    def reply_session_response(self, session_id, *, timeout=None):
        return self.post_response(f"session/{quote(session_id, safe='')}/reply", {}, timeout=timeout)

    def message_session_response(self, session_id, message, *, message_id=None, timeout=None):
        payload = {"parts": [{"type": "text", "text": message}]}
        if message_id is not None:
            payload["messageID"] = message_id
        return self.post_response(f"session/{quote(session_id, safe='')}/message", payload, timeout=timeout)

    def admit_prompt_response(self, session_id, payload, prompt_path):
        return self.post_response(_session_prompt_path(prompt_path, session_id), payload)

    def wait_session_response(self, session_id, wait_path):
        return self.post_response(_session_prompt_path(wait_path, session_id), {})

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

    def _request_timeout(self, timeout):
        if timeout is None:
            return self.timeout
        return timeout

    def _stream_open_timeout(self, deadline):
        if deadline is None:
            return self.timeout
        return deadline.require_time()


def _session_prompt_path(prompt_path, session_id):
    path = prompt_path.lstrip("/")
    quoted_session_id = quote(session_id, safe="")
    for placeholder in ("{sessionID}", ":sessionID", "{id}", ":id"):
        path = path.replace(placeholder, quoted_session_id)
    return path


def _with_session_payload(response):
    return OpenCodeApiResponse(_normalize_session_payload(response.data), response.body)


def _normalize_session_payload(payload):
    if isinstance(payload, list):
        return [_normalize_session_record(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    data = normalized.get("data")
    if isinstance(data, list):
        normalized["data"] = [_normalize_session_record(item) for item in data]
        return normalized
    if isinstance(data, dict):
        normalized["data"] = _normalize_session_record(data)
        return normalized

    for name in ("sessions", "children"):
        records = normalized.get(name)
        if isinstance(records, list):
            normalized[name] = [_normalize_session_record(item) for item in records]
            return normalized

    return _normalize_session_record(normalized)


def _normalize_session_record(record):
    if not isinstance(record, dict):
        return record
    if isinstance(record.get("data"), dict):
        normalized = dict(record)
        normalized["data"] = _normalize_session_record(record["data"])
        return normalized

    normalized = dict(record)
    _set_missing(normalized, "id", _first_present(record, "id", "sessionID", "sessionId", "session_id"))
    _set_missing(normalized, "directory", _session_directory(record))
    _set_missing(normalized, "title", _first_present(record, "title", "name"))
    _set_missing(normalized, "agent", _first_present(record, "agent", "agentID", "agentId", "agent_id"))
    _set_missing(normalized, "model", _first_present(record, "model", "modelID", "modelId", "model_id"))
    _set_missing(normalized, "tokens", _session_tokens(record))
    _set_missing(normalized, "createdAt", _session_created_at(record))
    _set_missing(normalized, "updatedAt", _session_updated_at(record))
    return normalized


def _set_missing(record, name, value):
    if value is not None and record.get(name) is None:
        record[name] = value


def _session_directory(record):
    value = _first_present(record, "directory", "cwd")
    if value is not None:
        return value
    location = record.get("location")
    if isinstance(location, dict):
        return location.get("directory")
    return None


def _session_tokens(record):
    tokens = _first_present(record, "tokens", "token", "tokenUsage", "token_usage", "usage")
    if isinstance(tokens, dict):
        normalized = dict(tokens)
        if normalized.get("total") is None:
            values = [value for value in normalized.values() if isinstance(value, int)]
            if values:
                normalized["total"] = sum(values)
        return normalized
    return tokens


def _session_created_at(record):
    value = _first_present(record, "createdAt", "created_at", "created")
    if value is not None:
        return value
    time = record.get("time")
    if isinstance(time, dict):
        return time.get("created")
    return None


def _session_updated_at(record):
    value = _first_present(record, "updatedAt", "updated_at", "updated")
    if value is not None:
        return value
    time = record.get("time")
    if isinstance(time, dict):
        return time.get("updated")
    return None


def _validate_base_url(base_url):
    parsed = urlparse(base_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OpenCodeApiError(f"invalid OpenCode server URL {base_url!r}: expected http(s) URL")


def _iter_response_lines_until_deadline(response, deadline):
    while True:
        _set_response_socket_timeout(response, deadline.require_time())
        try:
            line = response.readline()
        except TimeoutError as error:
            raise TimeoutExpired() from error
        if line == b"":
            return
        yield line


def _set_response_socket_timeout(response, timeout):
    response.fp.raw._sock.settimeout(timeout)

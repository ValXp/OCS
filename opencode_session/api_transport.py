import json
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from opencode_session.events import EventStreamError, iter_event_stream
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


class OpenCodeApiTimeoutError(OpenCodeApiError):
    """A transport timeout that was not governed by an explicit deadline."""


class OpenCodeApiResponse:
    def __init__(self, data, body):
        self.data = data
        self.body = body


class OpenCodeApiTransport:
    def __init__(self, base_url, *, timeout=3):
        _validate_base_url(base_url)
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def get_json(self, path, *, timeout=None, deadline=None):
        return self.get_response(path, timeout=timeout, deadline=deadline).data

    def get_response(self, path, *, timeout=None, deadline=None):
        return self._request_json("GET", path, timeout=timeout, deadline=deadline)

    def get_response_no_redirects(self, path, *, timeout=None, deadline=None):
        return self._request_json(
            "GET",
            path,
            timeout=timeout,
            deadline=deadline,
            follow_redirects=False,
        )

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

    def _request_json(self, method, path, payload=None, *, timeout=None, deadline=None, follow_redirects=True):
        response_body = self._request_body(
            method,
            path,
            payload,
            timeout=timeout,
            deadline=deadline,
            follow_redirects=follow_redirects,
        )
        try:
            data = json.loads(response_body or "{}")
        except json.JSONDecodeError as error:
            raise OpenCodeApiError(
                f"{method} /{path.lstrip('/')} returned invalid JSON",
                method=method,
                path=f"/{path.lstrip('/')}",
            ) from error
        return OpenCodeApiResponse(data, response_body)

    def _request_body(self, method, path, payload=None, *, timeout=None, deadline=None, follow_redirects=True):
        url = urljoin(self.base_url, path.lstrip("/"))
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            open_request = urlopen if follow_redirects else _NO_REDIRECT_OPENER.open
            with open_request(request, timeout=self._request_timeout(timeout, deadline)) as response:
                return response.read().decode("utf-8")
        except TimeoutExpired:
            raise
        except HTTPError as error:
            _raise_http_error(error, method, path)
        except URLError as error:
            _raise_transport_error(error, base_url=self.base_url, deadline=deadline)
        except TimeoutError as error:
            _raise_transport_error(error, base_url=self.base_url, deadline=deadline)

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
        if _url_error_is_timeout(error):
            if deadline is not None:
                raise TimeoutExpired() from error
            _raise_api_timeout(base_url, stream=stream, cause=error)
        raise OpenCodeApiError(f"cannot reach OpenCode server at {base_url.rstrip('/')}: {error.reason}") from error
    if deadline is not None:
        raise TimeoutExpired() from error
    _raise_api_timeout(base_url, stream=stream, cause=error)


def _raise_api_timeout(base_url, *, stream, cause):
    target = "event stream" if stream else "server"
    raise OpenCodeApiTimeoutError(f"OpenCode {target} timed out at {base_url.rstrip('/')}") from cause


def _url_error_is_timeout(error):
    return isinstance(getattr(error, "reason", None), TimeoutError)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


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

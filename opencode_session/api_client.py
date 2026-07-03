import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


class OpenCodeApiError(Exception):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


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

    def _request_json(self, method, path, payload=None):
        response_body = self._request_body(method, path, payload)
        try:
            data = json.loads(response_body or "{}")
        except json.JSONDecodeError as error:
            raise OpenCodeApiError(f"{method} /{path.lstrip('/')} returned invalid JSON") from error
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
            raise OpenCodeApiError(f"{method} /{path.lstrip('/')} failed: HTTP {error.code}", status=error.code) from error
        except URLError as error:
            raise OpenCodeApiError(f"cannot reach OpenCode server at {self.base_url.rstrip('/')}: {error.reason}") from error
        except TimeoutError as error:
            raise OpenCodeApiError(f"OpenCode server timed out at {self.base_url.rstrip('/')}") from error

    def create_session(self, directory, *, agent=None, model=None):
        return self.create_session_response(directory, agent=agent, model=model).data

    def create_session_response(self, directory, *, agent=None, model=None):
        payload = {"directory": directory}
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model
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

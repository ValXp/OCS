import json
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


class OpenCodeApiError(Exception):
    pass


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
        url = urljoin(self.base_url, path.lstrip("/"))
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as error:
            raise OpenCodeApiError(f"GET /{path.lstrip('/')} failed: HTTP {error.code}") from error
        except URLError as error:
            raise OpenCodeApiError(f"cannot reach OpenCode server at {self.base_url.rstrip('/')}: {error.reason}") from error
        except TimeoutError as error:
            raise OpenCodeApiError(f"OpenCode server timed out at {self.base_url.rstrip('/')}") from error

        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError as error:
            raise OpenCodeApiError(f"GET /{path.lstrip('/')} returned invalid JSON") from error

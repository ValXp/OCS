import json
import re
from urllib.parse import unquote, urljoin, urlsplit


HTTP_METHODS = frozenset(("delete", "get", "head", "options", "patch", "post", "put", "trace"))


class ApiDiagnosticsError(Exception):
    pass


class ApiDiagnosticsService:
    def __init__(self, client):
        self.client = client

    def list_routes(self, *, filter_text=None):
        paths = _openapi_paths(self.client.get_response_no_redirects("doc").data)
        needle = str(filter_text or "").lower()
        records = []
        for path in sorted(paths):
            route = paths[path]
            if not isinstance(route, dict):
                continue
            methods = sorted(str(method).upper() for method in route if str(method).lower() in HTTP_METHODS)
            if not methods:
                continue
            if needle and needle not in str(path).lower() and not any(needle in method.lower() for method in methods):
                continue
            records.append({"path": str(path), "methods": methods})
        return records

    def get(self, path):
        request_target, route_path = _read_only_target(path, self.client.base_url)
        routes = self.list_routes()
        if not any("GET" in route["methods"] and _matches_template(route["path"], route_path) for route in routes):
            raise ApiDiagnosticsError(f"OpenAPI document does not advertise GET {route_path}")
        return self.client.get_response_no_redirects(request_target)


def format_routes_compact(routes):
    return "\n".join(
        f"{method}\t{route['path']}"
        for route in routes
        for method in route["methods"]
    )


def format_diagnostics_compact(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _openapi_paths(document):
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise ApiDiagnosticsError("OpenAPI document must contain a paths object")
    return document["paths"]


def _read_only_target(value, base_url):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise ApiDiagnosticsError("diagnostic GET path must be an absolute same-server path beginning with /")
    if _has_unsafe_character(value):
        raise ApiDiagnosticsError("diagnostic GET path must not contain whitespace, control characters, or backslashes")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ApiDiagnosticsError("diagnostic GET path must not include a scheme, host, or fragment")
    _validate_path_forms(parsed.path, base_url)
    request_target = value
    _require_same_origin(base_url, request_target)
    return request_target, parsed.path


def _validate_path_forms(path, base_url):
    if re.search(r"%(?![0-9a-fA-F]{2})", path):
        raise ApiDiagnosticsError("diagnostic GET path contains an invalid percent escape")
    decoded = path
    for _index in range(len(path) + 1):
        _validate_path_form(decoded, base_url)
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            return
        if _path_delimiters(next_decoded) != _path_delimiters(decoded):
            raise ApiDiagnosticsError("diagnostic GET path must not contain encoded path delimiters")
        decoded = next_decoded
    raise ApiDiagnosticsError("diagnostic GET path contains excessive percent encoding")


def _validate_path_form(path, base_url):
    if not path.startswith("/") or path.startswith("//"):
        raise ApiDiagnosticsError("diagnostic GET path must remain an absolute same-server path when decoded")
    if _has_unsafe_character(path):
        raise ApiDiagnosticsError("diagnostic GET path must not contain whitespace, control characters, or backslashes")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ApiDiagnosticsError("diagnostic GET path must not contain dot segments")
    _require_same_origin(base_url, path)


def _path_delimiters(value):
    return tuple(character for character in value if character in "/\\?#")


def _has_unsafe_character(value):
    return any(character.isspace() or ord(character) < 32 or ord(character) == 127 or character == "\\" for character in value)


def _require_same_origin(base_url, request_target):
    resolved_url = urljoin(base_url.rstrip("/") + "/", request_target.lstrip("/"))
    if _origin(resolved_url) != _origin(base_url):
        raise ApiDiagnosticsError("diagnostic GET path resolves outside the configured OpenCode server")


def _origin(value):
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ApiDiagnosticsError(f"invalid OpenCode server URL {value!r}: invalid port") from error
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _matches_template(template, path):
    template_parts = str(template).rstrip("/").split("/")
    path_parts = str(path).rstrip("/").split("/")
    if len(template_parts) != len(path_parts):
        return False
    for template_part, path_part in zip(template_parts, path_parts):
        if (template_part.startswith("{") and template_part.endswith("}")) or template_part.startswith(":"):
            if not path_part:
                return False
            continue
        if re.fullmatch(re.escape(template_part), path_part) is None:
            return False
    return True

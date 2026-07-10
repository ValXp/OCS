import re
from urllib.parse import urlsplit


HTTP_METHODS = frozenset(("delete", "get", "head", "options", "patch", "post", "put", "trace"))


class ApiDiagnosticsError(Exception):
    pass


class ApiDiagnosticsService:
    def __init__(self, client):
        self.client = client

    def list_routes(self, *, filter_text=None):
        paths = _openapi_paths(self.client.require_openapi_doc())
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
        normalized_path = _read_only_path(path)
        routes = self.list_routes()
        if not any("GET" in route["methods"] and _matches_template(route["path"], normalized_path) for route in routes):
            raise ApiDiagnosticsError(f"OpenAPI document does not advertise GET {normalized_path}")
        return self.client.get_response(path)


def format_routes_compact(routes):
    return "\n".join(
        f"{method}\t{route['path']}"
        for route in routes
        for method in route["methods"]
    )


def _openapi_paths(document):
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise ApiDiagnosticsError("OpenAPI document must contain a paths object")
    return document["paths"]


def _read_only_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise ApiDiagnosticsError("diagnostic GET path must be an absolute same-server path beginning with /")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ApiDiagnosticsError("diagnostic GET path must not include a scheme, host, or fragment")
    return parsed.path


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

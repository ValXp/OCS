from urllib.parse import quote


PROJECT_ROUTE_SPECS = {
    "project_collection": ("/project", "GET"),
    "project_directories": ("/project/{projectID}/directories", "GET"),
    "workspace_collection": ("/experimental/workspace", "GET"),
    "workspace_item": ("/experimental/workspace/{workspaceID}", "DELETE"),
    "project_copy_refresh": ("/experimental/project/{projectID}/copy/refresh", "POST"),
}

PROJECT_ROUTE_PLAN = {name: path for name, (path, _method) in PROJECT_ROUTE_SPECS.items()}


def detect_project_route_availability(paths):
    return {
        name: {
            "path": path,
            "method": method,
            "available": _route_available(paths, path, method),
        }
        for name, (path, method) in PROJECT_ROUTE_SPECS.items()
    }


def render_project_route_path(path, *, project_id=None, workspace_id=None):
    rendered = str(path)
    if project_id is not None:
        rendered = _replace(rendered, project_id, "{projectID}", ":projectID", "{id}", ":id")
    if workspace_id is not None:
        rendered = _replace(rendered, workspace_id, "{workspaceID}", ":workspaceID", "{id}", ":id")
    return rendered


def _route_available(paths, path, method):
    for candidate in _path_variants(path):
        operations = paths.get(candidate) or {}
        if method.lower() in {name.lower() for name in operations}:
            return True
    return False


def _path_variants(path):
    variants = [path]
    replacements = (
        ("{projectID}", ":projectID"),
        ("{projectID}", "{id}"),
        ("{projectID}", ":id"),
        ("{workspaceID}", ":workspaceID"),
        ("{workspaceID}", "{id}"),
        ("{workspaceID}", ":id"),
    )
    for source, target in replacements:
        candidate = path.replace(source, target)
        if candidate not in variants:
            variants.append(candidate)
    return variants


def _replace(path, value, *placeholders):
    quoted = quote(str(value), safe="")
    for placeholder in placeholders:
        path = path.replace(placeholder, quoted)
    return path

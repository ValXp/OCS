from dataclasses import dataclass

from opencode_session.schema_event_codecs import API_EVENT_ROUTE
from opencode_session.project_routes import detect_project_route_availability
from opencode_session.schema_message_codecs import LEGACY_REPLY_PATH, LEGACY_RUN_PATH, SESSION_MESSAGE_PATH


SESSION_PATHS = ["/api/session", "/session"]
PROMPT_PATHS = ["/api/session/{sessionID}/prompt", "/session/{sessionID}/prompt_async"]
WAIT_PATHS = ["/api/session/{sessionID}/wait"]
EVENT_PATHS = [API_EVENT_ROUTE, "/event", "/global/event"]


@dataclass(frozen=True)
class OpenApiProfileDetection:
    health: str
    version: str
    route_availability: dict


def detect_openapi_profile(doc, *, health=None):
    health = health or {}
    paths = doc.get("paths") or {}

    session_path, session_available = _first_available_route(paths, SESSION_PATHS, "post")
    prompt_path, prompt_available = _first_available_route(paths, PROMPT_PATHS, "post")
    wait_path, wait_available = _first_available_route(paths, WAIT_PATHS, "post")
    if not wait_available and prompt_available and _query_parameter_available(paths, prompt_path, "post", "wait"):
        wait_path = f"{prompt_path}?wait=true"
        wait_available = True
    event_path, events_available = _first_available_route(paths, EVENT_PATHS, "get")
    blocking_message_available = _route_available(paths, SESSION_MESSAGE_PATH, "post")
    legacy_run_available = _route_available(paths, LEGACY_RUN_PATH, "post")
    legacy_reply_available = _route_available(paths, LEGACY_REPLY_PATH, "post")

    route_availability = {
        "session": _route(session_path, "POST", session_available),
        "v2_prompt": _route(prompt_path, "POST", prompt_available),
        "v2_wait": _route(wait_path, "POST", wait_available),
        "events": _route(event_path, "GET", events_available),
        "blocking_message": _route(SESSION_MESSAGE_PATH, "POST", blocking_message_available),
        "legacy_run": _route(LEGACY_RUN_PATH, "POST", legacy_run_available),
        "legacy_reply": _route(LEGACY_REPLY_PATH, "POST", legacy_reply_available),
    }
    route_availability.update(detect_project_route_availability(paths))

    return OpenApiProfileDetection(
        health=_health_status(health),
        version=str(health.get("version") or health.get("serverVersion") or "unknown"),
        route_availability=route_availability,
    )


def _route(path, method, available):
    return {"path": path, "method": method, "available": available}


def _first_available_route(paths, candidates, method):
    for path in candidates:
        if _route_available(paths, path, method):
            return path, True
    return candidates[0], False


def _route_available(paths, path, method):
    for candidate in _path_variants(path):
        route = paths.get(candidate) or {}
        if method.lower() in {key.lower() for key in route.keys()}:
            return True
    return False


def _query_parameter_available(paths, path, method, name):
    for candidate in _path_variants(path):
        operation = (paths.get(candidate) or {}).get(method.lower()) or {}
        parameters = operation.get("parameters") or []
        if any(parameter.get("name") == name for parameter in parameters):
            return True
    return False


def _path_variants(path):
    variants = [path]
    colon = path.replace("{sessionID}", ":sessionID")
    if colon not in variants:
        variants.append(colon)
    legacy_id = path.replace("{sessionID}", "{id}")
    if legacy_id not in variants:
        variants.append(legacy_id)
    return variants


def _health_status(health):
    if "status" in health:
        return str(health["status"])
    if health.get("healthy") is True or health.get("ok") is True:
        return "ok"
    return "unknown"

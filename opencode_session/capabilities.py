SESSION_PATHS = ["/api/session", "/session"]
PROMPT_PATHS = ["/api/session/{sessionID}/prompt", "/session/{sessionID}/prompt_async"]
WAIT_PATHS = ["/api/session/{sessionID}/wait"]
EVENT_PATHS = ["/api/event", "/event", "/global/event"]
SESSION_MESSAGE_PATH = "/session/{sessionID}/message"
LEGACY_RUN_PATH = "/session/{sessionID}/run"
LEGACY_REPLY_PATH = "/session/{sessionID}/reply"


def detect_capabilities(client, *, deadline=None):
    health = client.get_health(deadline=deadline)
    doc = client.get_openapi_doc(deadline=deadline)
    capabilities = capabilities_from_openapi_doc(doc, health=health)
    configure = getattr(client, "configure_route_plan", None)
    if callable(configure):
        configure(capabilities["route_plan"])
    return capabilities


def capabilities_from_openapi_doc(doc, *, health=None):
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
    legacy_fallback_available = legacy_run_available and legacy_reply_available

    route_availability = {
        "session": _route(session_path, "POST", session_available),
        "v2_prompt": _route(prompt_path, "POST", prompt_available),
        "v2_wait": _route(wait_path, "POST", wait_available),
        "events": _route(event_path, "GET", events_available),
        "blocking_message": _route(SESSION_MESSAGE_PATH, "POST", blocking_message_available),
        "legacy_run": _route(LEGACY_RUN_PATH, "POST", legacy_run_available),
        "legacy_reply": _route(LEGACY_REPLY_PATH, "POST", legacy_reply_available),
    }
    route_plan = route_plan_from_availability(route_availability)

    return {
        "health": _health_status(health),
        "version": str(health.get("version") or health.get("serverVersion") or "unknown"),
        "route_availability": route_availability,
        "route_plan": route_plan,
        "v2_prompt_support": prompt_available,
        "v2_wait_support": wait_available,
        "event_support": events_available,
        "blocking_message_available": blocking_message_available,
        "blocking_execution_available": blocking_message_available or legacy_fallback_available,
        "legacy_fallback_available": legacy_fallback_available,
    }


def route_plan_from_availability(route_availability):
    session_path = _planned_route_path(route_availability, "session", SESSION_PATHS[0])
    return {
        "session_collection": session_path,
        "session_item": _session_item_path(session_path),
        "v2_prompt": _planned_route_path(route_availability, "v2_prompt", PROMPT_PATHS[0]),
        "v2_wait": _planned_route_path(route_availability, "v2_wait", WAIT_PATHS[0]),
        "events": _planned_route_path(route_availability, "events", EVENT_PATHS[0]),
        "blocking_message": _planned_route_path(route_availability, "blocking_message", SESSION_MESSAGE_PATH),
        "legacy_run": _planned_route_path(route_availability, "legacy_run", LEGACY_RUN_PATH),
        "legacy_reply": _planned_route_path(route_availability, "legacy_reply", LEGACY_REPLY_PATH),
    }


def format_compact(capabilities):
    route_availability = capabilities["route_availability"]
    wait = route_availability["v2_wait"]["path"] if route_availability["v2_wait"]["available"] else "unsupported"
    legacy = "unsupported"
    if route_availability["legacy_run"]["available"] and route_availability["legacy_reply"]["available"]:
        legacy = f"{route_availability['legacy_run']['path']},{route_availability['legacy_reply']['path']}"
    execution = "unsupported"
    if route_availability["blocking_message"]["available"]:
        execution = route_availability["blocking_message"]["path"]
    elif legacy != "unsupported":
        execution = legacy

    return " ".join(
        [
            f"health={capabilities['health']}",
            f"version={capabilities['version']}",
            f"session={route_availability['session']['path'] if route_availability['session']['available'] else 'unsupported'}",
            f"prompt={route_availability['v2_prompt']['path'] if route_availability['v2_prompt']['available'] else 'unsupported'}",
            f"wait={wait}",
            f"events={route_availability['events']['path'] if route_availability['events']['available'] else 'unsupported'}",
            f"execution={execution}",
            f"legacy={legacy}",
        ]
    )


def unsupported_reasons(capabilities):
    route_availability = capabilities["route_availability"]
    reasons = []
    if not route_availability["session"]["available"]:
        reasons.append("missing session control: POST /api/session or POST /session")
    if not capabilities["v2_prompt_support"] and not capabilities["blocking_execution_available"]:
        reasons.append(
            "missing prompt admission or blocking execution: POST /api/session/{sessionID}/prompt, "
            "POST /session/{sessionID}/message, or legacy "
            "POST /session/{sessionID}/run + POST /session/{sessionID}/reply"
        )
    return reasons


def _planned_route_path(route_availability, name, fallback):
    route = route_availability.get(name) or {}
    if route.get("available") and route.get("path"):
        return route["path"]
    return fallback


def _session_item_path(session_collection_path):
    return f"{session_collection_path.rstrip('/')}/{{sessionID}}"


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

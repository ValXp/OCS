from opencode_session.api_profile import (
    EVENT_PATHS,
    LEGACY_REPLY_PATH,
    LEGACY_RUN_PATH,
    PROMPT_PATHS,
    SESSION_MESSAGE_PATH,
    SESSION_PATHS,
    WAIT_PATHS,
    OpenCodeServerProfile,
    route_plan_from_availability,
    server_profile_from_capabilities,
)


def detect_capabilities(client, *, deadline=None):
    health = client.get_health(deadline=deadline)
    doc = client.get_openapi_doc(deadline=deadline)
    return capabilities_from_openapi_doc(doc, health=health)


def configure_client_route_plan(client, capabilities):
    profile = server_profile_from_capabilities(capabilities)
    configure_profile = getattr(client, "configure_server_profile", None)
    if callable(configure_profile):
        configure_profile(profile)
        return client
    configure = getattr(client, "configure_route_plan", None)
    if callable(configure):
        configure(profile.route_plan)
    return client


def capabilities_from_openapi_doc(doc, *, health=None):
    return OpenCodeServerProfile.from_openapi_doc(doc, health=health).to_capabilities()


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

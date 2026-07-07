from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import quote

from opencode_session.api_profile_detection import (
    EVENT_PATHS,
    PROMPT_PATHS,
    SESSION_PATHS,
    WAIT_PATHS,
    detect_openapi_profile,
)
from opencode_session.schema_admission_adapter import ADMISSION_ADAPTER
from opencode_session.schema_event_adapter import event_adapter_for_route
from opencode_session.schema_message_adapter import (
    LEGACY_REPLY_PATH,
    LEGACY_RUN_PATH,
    SESSION_MESSAGE_PATH,
    UNKNOWN_MESSAGE_ADAPTER,
    message_adapter_for_endpoint,
    normalize_message_record as _normalize_message_record,
)
from opencode_session.schema_session_adapter import LEGACY_SESSION_ADAPTER, session_adapter_for_route


PUBLIC_ROUTE_PLAN_KEYS = (
    "session_collection",
    "session_item",
    "v2_prompt",
    "v2_wait",
    "events",
    "blocking_message",
    "legacy_run",
    "legacy_reply",
)

DEFAULT_ROUTE_PLAN = {
    "session_collection": "/api/session",
    "session_item": "/api/session/{sessionID}",
    "v2_prompt": "/api/session/{sessionID}/prompt",
    "v2_wait": "/api/session/{sessionID}/wait",
    "events": "/api/event",
    "blocking_message": "/session/{sessionID}/message",
    "legacy_run": "/session/{sessionID}/run",
    "legacy_reply": "/session/{sessionID}/reply",
    "session_abort": "/session/{sessionID}/abort",
    "session_fork": "/session/{sessionID}/fork",
    "session_children": "/session/{sessionID}/children",
    "permissions": "/permission",
    "permission_reply": "/permission/{requestID}/reply",
    "questions": "/question",
    "question_reply": "/question/{requestID}/reply",
    "question_reject": "/question/{requestID}/reject",
}


@dataclass(frozen=True)
class OpenCodeServerProfile:
    health: str
    version: str
    route_availability: dict
    route_plan: dict
    adapters: dict

    @classmethod
    def default(cls):
        return cls.from_route_plan({})

    @classmethod
    def from_openapi_doc(cls, doc, *, health=None):
        detection = detect_openapi_profile(doc, health=health)

        return cls.from_route_plan(
            route_plan_from_availability(detection.route_availability),
            health=detection.health,
            version=detection.version,
            route_availability=detection.route_availability,
        )

    @classmethod
    def from_capabilities(cls, capabilities):
        if isinstance(capabilities, cls):
            return capabilities
        if not isinstance(capabilities, dict):
            return cls.default()
        route_availability = deepcopy(capabilities.get("route_availability") or {})
        route_plan = capabilities.get("route_plan") or route_plan_from_availability(route_availability)
        return cls.from_route_plan(
            route_plan,
            health=str(capabilities.get("health") or "unknown"),
            version=str(capabilities.get("version") or "unknown"),
            route_availability=route_availability,
        )

    @classmethod
    def from_route_plan(cls, route_plan, *, health="unknown", version="unknown", route_availability=None):
        if isinstance(route_plan, cls):
            return route_plan
        full_route_plan = _complete_route_plan(route_plan, route_availability=route_availability)
        return cls(
            health=health,
            version=version,
            route_availability=deepcopy(route_availability or {}),
            route_plan=full_route_plan,
            adapters=_adapters_for_route_plan(full_route_plan),
        )

    @property
    def public_route_plan(self):
        return {name: self.route_plan[name] for name in PUBLIC_ROUTE_PLAN_KEYS}

    def to_capabilities(self):
        legacy_fallback_available = self.route_available("legacy_run") and self.route_available("legacy_reply")
        blocking_message_available = self.route_available("blocking_message")
        return {
            "health": self.health,
            "version": self.version,
            "route_availability": deepcopy(self.route_availability),
            "route_plan": self.public_route_plan,
            "v2_prompt_support": self.route_available("v2_prompt"),
            "v2_wait_support": self.route_available("v2_wait"),
            "event_support": self.route_available("events"),
            "blocking_message_available": blocking_message_available,
            "blocking_execution_available": blocking_message_available or legacy_fallback_available,
            "legacy_fallback_available": legacy_fallback_available,
        }

    def route_available(self, name):
        route = (self.route_availability or {}).get(name) or {}
        return bool(route.get("available"))

    def adapter(self, endpoint):
        return self.adapters.get(endpoint)

    def normalize_session_payload(self, payload, *, endpoint="session_collection"):
        return self._session_adapter(endpoint).normalize_payload(payload)

    def normalize_session_record(self, record, *, endpoint="session_collection"):
        return self._session_adapter(endpoint).normalize_record(record)

    def normalize_event_record(self, event, target_session_id=None):
        return self.adapters["events"].normalize_record(event, target_session_id)

    def normalize_message_record(self, message, *, endpoint="legacy_run"):
        return _normalize_message_record(message, route=self.message_route(endpoint))

    def normalize_admission_record(self, session_id, delivery, message_id, data, *, capabilities=None):
        return self.adapters["v2_prompt"].normalize_record(
            session_id,
            delivery,
            message_id,
            data,
            capabilities=capabilities or self.to_capabilities(),
        )

    def prompt_admission_payload(self, message_id, text, delivery):
        prompt_path = self.route_plan.get("v2_prompt") or PROMPT_PATHS[0]
        if _route_key(prompt_path) == PROMPT_PATHS[0]:
            return {"id": message_id, "prompt": {"text": text}, "delivery": delivery}
        return {
            "messageID": message_id,
            "parts": [{"type": "text", "text": text}],
            "delivery": delivery,
        }

    def blocking_execution_strategy(self, capabilities=None):
        if isinstance(capabilities, dict):
            routes = capabilities.get("route_availability") or {}
            blocking_message = routes.get("blocking_message") or {}
            if capabilities.get("blocking_message_available") or blocking_message.get("available"):
                return "session_message"
            if capabilities.get("legacy_fallback_available"):
                return "legacy_run_reply"
        if self.route_available("blocking_message"):
            return "session_message"
        if self.route_available("legacy_run") and self.route_available("legacy_reply"):
            return "legacy_run_reply"
        return None

    def message_route(self, endpoint):
        adapter = self.adapters.get(endpoint) or UNKNOWN_MESSAGE_ADAPTER
        return adapter.route

    def blocking_api_path(self):
        return {"message": self.route_plan.get("blocking_message", SESSION_MESSAGE_PATH)}

    def legacy_api_path(self):
        return {
            "run": self.route_plan.get("legacy_run", LEGACY_RUN_PATH),
            "reply": self.route_plan.get("legacy_reply", LEGACY_REPLY_PATH),
        }

    def _session_adapter(self, endpoint):
        return self.adapters.get(endpoint) or session_adapter_for_route(self.route_plan.get(endpoint))


def server_profile_from_capabilities(capabilities):
    return OpenCodeServerProfile.from_capabilities(capabilities)


def route_plan_from_availability(route_availability, *, include_domain=False):
    session_path = _planned_route_path(route_availability, "session", SESSION_PATHS[0])
    route_plan = {
        "session_collection": session_path,
        "session_item": _session_item_path(session_path),
        "v2_prompt": _planned_route_path(route_availability, "v2_prompt", PROMPT_PATHS[0]),
        "v2_wait": _planned_route_path(route_availability, "v2_wait", WAIT_PATHS[0]),
        "events": _planned_route_path(route_availability, "events", EVENT_PATHS[0]),
        "blocking_message": _planned_route_path(route_availability, "blocking_message", SESSION_MESSAGE_PATH),
        "legacy_run": _planned_route_path(route_availability, "legacy_run", LEGACY_RUN_PATH),
        "legacy_reply": _planned_route_path(route_availability, "legacy_reply", LEGACY_REPLY_PATH),
    }
    if include_domain:
        return {**DEFAULT_ROUTE_PLAN, **route_plan}
    return route_plan


def render_route_path(path, *, session_id=None, request_id=None):
    rendered = str(path)
    if session_id is not None:
        rendered = _replace_placeholders(rendered, session_id, "{sessionID}", ":sessionID", "{id}", ":id")
    if request_id is not None:
        rendered = _replace_placeholders(rendered, request_id, "{requestID}", ":requestID", "{id}", ":id")
    return rendered


def _replace_placeholders(path, value, *placeholders):
    quoted_value = quote(value, safe="")
    for placeholder in placeholders:
        path = path.replace(placeholder, quoted_value)
    return path


def _complete_route_plan(route_plan, *, route_availability=None):
    route_plan = dict(route_plan or {})
    full_route_plan = dict(DEFAULT_ROUTE_PLAN)
    if route_availability is not None:
        full_route_plan.update(route_plan_from_availability(route_availability, include_domain=True))
    full_route_plan.update(route_plan)
    if "session_collection" in route_plan and "session_item" not in route_plan:
        full_route_plan["session_item"] = _session_item_path(full_route_plan["session_collection"])
    return full_route_plan


def _adapters_for_route_plan(route_plan):
    session_adapter = session_adapter_for_route(route_plan.get("session_collection"))
    return {
        "session_collection": session_adapter,
        "session_item": session_adapter,
        "session_children": LEGACY_SESSION_ADAPTER,
        "events": event_adapter_for_route(route_plan.get("events")),
        "v2_prompt": ADMISSION_ADAPTER,
        "blocking_message": message_adapter_for_endpoint("blocking_message", route_plan.get("blocking_message")),
        "legacy_run": message_adapter_for_endpoint("legacy_run", route_plan.get("legacy_run")),
        "legacy_reply": message_adapter_for_endpoint("legacy_reply", route_plan.get("legacy_reply")),
    }


def _planned_route_path(route_availability, name, fallback):
    route = (route_availability or {}).get(name) or {}
    if route.get("available") and route.get("path"):
        return route["path"]
    return fallback


def _session_item_path(session_collection_path):
    return f"{session_collection_path.rstrip('/')}/{{sessionID}}"


def _route_key(path):
    return str(path).split("?", 1)[0].rstrip("/")

import json
from copy import deepcopy
from dataclasses import dataclass

from opencode_session.schema_common import (
    NormalizedEventRecord,
    first_present,
    first_present_in,
    first_mapping_at_paths,
    mapping_at_path,
    mapping_value,
    set_if_present,
    string_value,
)
from opencode_session.status import short_status


SUCCESS_STATUSES = {"complete", "completed", "done", "idle", "success", "succeeded"}
ABORT_STATUSES = {"abort", "aborted", "cancelled", "canceled"}

BLOCKER_EVENT_TYPES = frozenset({"permission.requested", "question.requested", "blocker.requested"})
ADMISSION_EVENT_TYPES = frozenset({"session.prompt.admitted", "session.prompt.promoted", "session.prompt.queued"})
PROMPT_EVENT_TYPES = frozenset({"session.prompt.started", "session.prompt.updated", "session.prompt.completed"})
STEP_EVENT_TYPES = frozenset({"session.step.started", "session.step.updated", "session.step.completed"})
STATUS_EVENT_TYPES = frozenset({"session.status", "session.idle"})
TEXT_EVENT_TYPES = frozenset({"message.part.updated", "message.part.delta", "message.text.delta"})
TOOL_EVENT_TYPES = frozenset({"tool.execute.started", "tool.execute.updated", "tool.execute.completed"})
ERROR_EVENT_TYPES = frozenset({"message.error", "session.error", "tool.execute.error"})


@dataclass(frozen=True)
class EventMappingSource:
    root: str
    path: tuple = ()


@dataclass(frozen=True)
class EventRouteSchema:
    payload_paths: tuple
    source_paths: tuple
    tool_paths: tuple
    error_paths: tuple
    field_aliases: dict
    kind_event_types: dict


EVENT_ROOT = "event"
PAYLOAD_ROOT = "payload"

EVENT_FIELD_ALIASES = {
    "session_id": ("sessionID", "sessionId", "session_id"),
    "nested_session_id": ("id", "sessionID", "sessionId", "session_id"),
    "event_type": ("type", "event", "name", "kind"),
    "status": ("status", "state", "phase"),
    "error_text": ("error", "reason"),
    "tool_name": ("toolName", "tool_name", "tool"),
    "call_id": ("callID", "callId", "toolCallID", "toolCallId", "tool_call_id"),
    "message_id": ("messageID", "messageId", "message_id", "promptID", "promptId", "id"),
    "text": ("delta", "text", "content"),
    "delivery": ("delivery", "deliveryMode", "mode"),
    "step": ("step", "stepID", "stepId", "step_id"),
    "title": ("title", "description"),
    "question": ("question", "prompt", "title"),
    "blocker_permission": ("permission", "permissionID", "permissionId"),
    "blocker_question": ("question", "questionID", "questionId"),
    "blocker": ("blocker", "blockerID", "blockerId"),
    "blocker_id": (
        "permissionID",
        "permissionId",
        "permission_id",
        "questionID",
        "questionId",
        "question_id",
        "blockerID",
        "blockerId",
        "blocker_id",
    ),
}

EVENT_KIND_CRITERIA = {
    "blocker": BLOCKER_EVENT_TYPES,
    "error": ERROR_EVENT_TYPES,
    "text": TEXT_EVENT_TYPES,
    "tool": TOOL_EVENT_TYPES,
    "admission": ADMISSION_EVENT_TYPES,
    "prompt": PROMPT_EVENT_TYPES,
    "step": STEP_EVENT_TYPES,
    "status": STATUS_EVENT_TYPES,
}

COMPATIBLE_EVENT_SOURCE_PATHS = (
    EventMappingSource(EVENT_ROOT),
    EventMappingSource(PAYLOAD_ROOT),
    EventMappingSource(EVENT_ROOT, ("info",)),
    EventMappingSource(PAYLOAD_ROOT, ("info",)),
    EventMappingSource(EVENT_ROOT, ("part",)),
    EventMappingSource(PAYLOAD_ROOT, ("part",)),
    EventMappingSource(EVENT_ROOT, ("message",)),
    EventMappingSource(PAYLOAD_ROOT, ("message",)),
    EventMappingSource(EVENT_ROOT, ("tool",)),
    EventMappingSource(PAYLOAD_ROOT, ("tool",)),
)

OPENAPI_EVENT_SOURCE_PATHS = tuple(
    source
    for source in COMPATIBLE_EVENT_SOURCE_PATHS
    if source.path != ("info",)
)

TOOL_SOURCE_PATHS = (
    EventMappingSource(EVENT_ROOT, ("tool",)),
    EventMappingSource(PAYLOAD_ROOT, ("tool",)),
)
ERROR_SOURCE_PATHS = (
    EventMappingSource(EVENT_ROOT, ("error",)),
    EventMappingSource(PAYLOAD_ROOT, ("error",)),
)

COMPATIBLE_EVENT_SCHEMA = EventRouteSchema(
    payload_paths=(("properties",), ("payload",), ("data",)),
    source_paths=COMPATIBLE_EVENT_SOURCE_PATHS,
    tool_paths=TOOL_SOURCE_PATHS,
    error_paths=ERROR_SOURCE_PATHS,
    field_aliases=EVENT_FIELD_ALIASES,
    kind_event_types=EVENT_KIND_CRITERIA,
)
OPENAPI_EVENT_SCHEMA = EventRouteSchema(
    payload_paths=(("properties",),),
    source_paths=OPENAPI_EVENT_SOURCE_PATHS,
    tool_paths=TOOL_SOURCE_PATHS,
    error_paths=ERROR_SOURCE_PATHS,
    field_aliases=EVENT_FIELD_ALIASES,
    kind_event_types=EVENT_KIND_CRITERIA,
)
LEGACY_EVENT_SCHEMA = EventRouteSchema(
    payload_paths=(("payload",), ("data",), ("properties",)),
    source_paths=COMPATIBLE_EVENT_SOURCE_PATHS,
    tool_paths=TOOL_SOURCE_PATHS,
    error_paths=ERROR_SOURCE_PATHS,
    field_aliases=EVENT_FIELD_ALIASES,
    kind_event_types=EVENT_KIND_CRITERIA,
)


@dataclass(frozen=True)
class EventRouteAdapter:
    schema: EventRouteSchema
    route: str = "event"
    version: str = "compatible"

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        if not isinstance(event, dict):
            return unknown_event_record(event)
        payload = first_mapping_at_paths(event, self.schema.payload_paths)
        sources = _event_sources(event, payload, self.schema.source_paths)
        tool = _first_mapping_from_sources(event, payload, self.schema.tool_paths)
        error = _first_mapping_from_sources(event, payload, self.schema.error_paths)

        session_id = _event_session_id(sources, self.schema.field_aliases)
        event_type = self.value(sources, "event_type")
        event_type_text = string_value(event_type)
        if target_session_id is not None and session_id != target_session_id:
            return ignored_event_record(session_id, target_session_id, event_type_text)

        status = self.value(sources, "status")
        raw_status = string_value(status)
        text = _event_text_value(sources, self.schema.field_aliases)
        error_text = _error_text(error) or string_value(self.value(sources, "error_text"))
        tool_name = _tool_name(tool) or string_value(self.value([event, payload], "tool_name"))
        call_id = string_value(self.value(sources, "call_id"))
        kind = _event_kind(event_type_text, text, tool_name, call_id, error_text, sources, self.schema)

        if kind == "unknown":
            return unknown_event_record(event, event_type=event_type_text, session_id=session_id)

        normalized = {"kind": kind, "schema_status": "known"}
        set_if_present(normalized, "session_id", session_id)
        set_if_present(normalized, "type", event_type_text)
        set_if_present(normalized, "message_id", _event_message_id(sources, self.schema.field_aliases))
        set_if_present(normalized, "status", short_status(raw_status))
        if raw_status is not None and short_status(raw_status) != raw_status:
            normalized["raw_status"] = raw_status
        set_if_present(normalized, "delivery", string_value(self.value(sources, "delivery")))
        set_if_present(normalized, "text", text)
        set_if_present(normalized, "tool", tool_name)
        set_if_present(normalized, "call_id", call_id)
        set_if_present(normalized, "step", string_value(self.value(sources, "step")))
        set_if_present(normalized, "title", string_value(self.value(sources, "title")))
        blocker = _blocker_type(event_type, sources, self.schema.field_aliases, self.schema.kind_event_types)
        set_if_present(normalized, "blocker", blocker)
        if blocker is not None:
            set_if_present(normalized, "blocker_id", _blocker_id(sources, self.schema.field_aliases))
            set_if_present(normalized, "question", string_value(self.value(sources, "question")))
        set_if_present(normalized, "error", error_text)
        return normalized

    def value(self, sources, field_name):
        return first_present_in(sources, *self.schema.field_aliases[field_name])


@dataclass(frozen=True)
class OpenApiEventRouteAdapter(EventRouteAdapter):
    schema: EventRouteSchema = OPENAPI_EVENT_SCHEMA
    version: str = "api-v1"


@dataclass(frozen=True)
class LegacyEventRouteAdapter(EventRouteAdapter):
    schema: EventRouteSchema = LEGACY_EVENT_SCHEMA
    version: str = "legacy"


def _event_kind(event_type, text, tool_name, call_id, error_text, sources, schema):
    normalized_type = str(event_type or "").lower()
    criteria = schema.kind_event_types
    if _blocker_type(event_type, sources, schema.field_aliases, criteria):
        return "blocker"
    if normalized_type in criteria["error"] or error_text is not None:
        return "error"
    if normalized_type in criteria["text"] and text is not None:
        return "text"
    if normalized_type in criteria["tool"] and (tool_name is not None or call_id is not None):
        return "tool"
    if normalized_type in criteria["admission"]:
        return "admission"
    if normalized_type in criteria["prompt"]:
        return "prompt"
    if normalized_type in criteria["step"]:
        return "step"
    if normalized_type in criteria["status"]:
        return "status"
    return "unknown"


def ignored_event_record(session_id, target_session_id, event_type) -> NormalizedEventRecord:
    normalized = {
        "kind": "ignored",
        "schema_status": "known",
        "target_session_id": target_session_id,
        "reason": "session_mismatch",
    }
    set_if_present(normalized, "session_id", session_id)
    set_if_present(normalized, "type", event_type)
    return normalized


def unknown_event_record(raw, *, event_type=None, session_id=None) -> NormalizedEventRecord:
    normalized = {
        "kind": "unknown",
        "schema_status": "unknown",
        "reason": "unrecognized_event_shape",
        "raw": deepcopy(raw),
    }
    set_if_present(normalized, "session_id", session_id)
    set_if_present(normalized, "type", event_type)
    return normalized


def _event_sources(event, payload, source_paths):
    return [_mapping_from_source(event, payload, source) for source in source_paths]


def _first_mapping_from_sources(event, payload, source_paths):
    for source in source_paths:
        value = _mapping_from_source(event, payload, source)
        if value is not None:
            return value
    return None


def _mapping_from_source(event, payload, source):
    root = event if source.root == EVENT_ROOT else payload
    return mapping_at_path(root, source.path)


def _event_session_id(sources, aliases):
    value = first_present_in(sources, *aliases["session_id"])
    if value is not None:
        return str(value)
    for source in sources:
        session = mapping_value(source, "session")
        value = first_present(session, *aliases["nested_session_id"])
        if value is not None:
            return str(value)
    return None


def _event_message_id(sources, aliases):
    value = first_present_in(sources, *aliases["message_id"])
    if value is not None:
        return str(value)
    return None


def _event_text_value(sources, aliases):
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type") == "text" and source.get("text") is not None:
            return str(source["text"])
        value = first_present(source, *aliases["text"])
        if isinstance(value, str):
            return value
    return None


def _tool_name(tool):
    if isinstance(tool, dict):
        value = first_present(tool, "name", "tool", "toolName", "tool_name")
        if value is not None:
            return str(value)
    elif tool is not None:
        return str(tool)
    return None


def _blocker_type(event_type, sources, aliases, kind_event_types):
    normalized_type = str(event_type or "").lower()
    if normalized_type == "permission.requested":
        return "permission"
    if normalized_type == "question.requested":
        return "question"
    if normalized_type in kind_event_types["blocker"]:
        return "blocker"
    if first_present_in(sources, *aliases["blocker_permission"]) is not None:
        return "permission"
    if first_present_in(sources, *aliases["blocker_question"]) is not None:
        return "question"
    if first_present_in(sources, *aliases["blocker"]) is not None:
        return "blocker"
    return None


def _blocker_id(sources, aliases):
    value = first_present_in(sources, *aliases["blocker_id"])
    if value is not None:
        return str(value)
    return None


def _error_text(error):
    if isinstance(error, dict):
        value = first_present(error, "message", "detail", "error")
        if value is not None:
            return str(value)
        return json.dumps(error, sort_keys=True)
    if error is not None:
        return str(error)
    return None


def event_adapter_for_route(route_path=None):
    normalized_path = str(route_path or "").split("?", 1)[0].rstrip("/")
    if normalized_path == "/api/event":
        return OPENAPI_EVENT_ADAPTER
    if normalized_path in {"/event", "/global/event"}:
        return LEGACY_EVENT_ADAPTER
    return EVENT_ADAPTER


EVENT_ADAPTER = EventRouteAdapter(COMPATIBLE_EVENT_SCHEMA)
OPENAPI_EVENT_ADAPTER = OpenApiEventRouteAdapter()
LEGACY_EVENT_ADAPTER = LegacyEventRouteAdapter()

def normalize_event_record(event, target_session_id=None, *, route_path=None):
    return event_adapter_for_route(route_path).normalize_record(event, target_session_id)

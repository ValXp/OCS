import json
from copy import deepcopy
from dataclasses import dataclass

from opencode_session.schema_common import (
    NormalizedEventRecord,
    first_present,
    first_present_in,
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
class EventRouteAdapter:
    route: str = "event"
    version: str = "compatible"

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        if not isinstance(event, dict):
            return unknown_event_record(event)
        properties = self.properties(event)
        info = self.info(event, properties)
        part = self.part(event, properties)
        message = self.message(event, properties)
        tool = self.tool(event, properties)
        error = self.error(event, properties)
        sources = self.sources(event, properties, info, part, message, tool)

        session_id = _event_session_id(sources)
        event_type = first_present_in(sources, "type", "event", "name", "kind")
        event_type_text = string_value(event_type)
        if target_session_id is not None and session_id != target_session_id:
            return ignored_event_record(session_id, target_session_id, event_type_text)

        status = first_present_in(sources, "status", "state", "phase")
        raw_status = string_value(status)
        text = _event_text_value(sources)
        error_text = _error_text(error) or string_value(first_present_in(sources, "error", "reason"))
        tool_name = _tool_name(tool) or string_value(first_present_in([event, properties], "toolName", "tool_name", "tool"))
        call_id = string_value(first_present_in(sources, "callID", "callId", "toolCallID", "toolCallId", "tool_call_id"))
        kind = _event_kind(event_type_text, text, tool_name, call_id, error_text, sources)

        if kind == "unknown":
            return unknown_event_record(event, event_type=event_type_text, session_id=session_id)

        normalized = {"kind": kind, "schema_status": "known"}
        set_if_present(normalized, "session_id", session_id)
        set_if_present(normalized, "type", event_type_text)
        set_if_present(normalized, "message_id", _event_message_id(sources))
        set_if_present(normalized, "status", short_status(raw_status))
        if raw_status is not None and short_status(raw_status) != raw_status:
            normalized["raw_status"] = raw_status
        set_if_present(normalized, "delivery", string_value(first_present_in(sources, "delivery", "deliveryMode", "mode")))
        set_if_present(normalized, "text", text)
        set_if_present(normalized, "tool", tool_name)
        set_if_present(normalized, "call_id", call_id)
        set_if_present(normalized, "step", string_value(first_present_in(sources, "step", "stepID", "stepId", "step_id")))
        set_if_present(normalized, "title", string_value(first_present_in(sources, "title", "description")))
        blocker = _blocker_type(event_type, sources)
        set_if_present(normalized, "blocker", blocker)
        if blocker is not None:
            set_if_present(normalized, "blocker_id", _blocker_id(sources))
            set_if_present(normalized, "question", string_value(first_present_in(sources, "question", "prompt", "title")))
        set_if_present(normalized, "error", error_text)
        return normalized

    def properties(self, event):
        return mapping_value(event, "properties") or mapping_value(event, "payload") or mapping_value(event, "data")

    def info(self, event, properties):
        return mapping_value(event, "info") or mapping_value(properties, "info")

    def part(self, event, properties):
        return mapping_value(event, "part") or mapping_value(properties, "part")

    def message(self, event, properties):
        return mapping_value(event, "message") or mapping_value(properties, "message")

    def tool(self, event, properties):
        return mapping_value(event, "tool") or mapping_value(properties, "tool")

    def error(self, event, properties):
        return mapping_value(event, "error") or mapping_value(properties, "error")

    def sources(self, event, properties, info, part, message, tool):
        return [event, properties, info, part, message, tool]


@dataclass(frozen=True)
class OpenApiEventRouteAdapter(EventRouteAdapter):
    version: str = "api-v1"

    def properties(self, event):
        return mapping_value(event, "properties")

    def info(self, event, properties):
        return None

    def sources(self, event, properties, info, part, message, tool):
        return [event, properties, part, message, tool]


@dataclass(frozen=True)
class LegacyEventRouteAdapter(EventRouteAdapter):
    version: str = "legacy"

    def properties(self, event):
        return mapping_value(event, "payload") or mapping_value(event, "data") or mapping_value(event, "properties")


def _event_kind(event_type, text, tool_name, call_id, error_text, sources):
    normalized_type = str(event_type or "").lower()
    if _blocker_type(event_type, sources):
        return "blocker"
    if normalized_type in ERROR_EVENT_TYPES or error_text is not None:
        return "error"
    if normalized_type in TEXT_EVENT_TYPES and text is not None:
        return "text"
    if normalized_type in TOOL_EVENT_TYPES and (tool_name is not None or call_id is not None):
        return "tool"
    if normalized_type in ADMISSION_EVENT_TYPES:
        return "admission"
    if normalized_type in PROMPT_EVENT_TYPES:
        return "prompt"
    if normalized_type in STEP_EVENT_TYPES:
        return "step"
    if normalized_type in STATUS_EVENT_TYPES:
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


def _event_session_id(sources):
    value = first_present_in(sources, "sessionID", "sessionId", "session_id")
    if value is not None:
        return str(value)
    for source in sources:
        session = mapping_value(source, "session")
        value = first_present(session, "id", "sessionID", "sessionId", "session_id")
        if value is not None:
            return str(value)
    return None


def _event_message_id(sources):
    value = first_present_in(sources, "messageID", "messageId", "message_id", "promptID", "promptId", "id")
    if value is not None:
        return str(value)
    return None


def _event_text_value(sources):
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type") == "text" and source.get("text") is not None:
            return str(source["text"])
        value = first_present(source, "delta", "text", "content")
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


def _blocker_type(event_type, sources):
    normalized_type = str(event_type or "").lower()
    if normalized_type == "permission.requested":
        return "permission"
    if normalized_type == "question.requested":
        return "question"
    if normalized_type in BLOCKER_EVENT_TYPES:
        return "blocker"
    if first_present_in(sources, "permission", "permissionID", "permissionId") is not None:
        return "permission"
    if first_present_in(sources, "question", "questionID", "questionId") is not None:
        return "question"
    if first_present_in(sources, "blocker", "blockerID", "blockerId") is not None:
        return "blocker"
    return None


def _blocker_id(sources):
    value = first_present_in(
        sources,
        "permissionID",
        "permissionId",
        "permission_id",
        "questionID",
        "questionId",
        "question_id",
        "blockerID",
        "blockerId",
        "blocker_id",
    )
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


EVENT_ADAPTER = EventRouteAdapter()
OPENAPI_EVENT_ADAPTER = OpenApiEventRouteAdapter()
LEGACY_EVENT_ADAPTER = LegacyEventRouteAdapter()

def normalize_event_record(event, target_session_id=None, *, route_path=None):
    return event_adapter_for_route(route_path).normalize_record(event, target_session_id)

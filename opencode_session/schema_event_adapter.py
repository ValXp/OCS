import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional

from opencode_session.schema_common import NormalizedEventRecord, first_present, set_if_present, string_value
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
class DecodedEvent:
    event_type: Optional[str]
    session_id: Optional[str]
    message_id: Optional[str] = None
    status: Optional[str] = None
    delivery: Optional[str] = None
    text: Optional[str] = None
    tool: Optional[str] = None
    call_id: Optional[str] = None
    step: Optional[str] = None
    title: Optional[str] = None
    blocker: Optional[str] = None
    blocker_id: Optional[str] = None
    question: Optional[str] = None
    error: Optional[str] = None


class ApiEventRouteDecoder:
    route = "event"
    version = "api-v1"

    def decode(self, event):
        if not isinstance(event, dict) or not isinstance(event.get("properties"), dict):
            return None
        event_type = string_value(event.get("type"))
        if event_type is None:
            return None
        return _decoded_from_fields(event_type, event["properties"])

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        return _normalize_decoded_event(event, self.decode(event), target_session_id)


class LegacyEventRouteDecoder:
    route = "event"
    version = "legacy"

    def decode(self, event):
        if not isinstance(event, dict):
            return None
        event_type = string_value(event.get("event"))
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = event.get("data")
        if event_type is None or not isinstance(payload, dict):
            return None
        return _decoded_from_fields(event_type, payload)

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        return _normalize_decoded_event(event, self.decode(event), target_session_id)


class KnownEventRouteDecoder:
    route = "event"
    version = "known-shapes"

    def __init__(self, decoders=None):
        self.decoders = tuple(decoders or (API_EVENT_DECODER, LEGACY_EVENT_DECODER))

    def decode(self, event):
        for decoder in self.decoders:
            decoded = decoder.decode(event)
            if decoded is not None:
                return decoded
        return None

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        return _normalize_decoded_event(event, self.decode(event), target_session_id)


def _decoded_from_fields(event_type, fields):
    tool = _tool_name(fields)
    error = _error_text(fields.get("error")) or string_value(first_present(fields, "reason"))
    blocker = _blocker_type(event_type, fields)
    return DecodedEvent(
        event_type=event_type,
        session_id=_string_field(fields, "sessionID", "sessionId", "session_id"),
        message_id=_string_field(fields, "messageID", "messageId", "message_id", "promptID", "promptId"),
        status=_status_value(fields),
        delivery=_string_field(fields, "delivery", "deliveryMode", "mode"),
        text=_text_value(fields),
        tool=tool,
        call_id=_string_field(fields, "callID", "callId", "toolCallID", "toolCallId", "tool_call_id"),
        step=_string_field(fields, "step", "stepID", "stepId", "step_id"),
        title=_string_field(fields, "title", "description"),
        blocker=blocker,
        blocker_id=_blocker_id(fields) if blocker is not None else None,
        question=_string_field(fields, "question", "prompt", "title") if blocker is not None else None,
        error=error,
    )


def _normalize_decoded_event(raw_event, decoded, target_session_id):
    if decoded is None:
        return unknown_event_record(raw_event)
    if target_session_id is not None and decoded.session_id is not None and decoded.session_id != target_session_id:
        return ignored_event_record(decoded.session_id, target_session_id, decoded.event_type)

    kind = _event_kind(decoded)
    if kind == "unknown":
        return unknown_event_record(raw_event, event_type=decoded.event_type, session_id=decoded.session_id)

    normalized = {"kind": kind, "schema_status": "known"}
    set_if_present(normalized, "session_id", decoded.session_id)
    set_if_present(normalized, "type", decoded.event_type)
    set_if_present(normalized, "message_id", decoded.message_id)
    set_if_present(normalized, "delivery", decoded.delivery)
    set_if_present(normalized, "text", decoded.text)
    set_if_present(normalized, "tool", decoded.tool)
    set_if_present(normalized, "call_id", decoded.call_id)
    set_if_present(normalized, "step", decoded.step)
    set_if_present(normalized, "title", decoded.title)
    set_if_present(normalized, "blocker", decoded.blocker)
    set_if_present(normalized, "blocker_id", decoded.blocker_id)
    set_if_present(normalized, "question", decoded.question)
    set_if_present(normalized, "error", decoded.error)
    if decoded.status is not None:
        normalized["status"] = short_status(decoded.status)
        if normalized["status"] != decoded.status:
            normalized["raw_status"] = decoded.status
    return normalized


def _event_kind(decoded):
    normalized_type = str(decoded.event_type or "").lower()
    if decoded.blocker is not None:
        return "blocker"
    if normalized_type in ERROR_EVENT_TYPES or decoded.error is not None:
        return "error"
    if normalized_type in TEXT_EVENT_TYPES and decoded.text is not None:
        return "text"
    if normalized_type in TOOL_EVENT_TYPES and (decoded.tool is not None or decoded.call_id is not None):
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


def _string_field(fields, *names):
    return string_value(first_present(fields, *names))


def _text_value(fields):
    value = first_present(fields, "delta", "text", "content")
    if isinstance(value, str):
        return value
    part = fields.get("part")
    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
        return part["text"]
    message = fields.get("message")
    if isinstance(message, dict):
        value = first_present(message, "text", "content")
        if isinstance(value, str):
            return value
    return None


def _status_value(fields):
    value = _string_field(fields, "status", "state")
    if value is not None:
        return value
    message = fields.get("message")
    if isinstance(message, dict):
        return _string_field(message, "status", "state")
    return None


def _tool_name(fields):
    tool = fields.get("tool")
    if isinstance(tool, dict):
        value = first_present(tool, "name", "tool", "toolName", "tool_name")
        if value is not None:
            return str(value)
        return json.dumps(tool, sort_keys=True)
    value = first_present(fields, "tool", "toolName", "tool_name")
    return string_value(value)


def _blocker_type(event_type, fields):
    normalized_type = str(event_type or "").lower()
    if normalized_type == "permission.requested":
        return "permission"
    if normalized_type == "question.requested":
        return "question"
    if normalized_type in BLOCKER_EVENT_TYPES:
        return "blocker"
    if first_present(fields, "permission", "permissionID", "permissionId", "permission_id") is not None:
        return "permission"
    if first_present(fields, "questionID", "questionId", "question_id") is not None:
        return "question"
    if first_present(fields, "blocker", "blockerID", "blockerId", "blocker_id") is not None:
        return "blocker"
    return None


def _blocker_id(fields):
    return _string_field(
        fields,
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
        return API_EVENT_DECODER
    if normalized_path in {"/event", "/global/event"}:
        return LEGACY_EVENT_DECODER
    return KNOWN_EVENT_DECODER


API_EVENT_DECODER = ApiEventRouteDecoder()
LEGACY_EVENT_DECODER = LegacyEventRouteDecoder()
KNOWN_EVENT_DECODER = KnownEventRouteDecoder((API_EVENT_DECODER, LEGACY_EVENT_DECODER))
EVENT_ADAPTER = KNOWN_EVENT_DECODER
OPENAPI_EVENT_ADAPTER = API_EVENT_DECODER
LEGACY_EVENT_ADAPTER = LEGACY_EVENT_DECODER


def normalize_event_record(event, target_session_id=None, *, route_path=None):
    return event_adapter_for_route(route_path).normalize_record(event, target_session_id)

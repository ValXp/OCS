import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional, Tuple

from opencode_session.schema_event import NormalizedEventRecord
from opencode_session.schema_helpers import (
    first_present,
    set_if_present,
    string_value,
)
from opencode_session.status import short_status


API_EVENT_ROUTE = "/api/event"
LEGACY_EVENT_ROUTES = frozenset({"/event", "/global/event"})
SUCCESS_STATUSES = {"complete", "completed", "done", "idle", "success", "succeeded"}
ABORT_STATUSES = {"abort", "aborted", "cancelled", "canceled"}

EVENT_SESSION_ID_FIELDS = ("sessionID", "sessionId", "session_id")
EVENT_MESSAGE_ID_FIELDS = ("messageID", "messageId", "message_id", "promptID", "promptId")
EVENT_STATUS_FIELDS = ("status", "state")
EVENT_DELIVERY_FIELDS = ("delivery", "deliveryMode", "mode")
EVENT_CALL_ID_FIELDS = ("callID", "callId", "toolCallID", "toolCallId", "tool_call_id")
EVENT_STEP_FIELDS = ("stepID", "stepId", "step_id")
EVENT_TOOL_NAME_FIELDS = ("toolName", "tool_name")
EVENT_PERMISSION_ID_FIELDS = ("permissionID", "permissionId", "permission_id")
EVENT_QUESTION_ID_FIELDS = ("questionID", "questionId", "question_id")
EVENT_BLOCKER_ID_FIELDS = ("blockerID", "blockerId", "blocker_id")

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


@dataclass(frozen=True)
class EventKindContract:
    kind: str
    detail_fields: Tuple[str, ...]
    requires_blocker: bool = False


class ApiEventRouteDecoder:
    route = "event"
    version = "api-v1"

    def decode(self, event):
        if not isinstance(event, dict) or not isinstance(event.get("properties"), dict):
            return None
        event_type = string_value(event.get("type"))
        if event_type is None:
            return None
        return _decoded_from_api_properties(event_type, event["properties"])

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
        return _decoded_from_legacy_payload(event_type, payload)

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


class UnknownEventRouteDecoder:
    route = "event"
    version = "unknown"

    def decode(self, event):
        return None

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        return unknown_event_record(event)


def _decoded_from_api_properties(event_type, fields):
    return _decoded_from_event_fields(event_type, fields)


def _decoded_from_legacy_payload(event_type, fields):
    return _decoded_from_event_fields(event_type, fields)


def _decoded_from_event_fields(event_type, fields):
    tool = _tool_name(fields)
    error = _error_text(fields.get("error")) or string_value(first_present(fields, "reason"))
    blocker = _blocker_type(event_type, fields)
    return DecodedEvent(
        event_type=event_type,
        session_id=_string_field(fields, *EVENT_SESSION_ID_FIELDS),
        message_id=_string_field(fields, *EVENT_MESSAGE_ID_FIELDS),
        status=_status_value(fields),
        delivery=_string_field(fields, *EVENT_DELIVERY_FIELDS),
        text=_text_value(fields),
        tool=tool,
        call_id=_string_field(fields, *EVENT_CALL_ID_FIELDS),
        step=_string_field(fields, "step", *EVENT_STEP_FIELDS),
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
    contract = EVENT_KIND_CONTRACTS.get(normalized_type)
    if contract is None:
        return "unknown"
    if contract.requires_blocker and decoded.blocker is None:
        return "unknown"
    if _has_event_detail(decoded, *contract.detail_fields):
        return contract.kind
    return "unknown"


def _has_event_detail(decoded, *names):
    return any(getattr(decoded, name) is not None for name in names)


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
    value = _string_field(fields, *EVENT_STATUS_FIELDS)
    if value is not None:
        return value
    message = fields.get("message")
    if isinstance(message, dict):
        return _string_field(message, *EVENT_STATUS_FIELDS)
    return None


def _tool_name(fields):
    tool = fields.get("tool")
    if isinstance(tool, dict):
        value = first_present(tool, "name", "tool", *EVENT_TOOL_NAME_FIELDS)
        if value is not None:
            return str(value)
        return json.dumps(tool, sort_keys=True)
    value = first_present(fields, "tool", *EVENT_TOOL_NAME_FIELDS)
    return string_value(value)


def _blocker_type(event_type, fields):
    normalized_type = str(event_type or "").lower()
    if normalized_type == "permission.requested":
        return "permission"
    if normalized_type == "question.requested":
        return "question"
    if normalized_type in BLOCKER_EVENT_TYPES:
        return "blocker"
    if first_present(fields, "permission", *EVENT_PERMISSION_ID_FIELDS) is not None:
        return "permission"
    if first_present(fields, *EVENT_QUESTION_ID_FIELDS) is not None:
        return "question"
    if first_present(fields, "blocker", *EVENT_BLOCKER_ID_FIELDS) is not None:
        return "blocker"
    return None


def _blocker_id(fields):
    return _string_field(
        fields,
        *EVENT_PERMISSION_ID_FIELDS,
        *EVENT_QUESTION_ID_FIELDS,
        *EVENT_BLOCKER_ID_FIELDS,
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
    if route_path is None:
        return KNOWN_EVENT_DECODER
    normalized_path = str(route_path).split("?", 1)[0].rstrip("/")
    return EVENT_ROUTE_DECODERS.get(normalized_path, UNKNOWN_EVENT_DECODER)


def _event_kind_contracts():
    contracts = {}
    for event_type in BLOCKER_EVENT_TYPES:
        contracts[event_type] = EventKindContract(
            "blocker",
            ("session_id", "message_id", "blocker_id", "question", "status"),
            requires_blocker=True,
        )
    for event_type in ERROR_EVENT_TYPES:
        contracts[event_type] = EventKindContract("error", ("session_id", "message_id", "error"))
    for event_type in TEXT_EVENT_TYPES:
        contracts[event_type] = EventKindContract("text", ("text",))
    for event_type in TOOL_EVENT_TYPES:
        contracts[event_type] = EventKindContract("tool", ("tool", "call_id"))
    for event_type in ADMISSION_EVENT_TYPES:
        contracts[event_type] = EventKindContract("admission", ("session_id", "message_id", "status", "delivery"))
    for event_type in PROMPT_EVENT_TYPES:
        contracts[event_type] = EventKindContract("prompt", ("session_id", "message_id", "status", "delivery"))
    for event_type in STEP_EVENT_TYPES:
        contracts[event_type] = EventKindContract("step", ("session_id", "message_id", "step", "status", "title"))
    for event_type in STATUS_EVENT_TYPES:
        contracts[event_type] = EventKindContract("status", ("session_id", "status"))
    return contracts


API_EVENT_DECODER = ApiEventRouteDecoder()
LEGACY_EVENT_DECODER = LegacyEventRouteDecoder()
KNOWN_EVENT_DECODER = KnownEventRouteDecoder((API_EVENT_DECODER, LEGACY_EVENT_DECODER))
UNKNOWN_EVENT_DECODER = UnknownEventRouteDecoder()
EVENT_ROUTE_DECODERS = {API_EVENT_ROUTE: API_EVENT_DECODER}
EVENT_ROUTE_DECODERS.update({route: LEGACY_EVENT_DECODER for route in LEGACY_EVENT_ROUTES})
EVENT_KIND_CONTRACTS = _event_kind_contracts()
EVENT_ADAPTER = KNOWN_EVENT_DECODER
OPENAPI_EVENT_ADAPTER = API_EVENT_DECODER
LEGACY_EVENT_ADAPTER = LEGACY_EVENT_DECODER


def normalize_event_record(event, target_session_id=None, *, route_path=None):
    return event_adapter_for_route(route_path).normalize_record(event, target_session_id)

import json
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


@dataclass(frozen=True)
class EventRouteAdapter:
    route: str = "event"
    version: str = "opencode-compatible"

    def normalize_record(self, event, target_session_id=None) -> NormalizedEventRecord:
        if not isinstance(event, dict):
            event = {"data": event}
        properties = mapping_value(event, "properties") or mapping_value(event, "payload") or mapping_value(event, "data")
        info = mapping_value(event, "info") or mapping_value(properties, "info")
        part = mapping_value(event, "part") or mapping_value(properties, "part")
        message = mapping_value(event, "message") or mapping_value(properties, "message")
        tool = mapping_value(event, "tool") or mapping_value(properties, "tool")
        error = mapping_value(event, "error") or mapping_value(properties, "error")
        sources = [event, properties, info, part, message, tool]

        session_id = _event_session_id(sources)
        if target_session_id is not None and session_id != target_session_id:
            return None

        event_type = first_present_in(sources, "type", "event", "name", "kind")
        status = first_present_in(sources, "status", "state", "phase")
        raw_status = string_value(status)
        text = _event_text_value(sources)
        error_text = _error_text(error) or string_value(first_present_in(sources, "error", "reason"))
        tool_name = _tool_name(tool) or string_value(first_present_in([event, properties], "toolName", "tool_name", "tool"))
        call_id = string_value(first_present_in(sources, "callID", "callId", "toolCallID", "toolCallId", "tool_call_id"))
        kind = _event_kind(event_type, status, text, tool_name, call_id, error_text, sources)

        normalized = {
            "kind": kind,
            "session_id": session_id,
            "type": string_value(event_type),
        }
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


def _event_kind(event_type, status, text, tool_name, call_id, error_text, sources):
    lowered_type = str(event_type or "").lower()
    lowered_status = str(status or "").lower()
    if _blocker_type(event_type, sources):
        return "blocker"
    if error_text is not None or "error" in lowered_type or "failed" in lowered_type:
        return "error"
    if text is not None and ("text" in lowered_type or "part" in lowered_type or "message" in lowered_type):
        return "text"
    if tool_name is not None or call_id is not None or "tool" in lowered_type:
        return "tool"
    if "prompt" in lowered_type:
        if lowered_status in {"admitted", "promoted", "queued"} or first_present_in(sources, "delivery", "deliveryMode", "mode"):
            return "admission"
        return "prompt"
    if "step" in lowered_type:
        return "step"
    if "idle" in lowered_type or "status" in lowered_type or lowered_status in SUCCESS_STATUSES or lowered_status in ABORT_STATUSES:
        return "status"
    return "event"


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
    lowered_type = str(event_type or "").lower()
    if "permission" in lowered_type:
        return "permission"
    if "question" in lowered_type:
        return "question"
    if "blocker" in lowered_type:
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


EVENT_ADAPTER = EventRouteAdapter()

normalize_event_record = EVENT_ADAPTER.normalize_record

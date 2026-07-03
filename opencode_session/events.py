import json


SUCCESS_STATUSES = {"complete", "completed", "done", "idle", "success", "succeeded"}
ABORT_STATUSES = {"abort", "aborted", "cancelled", "canceled"}


class EventStreamError(Exception):
    pass


def iter_event_stream(lines):
    event_name = None
    event_id = None
    data_lines = []
    for raw_line in lines:
        line = _decode_line(raw_line).rstrip("\r\n")
        if line == "":
            event = _event_from_parts(event_name, event_id, data_lines)
            if event is not None:
                yield event
            event_name = None
            event_id = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("{") and not data_lines:
            event = _event_from_parts(None, None, [line])
            if event is not None:
                yield event
            continue
        field, separator, value = line.partition(":")
        if not separator:
            data_lines.append(line)
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)

    event = _event_from_parts(event_name, event_id, data_lines)
    if event is not None:
        yield event


def normalize_event(event, target_session_id=None):
    if not isinstance(event, dict):
        event = {"data": event}
    properties = _mapping_value(event, "properties") or _mapping_value(event, "payload") or _mapping_value(event, "data")
    info = _mapping_value(event, "info") or _mapping_value(properties, "info")
    part = _mapping_value(event, "part") or _mapping_value(properties, "part")
    message = _mapping_value(event, "message") or _mapping_value(properties, "message")
    tool = _mapping_value(event, "tool") or _mapping_value(properties, "tool")
    error = _mapping_value(event, "error") or _mapping_value(properties, "error")
    sources = [event, properties, info, part, message, tool]

    session_id = _session_id(sources)
    if target_session_id is not None and session_id != target_session_id:
        return None

    event_type = _first_present(sources, "type", "event", "name", "kind")
    status = _first_present(sources, "status", "state", "phase")
    text = _text_value(sources)
    error_text = _error_text(error) or _string_value(_first_present(sources, "error", "reason"))
    tool_name = _tool_name(tool) or _string_value(_first_present([event, properties], "toolName", "tool_name", "tool"))
    call_id = _string_value(_first_present(sources, "callID", "callId", "toolCallID", "toolCallId", "tool_call_id"))
    kind = _event_kind(event_type, status, text, tool_name, call_id, error_text, sources)

    normalized = {
        "kind": kind,
        "session_id": session_id,
        "type": _string_value(event_type),
    }
    _set_if_present(normalized, "message_id", _message_id(sources))
    _set_if_present(normalized, "status", _string_value(status))
    _set_if_present(normalized, "delivery", _string_value(_first_present(sources, "delivery", "deliveryMode", "mode")))
    _set_if_present(normalized, "text", text)
    _set_if_present(normalized, "tool", tool_name)
    _set_if_present(normalized, "call_id", call_id)
    _set_if_present(normalized, "step", _string_value(_first_present(sources, "step", "stepID", "stepId", "step_id")))
    _set_if_present(normalized, "title", _string_value(_first_present(sources, "title", "description")))
    blocker = _blocker_type(event_type, sources)
    _set_if_present(normalized, "blocker", blocker)
    if blocker is not None:
        _set_if_present(normalized, "blocker_id", _blocker_id(sources))
        _set_if_present(normalized, "question", _string_value(_first_present(sources, "question", "prompt", "title")))
    _set_if_present(normalized, "error", error_text)
    return normalized


def format_watch_event(event):
    kind = event["kind"]
    fields = [("session", event.get("session_id"))]
    if kind == "admission":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("delivery", event.get("delivery")),
                ("status", event.get("status")),
            ]
        )
    elif kind == "tool":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("call", event.get("call_id")),
                ("tool", event.get("tool")),
                ("status", event.get("status")),
            ]
        )
    elif kind == "status":
        fields.append(("status", event.get("status")))
    elif kind == "prompt":
        fields.extend([("message", event.get("message_id")), ("status", event.get("status"))])
    elif kind == "step":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("step", event.get("step")),
                ("status", event.get("status")),
                ("title", event.get("title")),
            ]
        )
    elif kind == "text":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("chars", len(event.get("text") or "")),
                ("text", event.get("text")),
            ]
        )
    elif kind == "error":
        fields.extend(
            [
                ("message", event.get("message_id")),
                ("status", event.get("status")),
                ("error", event.get("error")),
            ]
        )
    elif kind == "blocker":
        fields.extend(
            [
                ("blocker", event.get("blocker")),
                ("id", event.get("blocker_id")),
                ("message", event.get("message_id")),
                ("question", event.get("question")),
            ]
        )
    else:
        fields.extend([("message", event.get("message_id")), ("status", event.get("status"))])
    return " ".join([kind, *[f"{name}={_compact_value(value)}" for name, value in fields if value is not None]])


def is_terminal_event(event):
    status = str(event.get("status") or "").lower()
    return status in SUCCESS_STATUSES or status in ABORT_STATUSES


def is_abort_event(event):
    return str(event.get("status") or "").lower() in ABORT_STATUSES


def _event_from_parts(event_name, event_id, data_lines):
    if not data_lines and event_name is None and event_id is None:
        return None
    data_text = "\n".join(data_lines)
    data = _decode_data(data_text) if data_text else {}
    if isinstance(data, dict):
        event = dict(data)
    else:
        event = {"data": data}
    if event_name is not None:
        event.setdefault("event", event_name)
    if event_id is not None:
        event.setdefault("event_id", event_id)
    return event


def _decode_data(data_text):
    try:
        return json.loads(data_text)
    except json.JSONDecodeError as error:
        raise EventStreamError(f"invalid JSON: {error.msg}") from error


def _decode_line(raw_line):
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8", errors="replace")
    return str(raw_line)


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
        if lowered_status in {"admitted", "promoted", "queued"} or _first_present(sources, "delivery", "deliveryMode", "mode"):
            return "admission"
        return "prompt"
    if "step" in lowered_type:
        return "step"
    if "idle" in lowered_type or "status" in lowered_type or lowered_status in SUCCESS_STATUSES or lowered_status in ABORT_STATUSES:
        return "status"
    return "event"


def _session_id(sources):
    value = _first_present(sources, "sessionID", "sessionId", "session_id")
    if value is not None:
        return str(value)
    for source in sources:
        session = _mapping_value(source, "session")
        value = _first_present([session], "id", "sessionID", "sessionId", "session_id")
        if value is not None:
            return str(value)
    return None


def _message_id(sources):
    value = _first_present(sources, "messageID", "messageId", "message_id", "promptID", "promptId", "id")
    if value is not None:
        return str(value)
    return None


def _text_value(sources):
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type") == "text" and source.get("text") is not None:
            return str(source["text"])
        value = _first_present([source], "delta", "text", "content")
        if isinstance(value, str):
            return value
    return None


def _tool_name(tool):
    if isinstance(tool, dict):
        value = _first_present([tool], "name", "tool", "toolName", "tool_name")
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
    if _first_present(sources, "permission", "permissionID", "permissionId") is not None:
        return "permission"
    if _first_present(sources, "question", "questionID", "questionId") is not None:
        return "question"
    if _first_present(sources, "blocker", "blockerID", "blockerId") is not None:
        return "blocker"
    return None


def _blocker_id(sources):
    value = _first_present(
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
        value = _first_present([error], "message", "detail", "error")
        if value is not None:
            return str(value)
        return json.dumps(error, sort_keys=True)
    if error is not None:
        return str(error)
    return None


def _mapping_value(mapping, name):
    if isinstance(mapping, dict) and isinstance(mapping.get(name), dict):
        return mapping[name]
    return None


def _first_present(sources, *names):
    for source in sources:
        if not isinstance(source, dict):
            continue
        for name in names:
            value = source.get(name)
            if value is not None:
                return value
    return None


def _set_if_present(mapping, key, value):
    if value is not None:
        mapping[key] = value


def _string_value(value):
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _compact_value(value):
    if value is None or value == "":
        return "-"
    text = str(value)
    if any(character.isspace() for character in text):
        return json.dumps(text)
    return text

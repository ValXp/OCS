import json

from opencode_session.status import short_status


SUCCESS_STATUSES = {"complete", "completed", "done", "idle", "success", "succeeded"}
ABORT_STATUSES = {"abort", "aborted", "cancelled", "canceled"}


def first_present(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def first_present_in(sources, *names):
    for source in sources:
        value = first_present(source, *names)
        if value is not None:
            return value
    return None


def normalize_session_payload(payload):
    if isinstance(payload, list):
        return [normalize_session_record(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    data = normalized.get("data")
    if isinstance(data, list):
        normalized["data"] = [normalize_session_record(item) for item in data]
        return normalized
    if isinstance(data, dict):
        normalized["data"] = normalize_session_record(data)
        return normalized

    for name in ("sessions", "children"):
        records = normalized.get(name)
        if isinstance(records, list):
            normalized[name] = [normalize_session_record(item) for item in records]
            return normalized

    return normalize_session_record(normalized)


def normalize_session_record(record):
    if not isinstance(record, dict):
        return record
    if isinstance(record.get("data"), dict):
        normalized = dict(record)
        normalized["data"] = normalize_session_record(record["data"])
        return normalized

    normalized = dict(record)
    _set_missing(normalized, "id", session_value(record, "id", "sessionID", "sessionId", "session_id"))
    _set_missing(normalized, "directory", session_value(record, "directory", "cwd"))
    _set_missing(normalized, "title", session_value(record, "title", "name"))
    _set_missing(normalized, "agent", session_value(record, "agent", "agentID", "agentId", "agent_id"))
    _set_missing(normalized, "model", session_value(record, "model", "modelID", "modelId", "model_id"))
    _set_missing(
        normalized,
        "tokens",
        normalized_tokens(session_value(record, "tokens", "token", "tokenUsage", "token_usage", "usage")),
    )
    _set_missing(normalized, "createdAt", session_value(record, "createdAt", "created_at", "created"))
    _set_missing(normalized, "updatedAt", session_value(record, "updatedAt", "updated_at", "updated"))
    return normalized


def session_record(session):
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        return session["data"]
    return session if isinstance(session, dict) else {}


def session_value(session, *names):
    session = session_record(session)
    value = first_present(session, *names)
    if value is not None:
        return value
    info = session.get("info")
    value = first_present(info, *names)
    if value is not None:
        return value
    location = session.get("location")
    if isinstance(location, dict):
        for name in names:
            if name in {"directory", "cwd"} and location.get("directory") is not None:
                return location.get("directory")
    time = session.get("time")
    if isinstance(time, dict):
        for name in names:
            if name in {"createdAt", "created_at"} and time.get("created") is not None:
                return time.get("created")
            if name in {"updatedAt", "updated_at"} and time.get("updated") is not None:
                return time.get("updated")
    return None


def normalize_message_record(message):
    message = message_record(message)
    normalized = dict(message)
    _set_missing(normalized, "id", message_value(message, "id", "messageID", "messageId", "message_id"))
    _set_missing(normalized, "role", message_value(message, "role", "author", "speaker", "type", "kind"))
    raw_status = message_value(message, "status", "state", "phase")
    if raw_status is not None:
        normalized["status"] = short_status(raw_status)
        if normalized["status"] != raw_status:
            normalized["raw_status"] = raw_status
    _set_missing(normalized, "cost", message_value(message, "cost"))
    _set_missing(normalized, "tokens", message_tokens(message))
    _set_missing(normalized, "text", message_text(message))
    return normalized


def iter_normalized_message_records(data):
    for message in iter_message_records(data):
        yield normalize_message_record(message)


def iter_message_records(data):
    if not isinstance(data, dict):
        return
    for key in ("message", "assistant", "reply", "output"):
        value = data.get(key)
        if isinstance(value, dict):
            yield value
    for key in ("messages", "items", "entries"):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                yield item


def message_record(message):
    if isinstance(message, dict) and isinstance(message.get("data"), dict):
        return message["data"]
    return message if isinstance(message, dict) else {}


def message_value(message, *names):
    message = message_record(message)
    value = first_present(message, *names)
    if value is not None:
        return value
    return first_present(message.get("info"), *names)


def message_tokens(message):
    return normalized_tokens(message_value(message, "tokens", "token", "tokenUsage", "token_usage", "usage"))


def message_text(message):
    message = message_record(message)
    text = message_value(message, "text", "content")
    if text is not None:
        return text
    parts = message.get("parts")
    if isinstance(parts, list):
        return "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def normalize_admission_record(session_id, delivery, message_id, data, *, capabilities):
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        data = {}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    state = first_present(data, "state", "status", "phase") or "admitted"
    return {
        "session_id": first_present(data, "sessionID", "sessionId", "session_id")
        or first_present(info, "sessionID", "sessionId", "session_id")
        or session_id,
        "message_id": first_present(data, "messageID", "messageId", "promptID", "promptId", "id")
        or first_present(info, "messageID", "messageId", "promptID", "promptId", "id")
        or message_id,
        "delivery": first_present(data, "delivery", "deliveryMode", "mode") or delivery,
        "state": state,
        "raw_state": state,
        "status": short_status(state),
        "terminal_state": None,
        "api_path": capabilities["route_availability"]["v2_prompt"]["path"],
        "fallback": {
            "available": capabilities["legacy_fallback_available"],
            "strategy": "legacy_run_reply",
            "used": False,
        },
        "admitted_sequence": first_present(data, "admittedSeq", "admittedSequence", "admitted_sequence", "sequence"),
        "promoted_sequence": first_present(data, "promotedSeq", "promotedSequence", "promoted_sequence"),
    }


def normalize_event_record(event, target_session_id=None):
    if not isinstance(event, dict):
        event = {"data": event}
    properties = _mapping_value(event, "properties") or _mapping_value(event, "payload") or _mapping_value(event, "data")
    info = _mapping_value(event, "info") or _mapping_value(properties, "info")
    part = _mapping_value(event, "part") or _mapping_value(properties, "part")
    message = _mapping_value(event, "message") or _mapping_value(properties, "message")
    tool = _mapping_value(event, "tool") or _mapping_value(properties, "tool")
    error = _mapping_value(event, "error") or _mapping_value(properties, "error")
    sources = [event, properties, info, part, message, tool]

    session_id = _event_session_id(sources)
    if target_session_id is not None and session_id != target_session_id:
        return None

    event_type = first_present_in(sources, "type", "event", "name", "kind")
    status = first_present_in(sources, "status", "state", "phase")
    raw_status = _string_value(status)
    text = _event_text_value(sources)
    error_text = _error_text(error) or _string_value(first_present_in(sources, "error", "reason"))
    tool_name = _tool_name(tool) or _string_value(first_present_in([event, properties], "toolName", "tool_name", "tool"))
    call_id = _string_value(first_present_in(sources, "callID", "callId", "toolCallID", "toolCallId", "tool_call_id"))
    kind = _event_kind(event_type, status, text, tool_name, call_id, error_text, sources)

    normalized = {
        "kind": kind,
        "session_id": session_id,
        "type": _string_value(event_type),
    }
    _set_if_present(normalized, "message_id", _event_message_id(sources))
    _set_if_present(normalized, "status", short_status(raw_status))
    if raw_status is not None and short_status(raw_status) != raw_status:
        normalized["raw_status"] = raw_status
    _set_if_present(normalized, "delivery", _string_value(first_present_in(sources, "delivery", "deliveryMode", "mode")))
    _set_if_present(normalized, "text", text)
    _set_if_present(normalized, "tool", tool_name)
    _set_if_present(normalized, "call_id", call_id)
    _set_if_present(normalized, "step", _string_value(first_present_in(sources, "step", "stepID", "stepId", "step_id")))
    _set_if_present(normalized, "title", _string_value(first_present_in(sources, "title", "description")))
    blocker = _blocker_type(event_type, sources)
    _set_if_present(normalized, "blocker", blocker)
    if blocker is not None:
        _set_if_present(normalized, "blocker_id", _blocker_id(sources))
        _set_if_present(normalized, "question", _string_value(first_present_in(sources, "question", "prompt", "title")))
    _set_if_present(normalized, "error", error_text)
    return normalized


def normalized_tokens(tokens):
    if isinstance(tokens, dict):
        normalized = dict(tokens)
        if normalized.get("total") is None:
            values = [value for value in normalized.values() if isinstance(value, int)]
            if values:
                normalized["total"] = sum(values)
        return normalized
    return tokens


def tokens_total(tokens):
    tokens = normalized_tokens(tokens)
    if isinstance(tokens, dict):
        return tokens.get("total")
    return tokens


def bool_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "yes", "1", "accepted", "aborted", "ok", "success"}:
            return True
        if lowered in {"false", "no", "0", "rejected", "failed", "error"}:
            return False
    return None


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
        session = _mapping_value(source, "session")
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


def _mapping_value(mapping, name):
    if isinstance(mapping, dict) and isinstance(mapping.get(name), dict):
        return mapping[name]
    return None


def _set_missing(record, name, value):
    if value is not None and record.get(name) is None:
        record[name] = value


def _set_if_present(mapping, key, value):
    if value is not None:
        mapping[key] = value


def _string_value(value):
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)

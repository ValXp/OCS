from copy import deepcopy

from opencode_session.schema_common import NormalizedMessageRecord, first_present, normalized_tokens, set_missing
from opencode_session.status import short_status


MESSAGE_CANONICAL_FIELDS = ("id", "role", "status", "raw_status", "cost", "tokens", "text")


def normalize_message_record(message) -> NormalizedMessageRecord:
    if not isinstance(message, dict):
        return unknown_message_record(message)
    message = message_record(message)

    fields = _compatible_message_fields(message)
    if not _has_any_message_field(fields):
        return unknown_message_record(message)

    normalized = dict(message)
    set_missing(normalized, "id", fields["id"])
    set_missing(normalized, "role", fields["role"])
    raw_status = fields["status"]
    if raw_status is not None:
        normalized["status"] = short_status(raw_status)
        if normalized["status"] != raw_status:
            normalized["raw_status"] = raw_status
    set_missing(normalized, "cost", fields["cost"])
    set_missing(normalized, "tokens", normalized_tokens(fields["tokens"]))
    set_missing(normalized, "text", message_text(message))
    require_message_canonical_fields(normalized)
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
    if _requested(names, "id", "messageID", "messageId", "message_id"):
        value = _compatible_message_id(message)
        if value is not None:
            return value
    if _requested(names, "role", "author", "speaker", "type", "kind"):
        value = _compatible_message_role(message)
        if value is not None:
            return value
    if _requested(names, "status", "state", "phase"):
        value = _compatible_message_status(message)
        if value is not None:
            return value
    if _requested(names, "cost"):
        value = _compatible_message_cost(message)
        if value is not None:
            return value
    if _requested(names, "tokens", "token", "tokenUsage", "token_usage", "usage"):
        value = _compatible_message_tokens(message)
        if value is not None:
            return value
    if _requested(names, "text", "content"):
        value = _compatible_message_text(message)
        if value is not None:
            return value
    if _requested(names, "error", "reason", "message"):
        value = _compatible_message_error(message)
        if value is not None:
            return value
    return None


def message_tokens(message):
    return normalized_tokens(_compatible_message_tokens(message_record(message)))


def message_text(message):
    message = message_record(message)
    text = _compatible_message_text(message)
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


def _compatible_message_fields(message):
    return {
        "id": _compatible_message_id(message),
        "role": _compatible_message_role(message),
        "status": _compatible_message_status(message),
        "cost": _compatible_message_cost(message),
        "tokens": _compatible_message_tokens(message),
        "text": _compatible_message_text(message),
        "error": _compatible_message_error(message),
    }


def _compatible_message_id(message):
    return _root_or_info_value(message, "id", "messageID", "messageId", "message_id")


def _compatible_message_role(message):
    return _root_or_info_value(message, "role", "author", "speaker", "type", "kind")


def _compatible_message_status(message):
    return _root_or_info_value(message, "status", "state", "phase")


def _compatible_message_cost(message):
    return _root_or_info_value(message, "cost")


def _compatible_message_tokens(message):
    return _root_or_info_value(message, "tokens", "token", "tokenUsage", "token_usage", "usage")


def _compatible_message_text(message):
    return _root_or_info_value(message, "text", "content")


def _compatible_message_error(message):
    return _root_or_info_value(message, "error", "reason", "message")


def _root_or_info_value(record, *names):
    value = first_present(record, *names)
    if value is not None:
        return value
    info = record.get("info") if isinstance(record, dict) else None
    return first_present(info, *names)


def _has_any_message_field(fields):
    return any(value is not None for value in fields.values())


def unknown_message_record(raw) -> NormalizedMessageRecord:
    normalized = {field_name: None for field_name in MESSAGE_CANONICAL_FIELDS}
    normalized["text"] = ""
    normalized["schema_status"] = "unknown"
    normalized["raw"] = deepcopy(raw)
    return normalized


def require_message_canonical_fields(record):
    for field_name in MESSAGE_CANONICAL_FIELDS:
        record.setdefault(field_name, None)
    record.setdefault("text", "")


def _requested(requested_names, *aliases):
    return any(name in aliases for name in requested_names)

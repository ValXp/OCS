from copy import deepcopy

from opencode_session.schema_common import (
    API_TOKEN_ALIASES,
    CAMEL_MESSAGE_ID_ALIASES,
    EVENT_STATUS_ALIASES,
    MESSAGE_ID_ALIASES,
    STATUS_ALIASES,
    TOKEN_ALIASES,
    NormalizedMessageRecord,
    normalized_tokens,
    root_or_info_value,
    set_missing,
)
from opencode_session.status import short_status


MESSAGE_CANONICAL_FIELDS = ("id", "role", "status", "raw_status", "cost", "tokens", "text")
MESSAGE_VALUE_ALIASES = (
    ("id", ("id", *MESSAGE_ID_ALIASES)),
    ("role", ("role", "author", "speaker", "type", "kind")),
    ("status", STATUS_ALIASES),
    ("cost", ("cost",)),
    ("tokens", TOKEN_ALIASES),
    ("text", ("text", "content")),
    ("error", ("error", "reason", "message")),
)
SESSION_MESSAGE_ROUTE = "session_message"
LEGACY_MESSAGE_ROUTE = "legacy_run_reply"


def normalize_message_record(message, *, route=None) -> NormalizedMessageRecord:
    return _normalize_message_record(message, _message_fields_for_route(route))


def _normalize_message_record(message, read_fields) -> NormalizedMessageRecord:
    if not isinstance(message, dict):
        return unknown_message_record(message)
    message = message_record(message)

    fields = read_fields(message)
    if not _has_known_message_shape(fields):
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
    set_missing(normalized, "error", fields["error"])
    require_message_canonical_fields(normalized)
    return normalized


def iter_normalized_message_records(data, *, route=None):
    for message in iter_message_records(data):
        yield normalize_message_record(message, route=route)


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


def message_value(message, *names, route=None):
    normalized = normalize_message_record(message, route=route)
    if normalized.get("schema_status") == "unknown":
        return None
    for field_name, aliases in MESSAGE_VALUE_ALIASES:
        if not _requested(names, *aliases):
            continue
        return normalized.get(field_name)
    return None


def message_tokens(message, *, route=None):
    message = message_record(message)
    fields = _message_fields_for_route(route)(message)
    if route is not None and not _has_known_message_shape(fields):
        return None
    return normalized_tokens(fields["tokens"])


def message_text(message, *, route=None):
    message = message_record(message)
    fields = _message_fields_for_route(route)(message)
    if route is not None and not _has_known_message_shape(fields):
        return ""
    text = fields["text"]
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


def _message_fields_for_route(route=None):
    if route == SESSION_MESSAGE_ROUTE:
        return _session_message_fields
    if route == LEGACY_MESSAGE_ROUTE:
        return _legacy_message_fields
    return _legacy_message_fields


def _session_message_fields(message):
    return {
        "id": root_or_info_value(message, "id", *CAMEL_MESSAGE_ID_ALIASES),
        "role": root_or_info_value(message, "role"),
        "status": root_or_info_value(message, *EVENT_STATUS_ALIASES),
        "cost": root_or_info_value(message, "cost"),
        "tokens": root_or_info_value(message, *API_TOKEN_ALIASES),
        "text": root_or_info_value(message, "text", "content"),
        "error": root_or_info_value(message, "error", "reason", "message"),
    }


def _legacy_message_fields(message):
    return {
        "id": root_or_info_value(message, "id", *MESSAGE_ID_ALIASES),
        "role": root_or_info_value(message, "role", "author", "speaker", "type", "kind"),
        "status": root_or_info_value(message, *STATUS_ALIASES),
        "cost": root_or_info_value(message, "cost"),
        "tokens": root_or_info_value(message, *TOKEN_ALIASES),
        "text": root_or_info_value(message, "text", "content"),
        "error": root_or_info_value(message, "error", "reason", "message"),
    }


def _has_known_message_shape(fields):
    return any(fields[name] is not None for name in ("id", "role", "status", "text", "error"))


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

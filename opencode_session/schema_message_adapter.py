from copy import deepcopy
from dataclasses import dataclass
from typing import Callable

from opencode_session.schema_helpers import (
    normalized_tokens,
    root_or_info_value,
    set_missing,
)
from opencode_session.schema_message import NormalizedMessageRecord
from opencode_session.status import short_status


MESSAGE_CANONICAL_FIELDS = ("id", "role", "status", "raw_status", "cost", "tokens", "text")
LEGACY_MESSAGE_ID_FIELDS = ("id", "messageID", "messageId", "message_id")
LEGACY_MESSAGE_ROLE_FIELDS = ("role", "author", "speaker", "type", "kind")
LEGACY_MESSAGE_STATUS_FIELDS = ("status", "state", "phase")
LEGACY_MESSAGE_TOKEN_FIELDS = ("tokens", "token", "tokenUsage", "token_usage", "usage")
SESSION_MESSAGE_ID_FIELDS = ("id", "messageID", "messageId")
SESSION_MESSAGE_STATUS_FIELDS = ("status", "state")
SESSION_MESSAGE_TOKEN_FIELDS = ("tokens", "tokenUsage", "usage")
MESSAGE_TEXT_FIELDS = ("text", "content")
MESSAGE_ERROR_FIELDS = ("error", "reason", "message")
MESSAGE_VALUE_ALIASES = (
    ("id", LEGACY_MESSAGE_ID_FIELDS),
    ("role", LEGACY_MESSAGE_ROLE_FIELDS),
    ("status", LEGACY_MESSAGE_STATUS_FIELDS),
    ("cost", ("cost",)),
    ("tokens", LEGACY_MESSAGE_TOKEN_FIELDS),
    ("text", MESSAGE_TEXT_FIELDS),
    ("error", MESSAGE_ERROR_FIELDS),
)
SESSION_MESSAGE_ROUTE = "session_message"
LEGACY_MESSAGE_ROUTE = "legacy_run_reply"


@dataclass(frozen=True)
class MessageRouteAdapter:
    route: str
    version: str
    read_fields: Callable


def normalize_message_record(message, *, route=None) -> NormalizedMessageRecord:
    return _normalize_message_record(message, message_adapter_for_route(route))


def _normalize_message_record(message, adapter) -> NormalizedMessageRecord:
    if not isinstance(message, dict):
        return unknown_message_record(message)
    message = message_record(message)

    fields = adapter.read_fields(message)
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
    set_missing(normalized, "text", _message_text_from_fields(message, fields))
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
    fields = message_adapter_for_route(route).read_fields(message)
    if route is not None and not _has_known_message_shape(fields):
        return None
    return normalized_tokens(fields["tokens"])


def message_text(message, *, route=None):
    message = message_record(message)
    fields = message_adapter_for_route(route).read_fields(message)
    if route is not None and not _has_known_message_shape(fields):
        return ""
    return _message_text_from_fields(message, fields)


def _message_text_from_fields(message, fields):
    if fields["text"] is not None:
        return fields["text"]
    parts = message.get("parts")
    if isinstance(parts, list):
        return "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def message_adapter_for_route(route=None):
    if route is None:
        return DEFAULT_MESSAGE_ADAPTER
    return MESSAGE_ROUTE_ADAPTERS.get(route, UNKNOWN_MESSAGE_ADAPTER)


def _session_message_fields(message):
    return {
        "id": root_or_info_value(message, *SESSION_MESSAGE_ID_FIELDS),
        "role": root_or_info_value(message, "role"),
        "status": root_or_info_value(message, *SESSION_MESSAGE_STATUS_FIELDS),
        "cost": root_or_info_value(message, "cost"),
        "tokens": root_or_info_value(message, *SESSION_MESSAGE_TOKEN_FIELDS),
        "text": root_or_info_value(message, *MESSAGE_TEXT_FIELDS),
        "error": root_or_info_value(message, *MESSAGE_ERROR_FIELDS),
    }


def _legacy_message_fields(message):
    return {
        "id": root_or_info_value(message, *LEGACY_MESSAGE_ID_FIELDS),
        "role": root_or_info_value(message, *LEGACY_MESSAGE_ROLE_FIELDS),
        "status": root_or_info_value(message, *LEGACY_MESSAGE_STATUS_FIELDS),
        "cost": root_or_info_value(message, "cost"),
        "tokens": root_or_info_value(message, *LEGACY_MESSAGE_TOKEN_FIELDS),
        "text": root_or_info_value(message, *MESSAGE_TEXT_FIELDS),
        "error": root_or_info_value(message, *MESSAGE_ERROR_FIELDS),
    }


def _unknown_message_fields(message):
    return {field_name: None for field_name in ("id", "role", "status", "cost", "tokens", "text", "error")}


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


SESSION_MESSAGE_ADAPTER = MessageRouteAdapter(
    route=SESSION_MESSAGE_ROUTE,
    version="session-message",
    read_fields=_session_message_fields,
)
LEGACY_MESSAGE_ADAPTER = MessageRouteAdapter(
    route=LEGACY_MESSAGE_ROUTE,
    version="legacy-run-reply",
    read_fields=_legacy_message_fields,
)
UNKNOWN_MESSAGE_ADAPTER = MessageRouteAdapter(
    route="unknown",
    version="unknown",
    read_fields=_unknown_message_fields,
)
MESSAGE_ROUTE_ADAPTERS = {
    SESSION_MESSAGE_ROUTE: SESSION_MESSAGE_ADAPTER,
    LEGACY_MESSAGE_ROUTE: LEGACY_MESSAGE_ADAPTER,
}
DEFAULT_MESSAGE_ADAPTER = LEGACY_MESSAGE_ADAPTER

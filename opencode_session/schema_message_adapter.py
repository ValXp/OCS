from copy import deepcopy
from dataclasses import dataclass

from opencode_session.schema_helpers import (
    CAMEL_MESSAGE_ID_ALIASES,
    MESSAGE_ID_ALIASES,
    normalized_tokens,
    set_missing,
)
from opencode_session.schema_message import NormalizedMessageRecord
from opencode_session.schema_route_contract import RouteAdapterContract, route_field
from opencode_session.status import short_status


MESSAGE_CANONICAL_FIELDS = ("id", "role", "status", "raw_status", "cost", "tokens", "text")
MESSAGE_FIELD_NAMES = ("id", "role", "status", "cost", "tokens", "text", "error")
MESSAGE_KNOWN_FIELDS = ("id", "role", "status", "text", "error")
MESSAGE_TEXT_FIELDS = ("text", "content")
MESSAGE_ERROR_FIELDS = ("error", "reason", "message")
SESSION_MESSAGE_ROUTE = "session_message"
LEGACY_MESSAGE_ROUTE = "legacy_run_reply"


SESSION_MESSAGE_CONTRACT = RouteAdapterContract(
    route=SESSION_MESSAGE_ROUTE,
    version="session-message",
    fields=(
        route_field("id", "id", *CAMEL_MESSAGE_ID_ALIASES),
        route_field("role", "role"),
        route_field("status", "status", "state"),
        route_field("cost", "cost"),
        route_field("tokens", "tokens", "tokenUsage", "usage"),
        route_field("text", *MESSAGE_TEXT_FIELDS),
        route_field("error", *MESSAGE_ERROR_FIELDS),
    ),
    known_fields=MESSAGE_KNOWN_FIELDS,
)
LEGACY_MESSAGE_CONTRACT = RouteAdapterContract(
    route=LEGACY_MESSAGE_ROUTE,
    version="legacy-run-reply",
    fields=(
        route_field("id", "id", *MESSAGE_ID_ALIASES),
        route_field("role", "role", "author", "speaker", "type", "kind"),
        route_field("status", "status", "state", "phase"),
        route_field("cost", "cost"),
        route_field("tokens", "tokens", "token", "tokenUsage", "token_usage", "usage"),
        route_field("text", *MESSAGE_TEXT_FIELDS),
        route_field("error", *MESSAGE_ERROR_FIELDS),
    ),
    known_fields=MESSAGE_KNOWN_FIELDS,
)
UNKNOWN_MESSAGE_CONTRACT = RouteAdapterContract(
    route="unknown",
    version="unknown",
    fields=tuple(route_field(name) for name in MESSAGE_FIELD_NAMES),
    known_fields=MESSAGE_KNOWN_FIELDS,
)
MESSAGE_VALUE_ALIASES = tuple((field.name, field.aliases) for field in LEGACY_MESSAGE_CONTRACT.fields)


@dataclass(frozen=True)
class MessageRouteAdapter:
    contract: RouteAdapterContract

    @property
    def route(self):
        return self.contract.route

    @property
    def version(self):
        return self.contract.version

    def read_fields(self, message):
        return self.contract.read_fields(message)

    def has_known_shape(self, fields):
        return self.contract.has_known_shape(fields)


def normalize_message_record(message, *, route=None) -> NormalizedMessageRecord:
    return _normalize_message_record(message, message_adapter_for_route(route))


def _normalize_message_record(message, adapter) -> NormalizedMessageRecord:
    if not isinstance(message, dict):
        return unknown_message_record(message)
    message = message_record(message)

    fields = adapter.read_fields(message)
    if not adapter.has_known_shape(fields):
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
    adapter = message_adapter_for_route(route)
    fields = adapter.read_fields(message)
    if route is not None and not adapter.has_known_shape(fields):
        return None
    return normalized_tokens(fields["tokens"])


def message_text(message, *, route=None):
    message = message_record(message)
    adapter = message_adapter_for_route(route)
    fields = adapter.read_fields(message)
    if route is not None and not adapter.has_known_shape(fields):
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
    contract=SESSION_MESSAGE_CONTRACT,
)
LEGACY_MESSAGE_ADAPTER = MessageRouteAdapter(
    contract=LEGACY_MESSAGE_CONTRACT,
)
UNKNOWN_MESSAGE_ADAPTER = MessageRouteAdapter(
    contract=UNKNOWN_MESSAGE_CONTRACT,
)
MESSAGE_ROUTE_ADAPTERS = {
    SESSION_MESSAGE_ROUTE: SESSION_MESSAGE_ADAPTER,
    LEGACY_MESSAGE_ROUTE: LEGACY_MESSAGE_ADAPTER,
}
DEFAULT_MESSAGE_ADAPTER = LEGACY_MESSAGE_ADAPTER

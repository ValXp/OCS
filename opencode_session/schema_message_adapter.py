import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional

from opencode_session.schema_helpers import (
    normalized_tokens,
    set_missing,
)
from opencode_session.schema_message import NormalizedMessageRecord
from opencode_session.schema_message_codecs import (
    DEFAULT_MESSAGE_CODEC,
    FINAL_MESSAGE_MINIMUM_FIELD_SETS,
    LEGACY_MESSAGE_CODEC,
    LEGACY_MESSAGE_CONTRACT,
    LEGACY_MESSAGE_ROUTE,
    LEGACY_REPLY_PATH,
    LEGACY_RUN_PATH,
    MESSAGE_CANONICAL_FIELDS,
    MESSAGE_ENDPOINT_CODECS,
    MESSAGE_ERROR_FIELDS,
    MESSAGE_ID_MINIMUM_FIELD_SETS,
    MESSAGE_KNOWN_FIELDS,
    MESSAGE_ROUTE_PATH_CODECS,
    MESSAGE_TEXT_FIELDS,
    MESSAGE_VALUE_ALIASES,
    SESSION_MESSAGE_CODEC,
    SESSION_MESSAGE_CONTRACT,
    SESSION_MESSAGE_PATH,
    SESSION_MESSAGE_ROUTE,
    UNKNOWN_MESSAGE_CODEC,
    UNKNOWN_MESSAGE_CONTRACT,
    message_codec_for_endpoint,
    message_codec_for_route,
)
from opencode_session.status import short_status

MESSAGE_REQUIRE_ID = "message_id"
MESSAGE_REQUIRE_FINAL_ASSISTANT = "final_assistant"
TERMINAL_MESSAGE_STATUSES = {"done", "failed", "aborted", "timeout"}


@dataclass(frozen=True)
class MessageAdapterResult:
    record: NormalizedMessageRecord
    fields: dict
    known: bool
    provider_failure: Optional[str] = None
    incomplete_reason: Optional[str] = None

    @property
    def schema_status(self):
        return "known" if self.known else "unknown"


@dataclass(frozen=True)
class MessageRouteAdapter:
    codec: object

    @property
    def contract(self):
        return self.codec.contract

    @property
    def route(self):
        return self.codec.route

    @property
    def version(self):
        return self.codec.version

    def read_fields(self, message):
        return self.codec.read_fields(message)

    def has_known_shape(self, fields):
        return self.codec.has_known_shape(fields)

    def has_minimum_shape(self, fields):
        return self.codec.has_minimum_shape(fields)

    def normalize_record(self, message) -> NormalizedMessageRecord:
        return self.normalize_result(message).record

    def normalize_result(self, message, *, requirement=None, label="message") -> MessageAdapterResult:
        return _normalize_message_result(message, self, requirement=requirement, label=label)


def normalize_message_record(message, *, route=None) -> NormalizedMessageRecord:
    return normalize_message_result(message, route=route).record


def normalize_message_result(message, *, route=None, requirement=None, label="message") -> MessageAdapterResult:
    return message_adapter_for_route(route).normalize_result(message, requirement=requirement, label=label)


def _normalize_message_record(message, adapter) -> NormalizedMessageRecord:
    return _normalize_message_result(message, adapter).record


def _normalize_message_result(message, adapter, *, requirement=None, label="message") -> MessageAdapterResult:
    if not isinstance(message, dict):
        return MessageAdapterResult(unknown_message_record(message), {}, False)
    message = message_record(message)

    fields = adapter.read_fields(message)
    if not adapter.has_known_shape(fields):
        return MessageAdapterResult(unknown_message_record(message), fields, False)

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
    return MessageAdapterResult(
        normalized,
        fields,
        True,
        provider_failure=message_provider_failure(normalized),
        incomplete_reason=_incomplete_message_reason(normalized, requirement, label=label),
    )


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


def message_provider_failure(record):
    status = str(message_raw_status(record, default="") or "").lower()
    error = record.get("error")
    if status not in {"failed", "error", "errored"}:
        if not status and error:
            if isinstance(error, dict):
                return error.get("message") or json.dumps(error, sort_keys=True)
            return str(error)
        return None
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error, sort_keys=True)
    return error or status


def message_raw_status(message, *, default=None):
    return message.get("raw_status") or message.get("status") or default


def _incomplete_message_reason(record, requirement, *, label):
    if requirement is None:
        return None
    if requirement == MESSAGE_REQUIRE_ID:
        if record.get("id"):
            return None
        return f"missing {label} message id"
    if requirement == MESSAGE_REQUIRE_FINAL_ASSISTANT:
        if not record.get("id"):
            return f"missing {label} message id"
        if _has_message_text(record) or _has_explicit_terminal_status(record):
            return None
        return "missing assistant text or explicit terminal status"
    raise ValueError(f"unknown message requirement: {requirement}")


def _has_message_text(record):
    return record.get("text") not in (None, "")


def _has_explicit_terminal_status(record):
    raw_status = message_raw_status(record)
    return raw_status is not None and short_status(raw_status) in TERMINAL_MESSAGE_STATUSES


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
    return _adapter_for_codec(message_codec_for_route(route))


def message_adapter_for_endpoint(endpoint, route_path=None):
    return _adapter_for_codec(message_codec_for_endpoint(endpoint, route_path))


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


def _adapter_for_codec(codec):
    if codec is SESSION_MESSAGE_CODEC:
        return SESSION_MESSAGE_ADAPTER
    if codec is LEGACY_MESSAGE_CODEC:
        return LEGACY_MESSAGE_ADAPTER
    return UNKNOWN_MESSAGE_ADAPTER


SESSION_MESSAGE_ADAPTER = MessageRouteAdapter(
    codec=SESSION_MESSAGE_CODEC,
)
LEGACY_MESSAGE_ADAPTER = MessageRouteAdapter(
    codec=LEGACY_MESSAGE_CODEC,
)
UNKNOWN_MESSAGE_ADAPTER = MessageRouteAdapter(
    codec=UNKNOWN_MESSAGE_CODEC,
)
MESSAGE_ROUTE_ADAPTERS = {
    SESSION_MESSAGE_ROUTE: SESSION_MESSAGE_ADAPTER,
    LEGACY_MESSAGE_ROUTE: LEGACY_MESSAGE_ADAPTER,
}
MESSAGE_ROUTE_PATH_ADAPTERS = {
    path: _adapter_for_codec(codec)
    for path, codec in MESSAGE_ROUTE_PATH_CODECS.items()
}
MESSAGE_ENDPOINT_ADAPTERS = {
    endpoint: _adapter_for_codec(codec)
    for endpoint, codec in MESSAGE_ENDPOINT_CODECS.items()
}
DEFAULT_MESSAGE_ADAPTER = LEGACY_MESSAGE_ADAPTER

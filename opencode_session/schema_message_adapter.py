from copy import deepcopy
from dataclasses import dataclass

from opencode_session.schema_common import (
    FieldExtractor,
    FieldSource,
    NormalizedMessageRecord,
    normalized_tokens,
    set_missing,
)
from opencode_session.status import short_status


MESSAGE_CANONICAL_FIELDS = ("id", "role", "status", "raw_status", "cost", "tokens", "text")


@dataclass(frozen=True)
class MessageRouteSchema:
    extractor: FieldExtractor
    known_fields: tuple


@dataclass(frozen=True)
class MessageRouteAdapter:
    schema: MessageRouteSchema
    route: str = "message"
    version: str = "compatible"

    def normalize_record(self, message) -> NormalizedMessageRecord:
        if not isinstance(message, dict):
            return unknown_message_record(message)
        message = self.record(message)
        if not self.is_known_record(message):
            return unknown_message_record(message)
        normalized = dict(message)
        set_missing(normalized, "id", self.field_value(message, "id"))
        set_missing(normalized, "role", self.field_value(message, "role"))
        raw_status = self.field_value(message, "status")
        if raw_status is not None:
            normalized["status"] = short_status(raw_status)
            if normalized["status"] != raw_status:
                normalized["raw_status"] = raw_status
        set_missing(normalized, "cost", self.field_value(message, "cost"))
        set_missing(normalized, "tokens", self.tokens(message))
        set_missing(normalized, "text", self.text(message))
        require_message_canonical_fields(normalized)
        return normalized

    def iter_normalized_records(self, data):
        for message in self.iter_records(data):
            yield self.normalize_record(message)

    def iter_records(self, data):
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

    def record(self, message):
        if isinstance(message, dict) and isinstance(message.get("data"), dict):
            return message["data"]
        return message if isinstance(message, dict) else {}

    def value(self, message, *names):
        message = self.record(message)
        return self.schema.extractor.named_value(message, *names)

    def field_value(self, message, field_name):
        message = self.record(message)
        return self.schema.extractor.value(message, field_name)

    def is_known_record(self, message):
        return self.schema.extractor.has_any(self.record(message), self.schema.known_fields)

    def tokens(self, message):
        return normalized_tokens(self.field_value(message, "tokens"))

    def text(self, message):
        message = self.record(message)
        text = self.field_value(message, "text")
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


@dataclass(frozen=True)
class OpenApiMessageRouteAdapter(MessageRouteAdapter):
    schema: MessageRouteSchema
    version: str = "api-v1"


@dataclass(frozen=True)
class LegacyMessageRouteAdapter(MessageRouteAdapter):
    schema: MessageRouteSchema
    version: str = "legacy"


def _message_schema(field_aliases):
    fields = {}
    for field_name, aliases in field_aliases.items():
        fields[field_name] = (
            FieldSource((), aliases),
            FieldSource(("info",), aliases),
        )
    return MessageRouteSchema(FieldExtractor(fields), tuple(field_aliases))


COMPATIBLE_MESSAGE_FIELD_ALIASES = {
    "id": ("id", "messageID", "messageId", "message_id"),
    "role": ("role", "author", "speaker", "type", "kind"),
    "status": ("status", "state", "phase"),
    "cost": ("cost",),
    "tokens": ("tokens", "token", "tokenUsage", "token_usage", "usage"),
    "text": ("text", "content"),
    "error": ("error", "reason", "message"),
}

OPENAPI_MESSAGE_FIELD_ALIASES = {
    "id": ("id",),
    "role": ("role",),
    "status": ("status",),
    "cost": ("cost",),
    "tokens": ("tokens", "tokenUsage", "usage"),
    "text": ("text", "content"),
    "error": ("error", "reason", "message"),
}


COMPATIBLE_MESSAGE_SCHEMA = _message_schema(COMPATIBLE_MESSAGE_FIELD_ALIASES)
OPENAPI_MESSAGE_SCHEMA = _message_schema(OPENAPI_MESSAGE_FIELD_ALIASES)


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


MESSAGE_ADAPTER = MessageRouteAdapter(COMPATIBLE_MESSAGE_SCHEMA)
OPENAPI_MESSAGE_ADAPTER = OpenApiMessageRouteAdapter(OPENAPI_MESSAGE_SCHEMA)
LEGACY_MESSAGE_ADAPTER = LegacyMessageRouteAdapter(COMPATIBLE_MESSAGE_SCHEMA)

normalize_message_record = MESSAGE_ADAPTER.normalize_record
iter_normalized_message_records = MESSAGE_ADAPTER.iter_normalized_records
iter_message_records = MESSAGE_ADAPTER.iter_records
message_record = MESSAGE_ADAPTER.record
message_value = MESSAGE_ADAPTER.value
message_tokens = MESSAGE_ADAPTER.tokens
message_text = MESSAGE_ADAPTER.text

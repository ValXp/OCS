from copy import deepcopy
from dataclasses import dataclass

from opencode_session.schema_common import NormalizedMessageRecord, first_present, normalized_tokens, set_missing
from opencode_session.status import short_status


MESSAGE_CANONICAL_FIELDS = ("id", "role", "status", "raw_status", "cost", "tokens", "text")


@dataclass(frozen=True)
class MessageRouteAdapter:
    route: str = "message"
    version: str = "opencode-compatible"

    def normalize_record(self, message) -> NormalizedMessageRecord:
        if not isinstance(message, dict):
            return unknown_message_record(message)
        message = self.record(message)
        normalized = dict(message)
        set_missing(normalized, "id", self.value(message, "id", "messageID", "messageId", "message_id"))
        set_missing(normalized, "role", self.value(message, "role", "author", "speaker", "type", "kind"))
        raw_status = self.value(message, "status", "state", "phase")
        if raw_status is not None:
            normalized["status"] = short_status(raw_status)
            if normalized["status"] != raw_status:
                normalized["raw_status"] = raw_status
        set_missing(normalized, "cost", self.value(message, "cost"))
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
        value = first_present(message, *names)
        if value is not None:
            return value
        return first_present(message.get("info"), *names)

    def tokens(self, message):
        return normalized_tokens(self.value(message, "tokens", "token", "tokenUsage", "token_usage", "usage"))

    def text(self, message):
        message = self.record(message)
        text = self.value(message, "text", "content")
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


MESSAGE_ADAPTER = MessageRouteAdapter()

normalize_message_record = MESSAGE_ADAPTER.normalize_record
iter_normalized_message_records = MESSAGE_ADAPTER.iter_normalized_records
iter_message_records = MESSAGE_ADAPTER.iter_records
message_record = MESSAGE_ADAPTER.record
message_value = MESSAGE_ADAPTER.value
message_tokens = MESSAGE_ADAPTER.tokens
message_text = MESSAGE_ADAPTER.text

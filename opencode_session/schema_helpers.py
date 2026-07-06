from typing import Dict, List, Union


JsonValue = Union[None, bool, int, float, str, List["JsonValue"], Dict[str, "JsonValue"]]
JsonObject = Dict[str, JsonValue]


SESSION_ID_ALIASES = ("sessionID", "sessionId", "session_id")
MESSAGE_ID_ALIASES = ("messageID", "messageId", "message_id")
CAMEL_MESSAGE_ID_ALIASES = ("messageID", "messageId")
PROMPT_ID_ALIASES = ("promptID", "promptId")
CALL_ID_ALIASES = ("callID", "callId", "call_id")
REQUEST_ID_ALIASES = ("requestID", "requestId")
DELIVERY_ALIASES = ("delivery", "deliveryMode", "mode")
STATUS_ALIASES = ("status", "state", "phase")


def collection_records(collection, *names):
    if isinstance(collection, list):
        return collection
    if isinstance(collection, dict):
        for name in names:
            records = collection.get(name)
            if isinstance(records, list):
                return records
    return []


def first_present(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def root_or_info_value(record, *names):
    value = first_present(record, *names)
    if value is not None:
        return value
    info = record.get("info") if isinstance(record, dict) else None
    return first_present(info, *names)


def child_value(record, child_name, *names):
    child = record.get(child_name) if isinstance(record, dict) else None
    return first_present(child, *names)


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


def mapping_value(mapping, name):
    if isinstance(mapping, dict) and isinstance(mapping.get(name), dict):
        return mapping[name]
    return None


def set_missing(record, name, value):
    if value is not None and record.get(name) is None:
        record[name] = value


def set_if_present(mapping, key, value):
    if value is not None:
        mapping[key] = value


def string_value(value):
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)

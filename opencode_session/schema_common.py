from typing import TypedDict


class NormalizedSessionRecord(TypedDict, total=False):
    id: str
    directory: str
    title: str
    agent: str
    model: str
    tokens: object
    createdAt: object
    updatedAt: object
    schema_status: str
    raw: object


class NormalizedMessageRecord(TypedDict, total=False):
    id: str
    role: str
    status: str
    raw_status: str
    cost: object
    tokens: object
    text: str


class NormalizedAdmissionRecord(TypedDict, total=False):
    session_id: str
    message_id: str
    delivery: str
    state: str
    raw_state: str
    status: str
    terminal_state: object
    api_path: str
    fallback: dict
    admitted_sequence: object
    promoted_sequence: object


class NormalizedEventRecord(TypedDict, total=False):
    kind: str
    session_id: str
    target_session_id: str
    type: str
    message_id: str
    status: str
    raw_status: str
    delivery: str
    text: str
    tool: str
    call_id: str
    step: str
    title: str
    blocker: str
    blocker_id: str
    question: str
    error: str
    reason: str
    schema_status: str
    raw: object


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


def first_present_in(sources, *names):
    for source in sources:
        value = first_present(source, *names)
        if value is not None:
            return value
    return None


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

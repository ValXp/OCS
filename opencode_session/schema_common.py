from typing import Dict, List, Optional, TypedDict, Union


JsonValue = Union[None, bool, int, float, str, List["JsonValue"], Dict[str, "JsonValue"]]
JsonObject = Dict[str, JsonValue]


class NormalizedSessionRecord(TypedDict):
    id: Optional[str]
    directory: Optional[str]
    title: Optional[str]
    agent: Optional[str]
    model: Optional[str]
    tokens: JsonValue
    createdAt: JsonValue
    updatedAt: JsonValue
    schema_status: str
    raw: JsonValue


class NormalizedMessageRecord(TypedDict):
    id: Optional[str]
    role: Optional[str]
    status: Optional[str]
    raw_status: Optional[str]
    cost: JsonValue
    tokens: JsonValue
    text: str
    raw: JsonValue


class NormalizedAdmissionRecord(TypedDict):
    session_id: str
    message_id: str
    delivery: str
    state: str
    raw_state: str
    status: str
    terminal_state: JsonValue
    api_path: str
    fallback: JsonObject
    admitted_sequence: JsonValue
    promoted_sequence: JsonValue


class NormalizedAbortRecord(TypedDict):
    session_id: str
    accepted: bool
    status: Optional[str]
    raw_status: JsonValue
    response: JsonObject


class NormalizedEventRecord(TypedDict, total=False):
    kind: str
    session_id: Optional[str]
    target_session_id: Optional[str]
    type: Optional[str]
    message_id: Optional[str]
    status: Optional[str]
    raw_status: Optional[str]
    delivery: Optional[str]
    text: Optional[str]
    tool: Optional[str]
    call_id: Optional[str]
    step: Optional[str]
    title: Optional[str]
    blocker: Optional[str]
    blocker_id: Optional[str]
    question: Optional[str]
    error: Optional[str]
    reason: Optional[str]
    schema_status: str
    raw: JsonValue


class DomainRecord(TypedDict, total=False):
    id: str
    name: str
    title: str
    role: Optional[str]
    directory: str
    server_url: str
    session_id: Optional[str]
    agent: Optional[str]
    model: Optional[str]
    prompt: str
    status: str
    lifecycle_state: str
    next_eligible_action: str
    workers: Dict[str, "DomainRecord"]
    dependencies: List[str]
    prompt_ids: List[str]
    retryable_failures: List[str]
    blockers: List[str]
    output_refs: List[str]
    retry_count: int
    retry_limit: int
    timeout_seconds: Optional[float]
    timeout_policy: str
    timeout_started_at: JsonValue
    timed_out_at: JsonValue
    failure_category: Optional[str]
    failure_reason: Optional[str]
    last_failure_category: Optional[str]
    last_failure_reason: Optional[str]
    error: str
    failure_retryable: bool
    manual_retry_required: bool
    result: JsonObject
    cleanup: JsonObject
    abort: JsonObject
    metadata: JsonObject
    capabilities: JsonObject
    route_availability: JsonObject
    raw: JsonValue


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

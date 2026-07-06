from dataclasses import dataclass
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


class WorkerAttemptRecord(TypedDict, total=False):
    id: str
    session_id: Optional[str]
    created_session_ids: List[str]
    status: str
    started_at: JsonValue
    finished_at: JsonValue
    failure_category: Optional[str]
    error: str
    result_status: str
    user_message_id: str
    assistant_message_id: str


# Persisted snapshots are intentionally sparse JSON. The storage boundary hydrates
# them into Worker before core orchestration code sees a worker.
class WorkerSnapshotRecord(TypedDict, total=False):
    id: str
    name: str
    title: str
    role: Optional[str]
    session_id: Optional[str]
    agent: Optional[str]
    model: Optional[str]
    prompt: str
    lifecycle_state: str
    dependencies: List[str]
    prompt_ids: List[str]
    retryable_failures: List[str]
    blockers: List[str]
    output_refs: List[str]
    attempts: List[WorkerAttemptRecord]
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
    cleanup: JsonObject
    abort: JsonObject


class WorkerRequiredFields(TypedDict):
    id: str
    role: Optional[str]
    session_id: Optional[str]
    agent: Optional[str]
    model: Optional[str]
    lifecycle_state: str
    status: str
    next_eligible_action: str
    dependencies: List[str]
    prompt_ids: List[str]
    retry_count: int
    retry_limit: int
    retryable_failures: List[str]
    timeout_seconds: Optional[float]
    timeout_policy: str
    timeout_started_at: JsonValue
    timed_out_at: JsonValue
    failure_category: Optional[str]
    failure_reason: Optional[str]
    last_failure_category: Optional[str]
    last_failure_reason: Optional[str]
    blockers: List[str]
    output_refs: List[str]


class Worker(WorkerRequiredFields, total=False):
    name: str
    title: str
    prompt: str
    error: str
    failure_retryable: bool
    manual_retry_required: bool
    cleanup: JsonObject
    abort: JsonObject
    attempts: List[WorkerAttemptRecord]
    result: JsonObject


WORKER_REQUIRED_FIELD_NAMES = tuple(WorkerRequiredFields.__annotations__)


class WorkerRecordShape(Worker):
    status: str
    next_eligible_action: str


class PersistedRunRecord(TypedDict, total=False):
    name: str
    run_id: str
    directory: str
    server_url: str
    status: str
    retry_count: int
    timeout_seconds: Optional[float]
    blockers: List[str]
    output_refs: List[str]
    workers: Dict[str, WorkerSnapshotRecord]
    created_at: JsonValue
    updated_at: JsonValue


class RunRecord(TypedDict, total=False):
    name: str
    run_id: str
    directory: str
    server_url: str
    status: str
    retry_count: int
    timeout_seconds: Optional[float]
    blockers: List[str]
    output_refs: List[str]
    workers: Dict[str, Worker]
    created_at: JsonValue
    updated_at: JsonValue


class CapabilitiesRecord(TypedDict, total=False):
    route_availability: JsonObject
    blocking_message_available: bool
    blocking_execution_available: bool
    legacy_fallback_available: bool
    wait_route: JsonObject
    prompt_route: JsonObject


class ExecutionResultRecord(TypedDict, total=False):
    session_id: str
    message_ids: JsonObject
    status: str
    raw_status: str
    terminal_state: str
    api_path: JsonObject
    execution_strategy: str
    fallback: JsonObject
    cost: JsonValue
    tokens: JsonValue
    text: str


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


@dataclass(frozen=True)
class FieldSource:
    path: tuple
    aliases: tuple


@dataclass(frozen=True)
class FieldExtractor:
    fields: dict

    def value(self, record, field_name):
        return field_source_value(record, self.fields.get(field_name, ()))

    def named_value(self, record, *names):
        requested_names = set(names)
        for sources in self.fields.values():
            value = field_source_value(record, sources, requested_names=requested_names)
            if value is not None:
                return value
        return None

    def has_any(self, record, field_names):
        return any(self.value(record, field_name) is not None for field_name in field_names)


def field_source_value(record, sources, *, requested_names=None):
    for source in sources:
        if requested_names is not None and not requested_names.intersection(source.aliases):
            continue
        value = first_present(mapping_at_path(record, source.path), *source.aliases)
        if value is not None:
            return value
    return None


def mapping_at_path(record, path):
    current = record
    for name in path:
        if not isinstance(current, dict):
            return None
        current = current.get(name)
    return current if isinstance(current, dict) else None


def first_mapping_at_paths(record, paths):
    for path in paths:
        value = mapping_at_path(record, path)
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

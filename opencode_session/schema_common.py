from typing import Dict, List, Optional, Protocol, TypedDict, Union


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
# them into worker_state.WorkerRecord before core orchestration code mutates them.
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


class HydratedWorker(Protocol):
    """Runtime worker record hydrated from a persisted snapshot."""

    @property
    def worker_id(self) -> str: ...

    @property
    def lifecycle_state(self) -> str: ...

    @property
    def role(self) -> object: ...

    @property
    def session_id(self) -> object: ...

    @property
    def agent(self) -> object: ...

    @property
    def model(self) -> object: ...

    @property
    def prompt(self) -> object: ...

    @property
    def prompt_ids(self) -> List[object]: ...

    @property
    def retry_count(self) -> int: ...

    @property
    def retry_limit(self) -> int: ...

    @property
    def retryable_failures(self) -> List[object]: ...

    @property
    def failure_retryable(self) -> object: ...

    @property
    def failure_category(self) -> object: ...

    @property
    def last_failure_category(self) -> object: ...

    @property
    def timeout_seconds(self) -> object: ...

    @property
    def timeout_policy(self) -> object: ...

    @property
    def timeout_started_at(self) -> object: ...

    @property
    def output_refs(self) -> List[object]: ...

    @property
    def cleanup(self) -> object: ...

    @property
    def abort(self) -> object: ...

    @property
    def has_prompt(self) -> bool: ...

    def retry_available(self, category: object = None) -> bool: ...

    def field(self, field_name: str, default: object = None) -> object: ...

    def set_field(self, field_name: str, value: object) -> "HydratedWorker": ...

    def merge_fields(self, fields: Union[WorkerSnapshotRecord, Worker, None] = None, **kwargs: object) -> "HydratedWorker": ...

    def apply_transition(self, transition: object) -> "HydratedWorker": ...

    def remember_prompt_id(self, prompt_id: Optional[str]) -> "HydratedWorker": ...

    def set_session(
        self,
        session_id: Optional[str],
        *,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> "HydratedWorker": ...

    def to_public_dict(self) -> Worker: ...

    def to_snapshot(self) -> WorkerSnapshotRecord: ...


WORKER_REQUIRED_FIELD_NAMES = tuple(WorkerRequiredFields.__annotations__)


class WorkerOutputRecord(Worker, total=False):
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


class HydratedRunRecord(TypedDict, total=False):
    name: str
    run_id: str
    directory: str
    server_url: str
    status: str
    retry_count: int
    timeout_seconds: Optional[float]
    blockers: List[str]
    output_refs: List[str]
    workers: Dict[str, HydratedWorker]
    created_at: JsonValue
    updated_at: JsonValue


RunRecord = HydratedRunRecord


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
